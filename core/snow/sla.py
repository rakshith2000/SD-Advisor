"""Task SLA reads.

Two ServiceNow tables are involved and they are easy to confuse:

  contract_sla  the DEFINITION - the rule ("Priority 3 (Medium) Resolution",
                2 days, applies to incidents). Configured by admins.
  task_sla      the INSTANCE - that rule attached to one specific ticket,
                carrying the live timing: stage, percentage, has_breached,
                business_time_left, planned_end_time.

The join between them is the reference field task_sla.sla, which points at
contract_sla.sys_id. Querying task_sla with sysparm_display_value=all returns
that field as {"display_value": <definition name>, "value": <sys_id>}, so both
halves arrive in a single call - no second lookup is needed at read time.

Written from scratch rather than reused: the equivalent in the existing audit
tool has a loop-scope bug that leaves its result list empty in almost every
case, so its SLA verdicts cannot be used as a baseline.
"""

import datetime
from typing import Any, Dict, List, Optional, Sequence

from core.logging_setup import get_logger
from core.snow.base import ServiceNowClient, parse_ts

log = get_logger('core.snow.sla')

EPOCH = datetime.datetime(1970, 1, 1)

# Kinds a definition can resolve to.
RESPONSE = 'RESPONSE'        # time to first meaningful contact
RESOLUTION = 'RESOLUTION'    # the customer-facing commitment we score on
VENDOR = 'VENDOR'            # third-party target - tracked, but not our breach
OTHER = 'OTHER'              # unrecognised; used only if nothing better exists
IGNORE = 'IGNORE'            # excluded from every calculation

VALID_KINDS = {RESPONSE, RESOLUTION, VENDOR, OTHER, IGNORE}

# Fallback classification when a definition is not in the configured map.
# Deliberately a fallback only - see classify().
RESPONSE_TOKENS = ('response', 'reaction', 'acknowledge')
RESOLUTION_TOKENS = ('resolution', 'resolve', 'restore', 'fix')

# task_sla.stage values that mean "this record is the live one". ServiceNow
# cancels the old task_sla and creates a new one when priority changes, so a
# ticket routinely carries superseded records alongside the current one.
LIVE_STAGES = {'in_progress', 'in progress', 'paused', 'pause'}


class SlaRecord:
    __slots__ = ('kind', 'name', 'sys_id', 'stage', 'has_breached', 'percentage',
                 'time_left_minutes', 'planned_end', 'active')

    def __init__(self, kind: str, name: str, sys_id: str, stage: str,
                 has_breached: bool, percentage: Optional[float],
                 time_left_minutes: Optional[float],
                 planned_end: Optional[datetime.datetime], active: bool):
        self.kind = kind
        self.name = name
        self.sys_id = sys_id
        self.stage = stage
        self.has_breached = has_breached
        self.percentage = percentage
        self.time_left_minutes = time_left_minutes
        self.planned_end = planned_end
        self.active = active

    @property
    def is_live(self) -> bool:
        """Whether this record represents the SLA currently in force.

        A completed or cancelled record still reports a breach flag, so without
        this a superseded SLA from before a priority change can be read as the
        current state.
        """
        if self.active:
            return True
        return (self.stage or '').strip().lower() in LIVE_STAGES

    def as_dict(self) -> Dict[str, Any]:
        return {
            'kind': self.kind,
            'name': self.name,
            'sys_id': self.sys_id,
            'stage': self.stage,
            'has_breached': self.has_breached,
            'percentage': self.percentage,
            'time_left_minutes': self.time_left_minutes,
            'planned_end': self.planned_end.isoformat() if self.planned_end else None,
            'active': self.active,
            'is_live': self.is_live,
        }


def _duration_to_minutes(raw: Any) -> Optional[float]:
    """ServiceNow durations are datetimes offset from 1970-01-01.

    e.g. business_time_left of '1970-01-03 22:44:51' is 2 days 22h 44m 51s,
    which is 4244.85 minutes.
    """
    parsed = parse_ts(raw)
    if parsed is None:
        return None
    return round((parsed - EPOCH).total_seconds() / 60.0, 2)


def classify_by_name(name: str) -> str:
    """Token fallback. Checked only when the definition is not configured."""
    lowered = (name or '').lower()
    if any(token in lowered for token in RESOLUTION_TOKENS):
        return RESOLUTION
    if any(token in lowered for token in RESPONSE_TOKENS):
        return RESPONSE
    return OTHER


