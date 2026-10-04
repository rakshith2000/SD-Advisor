"""Delta sync from ServiceNow into watched_ticket.

Runs every few minutes. Two modes:
  * delta   - everything touched since the watermark. Cheap, the normal path.
  * full    - the whole aged backlog. Runs on start-up and nightly so tickets
              that crossed the age threshold without being updated (precisely
              the ones leads care about) cannot be missed by a delta-only feed.

The watermark is deliberately rewound by a small overlap before each run:
ServiceNow's sys_updated_on has second granularity and records can land out of
order, so an exact-boundary watermark drops updates.

Every timestamp written here is naive UTC, taken from the ServiceNow `value`
field rather than `display_value`. See core.timeutil.
"""

import datetime
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from core.logging_setup import get_logger
from core.snow.base import display_value, reference_sys_id, utc_ts, utc_value
from core.snow.incidents import OPEN_STATES
from core.timeutil import utc_now

log = get_logger('pipeline.sync')

WATERMARK_KEY = 'incident_delta'
OVERLAP_MINUTES = 5

# The same states the fetch asks for, as a set for membership testing. Derived
# from OPEN_STATES rather than written out again: two copies of this list would
# eventually disagree, and the direction of that failure is a resolved ticket
# sitting on the board.
OPEN_STATE_VALUES = {s.strip() for s in OPEN_STATES.split(',') if s.strip()}

# How far back the first ever run looks when there is no watermark.
COLD_START_DAYS = 45


