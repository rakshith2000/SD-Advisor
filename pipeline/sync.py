"""Delta sync from ServiceNow into watched_ticket.

Runs every few minutes. Two modes:
  * delta   - everything touched since the watermark. Cheap, the normal path.
  * full    - the whole aged backlog. Runs on start-up and nightly so tickets
              that crossed the age threshold without being updated (precisely
              the ones leads care about) cannot be missed by a delta-only feed.

The watermark is deliberately rewound by a small overlap before each run:
ServiceNow's sys_updated_on has second granularity and records can land out of
order, so an exact-boundary watermark drops updates.
"""

import datetime
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from core.logging_setup import get_logger
from core.snow.base import display_value, parse_ts, reference_sys_id

log = get_logger('pipeline.sync')

WATERMARK_KEY = 'incident_delta'
OVERLAP_MINUTES = 5

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
    """ServiceNow display-value payload -> watched_ticket row."""
    return {
        'incident_number': (raw.get('number') or '').strip(),
        'sys_id': (raw.get('sys_id') or '').strip(),
        'short_description': (raw.get('short_description') or '').strip(),
        'description': raw.get('description') or '',
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
        'opened_at': parse_ts(raw.get('opened_at')) or parse_ts(raw.get('sys_created_on')),
        'sys_updated_on': parse_ts(raw.get('sys_updated_on')),
        'reassignment_count': int(str(raw.get('reassignment_count') or 0) or 0),
        'reopen_count': int(str(raw.get('reopen_count') or 0) or 0),
        'active': 1,
    }


class TicketSync:
    def __init__(self, db, incident_reader, settings):
        self.db = db
        self.reader = incident_reader
        self.settings = settings

    # -- entry points ------------------------------------------------------

    def run(self, full: bool = False) -> Dict[str, Any]:
        started = datetime.datetime.now()
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
                     (datetime.datetime.now() - started).total_seconds())

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
            number = (row.get('number') or '').strip()
            if number:
                merged[number] = row

        rows = list(merged.values())
        return rows, self._high_water(rows, fallback=now)

    @staticmethod
    def _high_water(rows: List[Dict[str, Any]],
                    fallback: datetime.datetime) -> datetime.datetime:
        stamps = [parse_ts(r.get('sys_updated_on')) for r in rows]
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
        """Mark tickets that have left the open set as inactive.

        Only safe after a full sync - a delta response legitimately omits
        tickets that simply were not touched.
        """
        if not full:
            return 0

        seen = {(r.get('number') or '').strip() for r in rows}
        tracked = self.db.retrieve(
            'watched_ticket', columns=['incident_number'],
            conditions=[{'col': 'active', 'op': 'eq', 'val': 1}])

        gone = [t['incident_number'] for t in tracked if t['incident_number'] not in seen]
        if not gone:
            return 0

        # Re-check individually rather than assuming: a ticket can drop out of
        # the query because its group changed, not because it closed.
        retired = 0
        for number in gone:
            try:
                current = self.reader.get_by_number(number)
            except Exception:
                continue

            still_open = bool(current) and str(current.get('active', '')).lower() in ('true', '1')
            if not still_open:
                self.db.update('watched_ticket', {'active': 0, 'last_synced_at': now},
                               conditions=[{'col': 'incident_number', 'op': 'eq', 'val': number}])
                retired += 1

        return retired

    # -- selection for analysis -------------------------------------------

    def aged_tickets_needing_attention(self, include_snoozed: bool = False) -> List[Dict[str, Any]]:
        """In-scope, active, older than the threshold, not snoozed."""
        cutoff = datetime.datetime.now() - datetime.timedelta(days=self.settings.aged_after_days)

        sql = """
            SELECT w.*
              FROM watched_ticket w
              LEFT JOIN suppression s ON s.incident_number = w.incident_number
             WHERE w.active = 1
               AND w.opened_at <= %s
        """
        params: List[Any] = [cutoff]

        if not include_snoozed:
            sql += ' AND (s.incident_number IS NULL OR s.snoozed_until <= NOW())'

        groups = self.settings.assignment_groups
        if groups:
            sql += ' AND w.assignment_group IN (' + ', '.join(['%s'] * len(groups)) + ')'
            params.extend(groups)

        sql += ' ORDER BY w.opened_at ASC'
        return self.db.query(sql, params)