class SlaReader:
    def __init__(self, client: ServiceNowClient, settings=None):
        self.client = client
        self.include_types: List[str] = []
        self._by_sys_id: Dict[str, str] = {}
        self._by_name: Dict[str, str] = {}
        self._duration_by_sys_id: Dict[str, str] = {}

        if settings is not None:
            self._load_definitions(settings)

    # -- configuration -----------------------------------------------------

    def _load_definitions(self, settings) -> None:
        """Read the explicit definition map from config.

        Keyed on contract_sla sys_id so a rename in ServiceNow cannot silently
        change how a ticket is scored. A 'name' key may also be supplied and is
        used as a secondary lookup, which keeps the config readable and lets a
        freshly-added definition be mapped before anyone digs out its sys_id.

        An optional 'duration' records what the definition was set to when the
        map was written. Nothing scores against it - durations come from the
        live task_sla record - but sla-map compares the two and reports drift,
        because retuning an SLA in ServiceNow changes sla_pct_consumed for
        every ticket under it and silently reorders the board.
        """
        self.include_types = [
            str(t).strip() for t in (settings.get('sla.include_types', []) or [])
            if str(t).strip()
        ]

        definitions = settings.get('sla.definitions', {}) or {}
        for key, entry in definitions.items():
            if str(key).startswith('_'):        # documentation keys
                continue

            duration = ''
            if isinstance(entry, str):
                kind, name = entry.strip().upper(), ''
            elif isinstance(entry, dict):
                kind = str(entry.get('kind', '')).strip().upper()
                name = str(entry.get('name', '')).strip()
                duration = str(entry.get('duration', '')).strip()
            else:
                continue

            if kind not in VALID_KINDS:
                log.warning("Ignoring SLA definition %r: kind %r is not one of %s",
                            key, kind, sorted(VALID_KINDS))
                continue

            sys_id = str(key).strip().lower()
            self._by_sys_id[sys_id] = kind
            if name:
                self._by_name[name.lower()] = kind
            if duration:
                self._duration_by_sys_id[sys_id] = duration

        if self._by_sys_id:
            log.info('Loaded %d configured SLA definitions (include_types=%s)',
                     len(self._by_sys_id), self.include_types or 'all')

    def classify(self, sys_id: str, name: str) -> str:
        return self.classify_with_source(sys_id, name)[0]

    def classify_with_source(self, sys_id: str, name: str):
        """Return (kind, source). Source is reported by the sla-map command so
        an operator can see which definitions rely on the fragile fallback."""
        key = (sys_id or '').strip().lower()
        if key and key in self._by_sys_id:
            return self._by_sys_id[key], 'config:sys_id'

        lowered = (name or '').strip().lower()
        if lowered and lowered in self._by_name:
            return self._by_name[lowered], 'config:name'

        kind = classify_by_name(name)
        return kind, 'name-token' if kind != OTHER else 'unmatched'

    # -- reads -------------------------------------------------------------

    def _task_query(self, incident_number: str) -> str:
        query = f'task.number={incident_number}'
        if self.include_types:
            # Dot-walks to contract_sla.type, so OLAs and underpinning
            # contracts never reach the scorer.
            query += '^sla.typeIN' + ','.join(self.include_types)
        return query

    def get_for_task(self, incident_number: str) -> List[SlaRecord]:
        rows = self.client.get_all('task_sla', {
            'sysparm_query': self._task_query(incident_number),
            'sysparm_display_value': 'all',
        })

        records: List[SlaRecord] = []
        for row in rows:
            try:
                records.append(self._parse(row))
            except Exception:
                log.debug('Skipping unparseable task_sla row for %s', incident_number)
        return records

    def get_definitions(self, collection: str = 'incident') -> List[Dict[str, Any]]:
        """Every active SLA definition for a table, with its resolved kind.

        Used by the sla-map verification command; not needed at scoring time,
        because task_sla already carries the definition name and sys_id.
        """
        rows = self.client.get_all('contract_sla', {
            'sysparm_query': f'collection={collection}^active=true',
            'sysparm_fields': 'sys_id,name,type,duration',
            'sysparm_display_value': 'true',
        })

        definitions = []
        for row in rows:
            sys_id = (row.get('sys_id') or '').strip()
            name = (row.get('name') or '').strip()
            kind, source = self.classify_with_source(sys_id, name)
            live = (row.get('duration') or '').strip()
            recorded = self._duration_by_sys_id.get(sys_id.lower(), '')
            definitions.append({
                'sys_id': sys_id,
                'name': name,
                'type': (row.get('type') or '').strip(),
                'duration': live,
                'recorded_duration': recorded,
                # Only meaningful when a duration was recorded; an unrecorded
                # definition is unverified, not drifted.
                'duration_drift': bool(recorded) and live != recorded,
                'kind': kind,
                'source': source,
            })
        return sorted(definitions, key=lambda d: (d['kind'], d['name']))

    def _parse(self, row: Dict[str, Any]) -> SlaRecord:
        def disp(key: str, default: str = '') -> str:
            field = row.get(key)
            if isinstance(field, dict):
                return str(field.get('display_value', default) or default).strip()
            return str(field if field is not None else default).strip()

        def val(key: str) -> Any:
            field = row.get(key)
            if isinstance(field, dict):
                return field.get('value')
            return field

        # task_sla.sla is the reference to contract_sla: display_value is the
        # definition's name, value is its sys_id.
        name = disp('sla')
        sys_id = str(val('sla') or '').strip()

        # Prefer the raw stage value ('in_progress') over the display label
        # ('In Progress'); both are handled by LIVE_STAGES either way.
        stage = str(val('stage') or disp('stage') or '').strip()

        breached = disp('has_breached').lower() in ('true', '1', 'yes')
        active = disp('active').lower() in ('true', '1', 'yes')

        percentage_raw = disp('percentage')
        try:
            percentage = round(float(percentage_raw), 2) if percentage_raw else None
        except ValueError:
            percentage = None

        return SlaRecord(
            kind=self.classify(sys_id, name),
            name=name,
            sys_id=sys_id,
            stage=stage,
            has_breached=breached,
            percentage=percentage,
            time_left_minutes=_duration_to_minutes(val('business_time_left')),
            planned_end=parse_ts(val('planned_end_time')),
            active=active,
        )

    # -- summary used by the signal extractor ------------------------------

    @staticmethod
    def summarise(records: Sequence[SlaRecord]) -> Dict[str, Any]:
        """Collapse a task's SLA records into the fields we score on.

        Precedence, in order:
          1. IGNORE records are dropped entirely.
          2. VENDOR records are held aside - a third-party overrun is reported
             separately rather than counting as our breach.
          3. Resolution SLAs are preferred; response SLAs are only consulted if
             nothing else exists.
          4. Live records beat completed or cancelled ones, so a superseded SLA
             from before a priority change cannot be read as current state.
          5. Among what remains, the record closest to breaching wins - that is
             the one a lead needs to act on.
        """
        summary: Dict[str, Any] = {
            'sla_breached': False,
            'sla_pct_consumed': None,
            'sla_time_left_mins': None,
            'projected_breach_at': None,
            'vendor_sla_breached': False,
            'sla_records': [r.as_dict() for r in records],
        }

        usable = [r for r in records if r.kind != IGNORE]
        if not usable:
            return summary

        vendor = [r for r in usable if r.kind == VENDOR]
        if vendor:
            vendor_live = [r for r in vendor if r.is_live] or vendor
            summary['vendor_sla_breached'] = any(r.has_breached for r in vendor_live)

        core = [r for r in usable if r.kind != VENDOR]
        if not core:
            return summary

        resolution = [r for r in core if r.kind == RESOLUTION]
        candidates = (resolution
                      or [r for r in core if r.kind != RESPONSE]
                      or core)

        live = [r for r in candidates if r.is_live]
        candidates = live or candidates

        summary['sla_breached'] = any(r.has_breached for r in candidates)

        def urgency_key(record: SlaRecord):
            # Breached first, then least time remaining, then most consumed.
            return (
                0 if record.has_breached else 1,
                record.time_left_minutes if record.time_left_minutes is not None else 1e12,
                -(record.percentage or 0.0),
            )

        worst = sorted(candidates, key=urgency_key)[0]
        summary['sla_pct_consumed'] = worst.percentage
        summary['sla_time_left_mins'] = worst.time_left_minutes
        summary['projected_breach_at'] = worst.planned_end
        return summary