def _hash_content(ticket: Dict[str, Any]) -> str:
    """Change fingerprint. If this is unchanged we skip re-analysis entirely."""
    payload = json.dumps({
        'updated': ticket.get('sys_updated_on'),
        'state': ticket.get('state'),
        'hold': ticket.get('hold_reason'),
        'group': ticket.get('assignment_group'),
        'agent': ticket.get('assigned_to'),
        'priority': ticket.get('priority'),
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def flatten_incident(raw: Dict[str, Any]) -> Dict[str, Any]:
    """A sysparm_display_value=all payload -> a watched_ticket row.

    Under `all` EVERY field arrives as {'display_value': ..., 'value': ...},
    including plain strings such as number and short_description. Reading one
    straight out of the payload yields a dict, not a string, so every field
    goes through display_value() or utc_ts() without exception.
    """
    return {
        'incident_number': display_value(raw.get('number')),
        'sys_id': display_value(raw.get('sys_id')),
        'short_description': display_value(raw.get('short_description')),
        'description': display_value(raw.get('description')),
        'caller_name': display_value(raw.get('caller_id')),
        'caller_sys_id': reference_sys_id(raw.get('caller_id')),
        'assigned_to': display_value(raw.get('assigned_to')),
        'assignment_group': display_value(raw.get('assignment_group')),
        'state': display_value(raw.get('state')),
        'hold_reason': display_value(raw.get('hold_reason')),
        'priority': display_value(raw.get('priority')),
        'impact': display_value(raw.get('impact')),
        'urgency': display_value(raw.get('urgency')),
        'category': display_value(raw.get('category')),
        'subcategory': display_value(raw.get('subcategory')),
        'ci': display_value(raw.get('cmdb_ci')),
        'contact_type': display_value(raw.get('contact_type')),
        'rfc': display_value(raw.get('rfc')),
        'problem_id': display_value(raw.get('problem_id')),
        # Datetimes come from `value` (UTC); everything above from
        # `display_value`. See core.timeutil for why.
        'opened_at': utc_ts(raw.get('opened_at')) or utc_ts(raw.get('sys_created_on')),
        'sys_updated_on': utc_ts(raw.get('sys_updated_on')),
        'reassignment_count': int(display_value(raw.get('reassignment_count')) or 0),
        'reopen_count': int(display_value(raw.get('reopen_count')) or 0),
        'active': 1,
    }


class TicketSync:
    def __init__(self, db, incident_reader, settings):
        self.db = db
        self.reader = incident_reader
        self.settings = settings

    # -- entry points ------------------------------------------------------

    def run(self, full: bool = False) -> Dict[str, Any]:
        started = utc_now()
        try:
            if full:
                rows, high_water = self._fetch_full(started)
                mode = 'full'
            else:
                rows, high_water = self._fetch_delta(started)
                mode = 'delta'

            changed = self._persist(rows, started)
            retired = self._retire_closed(rows, full, started)

            self.db.set_sync_watermark(
                WATERMARK_KEY, high_water, 'OK',
                f'mode={mode} fetched={len(rows)} changed={changed} retired={retired}')

            log.info('Sync %s complete: %d fetched, %d changed, %d retired (%.1fs)',
                     mode, len(rows), changed, retired,
                     (utc_now() - started).total_seconds())

            return {'mode': mode, 'fetched': len(rows), 'changed': changed,
                    'retired': retired, 'watermark': high_water}

        except Exception as exc:
            self.db.set_sync_watermark(
                WATERMARK_KEY, self.db.get_sync_watermark(WATERMARK_KEY), 'ERROR', str(exc))
            log.exception('Sync failed')
            raise

    # -- fetching ----------------------------------------------------------

    def _fetch_delta(self, now: datetime.datetime) -> Tuple[List[Dict[str, Any]], datetime.datetime]:
        watermark = self.db.get_sync_watermark(WATERMARK_KEY)

        if watermark is None:
            log.info('No watermark found - performing cold-start full sync')
            return self._fetch_full(now)

        since = watermark - datetime.timedelta(minutes=OVERLAP_MINUTES)
        rows = self.reader.get_changed_since(since)
        return rows, self._high_water(rows, fallback=now)

    def _fetch_full(self, now: datetime.datetime) -> Tuple[List[Dict[str, Any]], datetime.datetime]:
        aged = self.reader.get_aged_open_incidents(
            self.settings.aged_after_days, now=now)

        # Also pull younger open tickets so their history is already local by
        # the time they age in. Bounded by COLD_START_DAYS.
        recent_cutoff = now - datetime.timedelta(days=COLD_START_DAYS)
        recent = self.reader.get_changed_since(recent_cutoff)

        merged: Dict[str, Dict[str, Any]] = {}
        for row in list(aged) + list(recent):
            number = display_value(row.get('number'))
            if number:
                merged[number] = row

        rows = list(merged.values())
        return rows, self._high_water(rows, fallback=now)

    @staticmethod
    def _high_water(rows: List[Dict[str, Any]],
                    fallback: datetime.datetime) -> datetime.datetime:
        stamps = [utc_ts(r.get('sys_updated_on')) for r in rows]
        stamps = [s for s in stamps if s is not None]
        return max(stamps) if stamps else fallback

    # -- persistence -------------------------------------------------------

    def _persist(self, rows: List[Dict[str, Any]], now: datetime.datetime) -> int:
        existing = {
            r['incident_number']: r['content_hash']
            for r in self.db.retrieve('watched_ticket', columns=['incident_number', 'content_hash'])
        }

        changed = 0
        for raw in rows:
            record = flatten_incident(raw)
            if not record['incident_number']:
                continue

            record['content_hash'] = _hash_content(record)
            record['last_synced_at'] = now
            record['first_seen_at'] = now

            if existing.get(record['incident_number']) != record['content_hash']:
                changed += 1

            self.db.upsert(
                'watched_ticket', record,
                update_columns=[c for c in record if c not in ('incident_number', 'first_seen_at')],
            )

        return changed

    def _retire_closed(self, rows: List[Dict[str, Any]], full: bool,
                       now: datetime.datetime) -> int:
        """Reconcile tickets that have left the fetch, and retire the departed.

        This is the only thing standing between the board and a permanently
        stale row, because a query returns what matches and can never return
        what stopped matching. Every fetch carries three conditions -
        active=true, state in 1,2,3, and the group scope - so the instant a
        ticket is resolved or reassigned out of our queues it simply vanishes
        from the feed. Nothing updates the local row again, and it sits on the
        board indefinitely showing the group it used to be in.

        Asking "is it still active?" is not enough to catch that. A ticket
        drops out for three different reasons and only one of them clears the
        active flag:

            closed       active=false              -> retire
            resolved     active usually STAYS true -> retire on state
            reassigned   active=true, still open   -> retire on group

        Resolved is the one that bites. On most instances `active` is not
        cleared until auto-close runs days later, so a resolved incident looks
        open to a flag check and stays on the board for the whole window.

        The row is refreshed from ServiceNow before being retired, so what is
        left behind records where the ticket actually went rather than the
        last thing we happened to see.

        Only runs after a full sync - a delta response legitimately omits
        tickets that simply were not touched, and retiring on that would empty
        the board every ten minutes.
        """
        if not full:
            return 0

        seen = {display_value(r.get('number')) for r in rows}
        tracked = self.db.retrieve(
            'watched_ticket', columns=['incident_number'],
            conditions=[{'col': 'active', 'op': 'eq', 'val': 1}])

        gone = [t['incident_number'] for t in tracked if t['incident_number'] not in seen]
        if not gone:
            return 0

        # One read per departed ticket, so the work is bounded by how many
        # left since the last full sync - normally a handful. The ceiling is
        # here for the abnormal case: a mistyped group name narrows the fetch
        # to nothing, every tracked ticket lands in `gone` at once, and this
        # loop would otherwise issue thousands of calls.
        ceiling = int(self.settings.get('sync.max_reconcile_per_run', 500) or 500)
        if len(gone) > ceiling:
            log.warning('%d tickets left the fetch; reconciling the first %d. '
                        'Check the configured assignment groups if this repeats.',
                        len(gone), ceiling)
            gone = gone[:ceiling]

        tracked_groups = {g.strip().lower()
                          for g in (self.settings.assignment_groups or []) if g.strip()}

        retired = 0
        unreadable = 0

        for number in gone:
            try:
                current = self.reader.get_by_number(number)
            except Exception:
                # Cannot tell why it left, so change nothing. Retiring on a
                # transport error would clear the board during an outage.
                unreadable += 1
                continue

            if current is None:
                # Deleted, or no longer readable by the integration user. Either
                # way it cannot be reviewed, and leaving it on the board offers
                # a ticket nobody can open.
                self._retire(number, now, 'no longer visible in ServiceNow')
                retired += 1
                continue

            state = utc_value(current.get('state'))
            active = utc_value(current.get('active')).strip().lower() in ('true', '1')
            group = display_value(current.get('assignment_group'))

            # Refresh first, retire second: flatten_incident always writes
            # active=1, so the order matters, and the stored row should say
            # where the ticket went.
            self._persist([current], now)

            if not active or state not in OPEN_STATE_VALUES:
                self._retire(number, now, f'state={state or "?"} active={active}')
                retired += 1
            elif tracked_groups and group.strip().lower() not in tracked_groups:
                self._retire(number, now, f'reassigned to {group or "an unnamed group"}')
                retired += 1
            # Otherwise it is still open and still ours. It fell out of the
            # fetch for some other reason - most often a full sync whose
            # windows did not reach it - and the refresh above was the point.

        if unreadable:
            log.warning('%d departed tickets could not be re-read and were left '
                        'untouched', unreadable)

        return retired

    def _retire(self, number: str, now: datetime.datetime, reason: str) -> None:
        self.db.update('watched_ticket', {'active': 0, 'last_synced_at': now},
                       conditions=[{'col': 'incident_number', 'op': 'eq', 'val': number}])
        log.info('Retired %s from the board: %s', number, reason)

    # -- selection for analysis -------------------------------------------

    def aged_tickets_needing_attention(self, include_snoozed: bool = False) -> List[Dict[str, Any]]:
        """In-scope, active, older than the threshold, not snoozed."""
        cutoff = utc_now() - datetime.timedelta(days=self.settings.aged_after_days)

        sql = """
            SELECT w.*
              FROM watched_ticket w
              LEFT JOIN suppression s ON s.incident_number = w.incident_number
             WHERE w.active = 1
               AND w.opened_at <= %s
        """
        params: List[Any] = [cutoff]

        if not include_snoozed:
            sql += ' AND (s.incident_number IS NULL OR s.snoozed_until <= UTC_TIMESTAMP())'

        groups = self.settings.assignment_groups
        if groups:
            sql += ' AND w.assignment_group IN (' + ', '.join(['%s'] * len(groups)) + ')'
            params.extend(groups)

        sql += ' ORDER BY w.opened_at ASC'
        return self.db.query(sql, params)
