"""The insight pages: eight sections, one per question somebody actually asks.

Each section is a method returning panels. A panel is data, not markup -
{kind, title, note, body} - and one template renders them all, so a new panel
is a dict here rather than another page that can drift from its siblings.

Three rules hold across every section.

**Scope is applied in SQL, never in the template.** Every query carries the
caller's queues. A chart that aggregates the whole estate and is merely
captioned with one queue's name is a disclosure wearing a label - and on an
aggregate it is invisible, because there are no incident numbers on screen to
notice it by.

**Trends read signal_daily; current state reads v_current_board.** The raw
ticket_signal table is around eight million rows and is not something to put
behind a page somebody opens every morning.

**A figure that cannot be computed honestly is absent, not zero.** "0%
agreement" and "nobody has judged anything yet" are different facts that lead
to different actions, and showing the first when the second is true is how a
dashboard starts lying.
"""

import datetime
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core import charts
from core.logging_setup import get_logger
from core.timeutil import utc_now
from delivery.digest import ACTION_LABELS, FLAG_LABELS

log = get_logger('web.insights')

# (key, label, blurb). The order is the tab order and it is deliberate:
# today first, then the trend behind today, then the parts that explain it.
SECTIONS: List[Tuple[str, str, str]] = [
    ('operations', 'Operations',
     'What needs doing this morning, and who it is waiting on.'),
    ('backlog', 'Backlog health',
     'Is the aged pile under control, and if not, why not.'),
    ('risk', 'SLA and risk',
     'What has breached, what is about to, and what nobody was warned about.'),
    ('people', 'People and workload',
     'Who is carrying the aged load, and how responsive each queue is.'),
    ('quality', 'Recommendation quality',
     'Whether the advice is good, and whether its confidence is honest.'),
    ('knowledge', 'Knowledge base',
     'Whether articles are found, used and right - and where the gaps are.'),
    ('health', 'Service health',
     'Is the service itself working, and is anybody using it.'),
    ('summary', 'Executive summary',
     'The before and after, for a review meeting.'),
]

SECTION_KEYS = [key for key, _label, _blurb in SECTIONS]

AGREED = "('ACCEPTED', 'MODIFIED', 'DONE')"

# -- time range -------------------------------------------------------------

RANGES: List[Tuple[str, str, int]] = [
    ('7d', 'Last 7 days', 7),
    ('30d', 'Last 30 days', 30),
    ('90d', 'Last 90 days', 90),
    ('180d', 'Last 180 days', 180),
]
DEFAULT_RANGE = '30d'

# ticket_signal is pruned at 180 days. Offering a year would draw a flat line
# across the first nine months of it, which reads as a quiet period rather
# than as an absence of data.
MAX_RANGE_DAYS = 180

_PRESET_DAYS = {key: days for key, _label, days in RANGES}


class TimeRange:
    """The selected window: a named preset, or an explicit pair of dates."""

    def __init__(self, key: str, start: datetime.date, end: datetime.date):
        self.key = key
        self.start = start
        self.end = end

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def is_custom(self) -> bool:
        return self.key == 'custom'

    @property
    def label(self) -> str:
        for key, label, _days in RANGES:
            if key == self.key:
                return label
        return f'{self.start:%d %b} to {self.end:%d %b %Y}'

    @property
    def since(self) -> datetime.datetime:
        return datetime.datetime.combine(self.start, datetime.time.min)

    @property
    def until(self) -> datetime.datetime:
        # The whole of the final day. A midnight boundary silently drops
        # everything recorded today, which is the day people ask about most.
        return datetime.datetime.combine(self.end, datetime.time.max)


def parse_range(key: Optional[str], start: Optional[str], end: Optional[str],
                today: Optional[datetime.date] = None) -> TimeRange:
    """Build a TimeRange from query parameters. Never raises.

    A bad value falls back to the default rather than erroring: the range is
    a view preference and lives in the URL, where people edit it by hand.
    Refusing to render the page is the wrong severity of response.
    """
    today = today or utc_now().date()

    if key == 'custom' or (start and end):
        parsed_start, parsed_end = _date(start), _date(end)
        if parsed_start and parsed_end:
            if parsed_end < parsed_start:
                parsed_start, parsed_end = parsed_end, parsed_start
            # Clamped at both ends. A start before retention draws a flat
            # line; an end in the future draws empty columns that read as an
            # outage.
            parsed_end = min(parsed_end, today)
            parsed_start = max(
                parsed_start,
                today - datetime.timedelta(days=MAX_RANGE_DAYS - 1))
            return TimeRange('custom', parsed_start, parsed_end)

    days = _PRESET_DAYS.get(key or '', _PRESET_DAYS[DEFAULT_RANGE])
    chosen = key if key in _PRESET_DAYS else DEFAULT_RANGE
    return TimeRange(chosen, today - datetime.timedelta(days=days - 1), today)


def _date(value: Optional[str]) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.datetime.strptime(str(value).strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


# -- panel constructors ------------------------------------------------------

def tile(label: str, value: Any, note: str = '', tone: Optional[str] = None,
         spark: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """A stat tile.

    `note` is mandatory in practice wherever `tone` is set: status colour
    never carries meaning on its own, so the words are what survive a
    greyscale print or a reader who cannot separate the red from the green.
    """
    return {'kind': 'tile', 'label': label, 'value': value, 'note': note,
            'tone': tone,
            'spark': charts.sparkline([v for v in spark]) if spark else None}


def panel(title: str, body: Any, note: str = '', width: str = 'half',
          footnote: str = '', legend: Sequence[Dict[str, Any]] = ()
          ) -> Dict[str, Any]:
    return {'kind': 'chart', 'title': title, 'note': note, 'body': body,
            'width': width, 'footnote': footnote, 'legend': list(legend)}


def table_panel(title: str, headings: Sequence[str],
                rows: Sequence[Sequence[Any]], note: str = '',
                width: str = 'half', numeric: Sequence[int] = (),
                link: Optional[int] = None, footnote: str = '',
                empty: str = 'Nothing to show.') -> Dict[str, Any]:
    return {'kind': 'table', 'title': title, 'note': note,
            'headings': list(headings), 'rows': [list(r) for r in rows],
            'numeric': list(numeric), 'link': link, 'width': width,
            'footnote': footnote, 'empty': empty}


def pct(numerator: Any, denominator: Any) -> Optional[float]:
    n, d = int(numerator or 0), int(denominator or 0)
    return round(100.0 * n / d, 1) if d else None


def show(value: Optional[float], suffix: str = '%') -> str:
    return charts.DASH if value is None else f'{value:g}{suffix}'


def tone_at(value: Optional[float], good_at: float) -> Optional[str]:
    if value is None:
        return None
    return 'good' if value >= good_at else 'serious'


def days_text(value: Any) -> str:
    try:
        whole = int(float(value or 0))
    except (TypeError, ValueError):
        return charts.DASH
    if whole < 1:
        return 'Under a day'
    return f'{whole} Day' if whole == 1 else f'{whole} Days'


def stamp(value: Any) -> str:
    if not value:
        return charts.DASH
    try:
        return value.strftime('%d %b %H:%M')
    except AttributeError:
        return str(value)


def flags_of(row: Dict[str, Any]) -> List[str]:
    """risk_flags comes back as a list or a JSON string depending on driver."""
    raw = row.get('risk_flags')
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def owner_name(row: Dict[str, Any]) -> str:
    """SERVICE_DESK is a role, not a queue: on one row the useful answer is
    the name of the team the incident is actually assigned to."""
    if row.get('pending_action_owner') == 'SERVICE_DESK':
        return (row.get('assignment_group') or 'Assigned team').strip()
    return (row.get('pending_action_owner') or charts.DASH
            ).replace('_', ' ').title()


OWNER_LABELS = {
    'SERVICE_DESK': 'Our team', 'CALLER': 'Caller', 'VENDOR': 'Vendor',
    'CHANGE': 'Change', 'PROBLEM': 'Problem', 'APPROVAL': 'Approval',
    'NONE': 'Nobody',
}


class Insights:
    """Builds one section at a time, scoped to the caller's queues."""

    def __init__(self, ctx, groups: Sequence[str], window: TimeRange):
        self.ctx = ctx
        self.db = ctx.db
        self.groups = [g for g in groups if str(g).strip()]
        self.window = window

    # -- scoped query plumbing ---------------------------------------------

    def scope(self, column: str = 'assignment_group') -> Tuple[str, List[Any]]:
        """The WHERE fragment restricting to the caller's queues.

        An empty scope yields a clause that matches nothing. That is the
        whole point: an unprovisioned account must see an empty page, never
        the estate. Omitting the clause would turn "entitled to nothing" into
        "entitled to everything", which reads as success.
        """
        if not self.groups:
            return ' AND 1 = 0', []
        return (f' AND {column} IN ({", ".join(["%s"] * len(self.groups))})',
                list(self.groups))

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        try:
            return self.db.query(sql, list(params))
        except Exception:
            # A missing rollup table or one slow query must degrade a single
            # panel, not a page carrying seven others.
            log.exception('Insights query failed')
            return []

    def one(self, sql: str, params: Sequence[Any] = ()) -> Dict[str, Any]:
        rows = self.query(sql, params)
        return rows[0] if rows else {}

    def daily(self, columns: str) -> List[Dict[str, Any]]:
        """A scoped daily series off the rollup, summed across queues."""
        clause, params = self.scope()
        return self.query(f"""
            SELECT day, {columns}
              FROM signal_daily
             WHERE day BETWEEN %s AND %s {clause}
             GROUP BY day ORDER BY day
        """, [self.window.start, self.window.end] + params)

    def board(self) -> List[Dict[str, Any]]:
        clause, params = self.scope()
        return self.query(
            f'SELECT * FROM v_current_board WHERE snoozed = 0 {clause}',
            params)

    @staticmethod
    def labels(rows: Sequence[Dict[str, Any]]) -> List[str]:
        out = []
        for row in rows:
            day = row['day']
            out.append(day.strftime('%d %b') if hasattr(day, 'strftime')
                       else str(day))
        return out

    # -- dispatch -----------------------------------------------------------

    def build(self, section: str) -> Dict[str, Any]:
        return {
            'operations': self.operations,
            'backlog': self.backlog,
            'risk': self.risk,
            'people': self.people,
            'quality': self.quality,
            'knowledge': self.knowledge,
            'health': self.health,
            'summary': self.summary,
        }.get(section, self.operations)()

    # ===================================================================
    # A. Operations
    # ===================================================================

    def operations(self) -> Dict[str, Any]:
        rows = self.board()
        total = len(rows)
        clause, params = self.scope()

        team_owned = sum(1 for r in rows
                         if r.get('pending_action_owner') == 'SERVICE_DESK')
        breached = sum(1 for r in rows if r.get('sla_breached'))
        stale = sum(1 for r in rows if 'PROLONGED_INACTIVITY' in flags_of(r))
        unassigned = sum(1 for r in rows
                         if not (r.get('assigned_to') or '').strip())

        soon = self.one(f"""
            SELECT COUNT(*) AS n FROM v_current_board
             WHERE snoozed = 0 AND sla_breached = 0
               AND projected_breach_at IS NOT NULL
               AND projected_breach_at
                   <= DATE_ADD(UTC_TIMESTAMP(), INTERVAL 48 HOUR) {clause}
        """, params)

        owners: Dict[str, int] = {}
        actions: Dict[str, int] = {}
        flag_counts: Dict[str, int] = {}
        for row in rows:
            key = row.get('pending_action_owner') or 'NONE'
            owners[key] = owners.get(key, 0) + 1
            action = row.get('recommended_action') or 'NOT_ANALYSED'
            actions[action] = actions.get(action, 0) + 1
            for flag in flags_of(row):
                flag_counts[flag] = flag_counts.get(flag, 0) + 1

        worklist = sorted(rows,
                          key=lambda r: -(r.get('attention_score') or 0))[:12]

        return {
            'tiles': [
                tile('Aged tickets', total,
                     f'open more than {self.ctx.settings.aged_after_days} days'),
                tile('Awaiting our team', team_owned,
                     f'{total - team_owned} are with other parties',
                     'critical' if team_owned else 'good'),
                tile('SLA breached', breached, 'already past target',
                     'critical' if breached else 'good'),
                tile('No action 4+ days', stale, 'prolonged inactivity',
                     'serious' if stale else 'good'),
                tile('Breaching in 48h', int(soon.get('n') or 0),
                     'still actionable today',
                     'warning' if soon.get('n') else 'good'),
                tile('Unassigned', unassigned, 'nobody owns these',
                     'serious' if unassigned else 'good'),
            ],
            'panels': [
                panel('Who we are waiting on',
                      charts.hbar_chart([
                          {'label': OWNER_LABELS.get(k, k), 'value': v,
                           'colour': charts.STATUS['critical']
                                     if k == 'SERVICE_DESK'
                                     else charts.SERIES[0]}
                          for k, v in sorted(owners.items(),
                                             key=lambda kv: -kv[1])],
                          label_w=110),
                      'by pending action owner', 'third',
                      footnote='Everything outside the first bar is waiting '
                               'on somebody else. Chasing those is the most '
                               'common way a review morning is wasted.'),
                panel('Suggested next step',
                      charts.hbar_chart([
                          {'label': ACTION_LABELS.get(k, 'Not yet analysed'),
                           'value': v}
                          for k, v in sorted(actions.items(),
                                             key=lambda kv: -kv[1])],
                          label_w=155),
                      'the shape of today', 'third'),
                panel('Risk flags in play',
                      charts.hbar_chart([
                          {'label': FLAG_LABELS.get(
                              k, k.replace('_', ' ').title()), 'value': v,
                           'colour': charts.STATUS['critical']
                                     if k in ('SLA_BREACHED',
                                              'PROLONGED_INACTIVITY')
                                     else charts.SERIES[0]}
                          for k, v in sorted(flag_counts.items(),
                                             key=lambda kv: -kv[1])[:8]],
                          label_w=155),
                      'a ticket can carry several', 'third',
                      footnote='Which is why this is bars and not a pie - '
                               'the parts would sum past the whole.'),
                table_panel(
                    'Attention-ranked worklist',
                    ['Score', 'Incident', 'Summary', 'Assigned to', 'Age',
                     'Idle', 'Pending on', 'Suggested step'],
                    [[r.get('attention_score'), r['incident_number'],
                      (r.get('short_description') or '')[:70],
                      r.get('assigned_to') or 'Unassigned',
                      days_text(r.get('age_days')),
                      days_text(r.get('idle_days')),
                      owner_name(r),
                      ACTION_LABELS.get(r.get('recommended_action') or '',
                                        charts.DASH)]
                     for r in worklist],
                    'working down from the top is the review', 'full',
                    numeric=(0,), link=1,
                    empty='Nothing aged in your queues. That is the good '
                          'outcome, not a broken panel.'),
            ],
        }

    # ===================================================================
    # B. Backlog health
    # ===================================================================

    def backlog(self) -> Dict[str, Any]:
        rows = self.daily(
            'SUM(tickets) AS tickets, SUM(newly_aged) AS newly, '
            'SUM(retired) AS retired, SUM(band_5_10) AS b1, '
            'SUM(band_10_20) AS b2, SUM(band_20_30) AS b3, '
            'SUM(band_30_plus) AS b4, MAX(p90_age_days) AS p90, '
            'AVG(median_age_days) AS median_age')

        if not rows:
            return self._awaiting_rollup()

        labels = self.labels(rows)
        tickets = [int(r['tickets'] or 0) for r in rows]
        newly = [int(r['newly'] or 0) for r in rows]
        retired = [int(r['retired'] or 0) for r in rows]
        change = tickets[-1] - tickets[0]
        p90_now = float(rows[-1].get('p90') or 0)
        p90_then = float(rows[0].get('p90') or 0)

        bands = [
            {'name': '5-10 days', 'values': [int(r['b1'] or 0) for r in rows],
             'colour': charts.RAMP[0]},
            {'name': '10-20 days', 'values': [int(r['b2'] or 0) for r in rows],
             'colour': charts.RAMP[1]},
            {'name': '20-30 days', 'values': [int(r['b3'] or 0) for r in rows],
             'colour': charts.RAMP[2]},
            {'name': '30+ days', 'values': [int(r['b4'] or 0) for r in rows],
             'colour': charts.RAMP[3]},
        ]

        clause, params = self.scope()
        queues = self.query(f"""
            SELECT assignment_group,
                   ROUND(SUM(tickets) / COUNT(DISTINCT day), 1) AS avg_open
              FROM signal_daily
             WHERE day BETWEEN %s AND %s {clause}
             GROUP BY assignment_group ORDER BY avg_open DESC
        """, [self.window.start, self.window.end] + params)

        return {
            'tiles': [
                tile('Aged now', tickets[-1],
                     f'{abs(change)} {"fewer" if change <= 0 else "more"} '
                     f'than {self.window.days} days ago',
                     'good' if change <= 0 else 'serious', spark=tickets),
                tile('Median age',
                     days_text(rows[-1].get('median_age')),
                     'what a typical aged ticket looks like'),
                tile('p90 age', days_text(p90_now),
                     f'was {days_text(p90_then)} - the tail is what matters',
                     'critical' if p90_now > p90_then else 'good'),
                tile('Arrived / retired', f'{sum(newly)} / {sum(retired)}',
                     'across the whole range',
                     'good' if sum(retired) >= sum(newly) else 'serious'),
            ],
            'panels': [
                panel('Aged backlog',
                      charts.line_chart(
                          labels,
                          [{'name': 'Aged tickets', 'values': tickets,
                            'area': True}], height=230),
                      'daily count past the aged threshold', 'half'),
                panel('Arrived against retired',
                      charts.bar_chart(labels, [
                          {'name': 'Newly aged', 'values': newly,
                           'colour': charts.SERIES[1]},
                          {'name': 'Retired', 'values': retired,
                           'colour': charts.SERIES[0]}], height=230),
                      'same unit, one axis', 'half',
                      legend=[{'name': 'Newly aged',
                               'colour': charts.SERIES[1]},
                              {'name': 'Retired',
                               'colour': charts.SERIES[0]}],
                      footnote='Never two scales on one frame. Retired covers '
                               'resolved and reassigned away - both are '
                               'departures from this board.'),
                panel('Ageing bands',
                      charts.bar_chart(labels, bands, stacked=True,
                                       height=240),
                      'ordered magnitude, so one hue light to dark', 'half',
                      legend=[{'name': b['name'], 'colour': b['colour']}
                              for b in bands],
                      footnote='A flat total over a growing 30+ band is the '
                               'commonest way a backlog goes wrong without '
                               'the headline moving.'),
                panel('Average open by queue',
                      charts.hbar_chart(
                          [{'label': q['assignment_group'],
                            'value': float(q['avg_open'] or 0)}
                           for q in queues], label_w=150),
                      'mean daily count over the range', 'half'),
            ],
        }

    # ===================================================================
    # C. SLA and risk
    # ===================================================================

    def risk(self) -> Dict[str, Any]:
        clause, params = self.scope()

        bands = self.one(f"""
            SELECT SUM(sla_pct_consumed < 50) AS under50,
                   SUM(sla_pct_consumed >= 50 AND sla_pct_consumed < 75) AS to75,
                   SUM(sla_pct_consumed >= 75 AND sla_breached = 0) AS to100,
                   SUM(sla_breached = 1) AS breached,
                   SUM(sla_pct_consumed IS NULL) AS no_sla
              FROM v_current_board WHERE snoozed = 0 {clause}
        """, params)

        horizon = self.query(f"""
            SELECT CASE
                     WHEN projected_breach_at
                          <= DATE_ADD(UTC_TIMESTAMP(), INTERVAL 24 HOUR) THEN '24h'
                     WHEN projected_breach_at
                          <= DATE_ADD(UTC_TIMESTAMP(), INTERVAL 48 HOUR) THEN '48h'
                     ELSE '72h' END AS bucket, COUNT(*) AS n
              FROM v_current_board
             WHERE snoozed = 0 AND sla_breached = 0
               AND projected_breach_at IS NOT NULL
               AND projected_breach_at
                   <= DATE_ADD(UTC_TIMESTAMP(), INTERVAL 72 HOUR) {clause}
             GROUP BY bucket
        """, params)
        buckets = {r['bucket']: int(r['n'] or 0) for r in horizon}

        # Qualified via scope() rather than by string-replacing the clause:
        # the query is aliased, and patching generated SQL with str.replace
        # is the kind of thing that works until a column name is a substring
        # of another one.
        aliased, alias_params = self.scope('b.assignment_group')
        silent = self.one(f"""
            SELECT COUNT(*) AS n FROM v_current_board b
             WHERE b.sla_breached = 1 {aliased}
               AND NOT EXISTS (
                   SELECT 1 FROM alert_log a
                    WHERE a.incident_number = b.incident_number
                      AND a.alert_type IN ('SLA_AT_RISK', 'SLA_BREACHED'))
        """, alias_params)

        upcoming = self.query(f"""
            SELECT incident_number, short_description, assigned_to,
                   assignment_group, projected_breach_at, sla_pct_consumed
              FROM v_current_board
             WHERE snoozed = 0 AND sla_breached = 0
               AND projected_breach_at IS NOT NULL
               AND projected_breach_at
                   <= DATE_ADD(UTC_TIMESTAMP(), INTERVAL 72 HOUR) {clause}
             ORDER BY projected_breach_at LIMIT 15
        """, params)

        trend = self.daily('SUM(sla_breached) AS breached, '
                           'SUM(sla_at_risk) AS at_risk')
        silent_n = int(silent.get('n') or 0)

        return {
            'tiles': [
                tile('Breached', int(bands.get('breached') or 0),
                     'past the resolution target',
                     'critical' if bands.get('breached') else 'good'),
                tile('Breaching in 24h', buckets.get('24h', 0),
                     'the only ones still saveable today',
                     'critical' if buckets.get('24h') else 'good'),
                tile('Breaching in 48h', buckets.get('48h', 0),
                     'plan for tomorrow',
                     'warning' if buckets.get('48h') else 'good'),
                tile('Silent breaches', silent_n,
                     'breached with no alert ever fired',
                     'critical' if silent_n else 'good'),
            ],
            'panels': [
                panel('SLA runway',
                      charts.hbar_chart([
                          {'label': 'Under 50% used',
                           'value': int(bands.get('under50') or 0),
                           'colour': charts.STATUS['good']},
                          {'label': '50-75% used',
                           'value': int(bands.get('to75') or 0),
                           'colour': charts.STATUS['warning']},
                          {'label': '75-100% used',
                           'value': int(bands.get('to100') or 0),
                           'colour': charts.STATUS['serious']},
                          {'label': 'Breached',
                           'value': int(bands.get('breached') or 0),
                           'colour': charts.STATUS['critical']},
                          {'label': 'No SLA attached',
                           'value': int(bands.get('no_sla') or 0),
                           'colour': charts.SERIES[0]},
                      ], label_w=135),
                      'how much target is left across the backlog', 'half',
                      footnote='"No SLA attached" is not a safe bucket. It '
                               'means nothing is measuring those at all.'),
                panel('Breaches and near misses',
                      charts.line_chart(self.labels(trend), [
                          {'name': 'Breached',
                           'values': [int(r['breached'] or 0) for r in trend],
                           'colour': charts.STATUS['critical']},
                          {'name': 'At risk',
                           'values': [int(r['at_risk'] or 0) for r in trend],
                           'colour': charts.STATUS['warning']},
                      ], height=230),
                      'daily count carried in the backlog', 'half',
                      legend=[{'name': 'Breached',
                               'colour': charts.STATUS['critical']},
                              {'name': 'At risk',
                               'colour': charts.STATUS['warning']}]),
                table_panel(
                    'Projected to breach in the next 72 hours',
                    ['Incident', 'Summary', 'Queue', 'Assigned to',
                     'SLA used', 'Breaches at'],
                    [[r['incident_number'],
                      (r.get('short_description') or '')[:60],
                      r.get('assignment_group'),
                      r.get('assigned_to') or 'Unassigned',
                      show(round(float(r['sla_pct_consumed']), 1)
                           if r.get('sla_pct_consumed') is not None else None),
                      stamp(r.get('projected_breach_at'))]
                     for r in upcoming],
                    'the only view on this page you can still act on', 'full',
                    link=0,
                    empty='Nothing projected to breach in the next 72 hours.'),
            ],
        }

    # ===================================================================
    # D. People and workload
    # ===================================================================

    def people(self) -> Dict[str, Any]:
        clause, params = self.scope()
        agent_expr = ("COALESCE(NULLIF(TRIM(assigned_to), ''), "
                      "'(unassigned)')")

        agents = self.query(f"""
            SELECT {agent_expr} AS agent, COUNT(*) AS tickets,
                   MAX(attention_score) AS max_score,
                   ROUND(AVG(attention_score)) AS avg_score,
                   ROUND(AVG(idle_days), 1) AS avg_idle,
                   SUM(sla_breached) AS breached,
                   SUM(caller_replied_unanswered) AS caller_waiting,
                   SUM(age_days >= 30) AS over_30,
                   SUM(age_days < 10) AS b1,
                   SUM(age_days >= 10 AND age_days < 20) AS b2,
                   SUM(age_days >= 20 AND age_days < 30) AS b3,
                   SUM(age_days >= 30) AS b4
              FROM v_current_board WHERE snoozed = 0 {clause}
             GROUP BY agent ORDER BY tickets DESC LIMIT 20
        """, params)

        total = sum(int(a['tickets'] or 0) for a in agents)
        busiest = agents[0] if agents else {}
        concentration = pct(busiest.get('tickets'), total)
        unassigned = sum(int(a['tickets'] or 0) for a in agents
                         if a['agent'] == '(unassigned)')

        return {
            'tiles': [
                tile('People with aged work', len(agents), 'in your queues'),
                tile('Busiest', busiest.get('agent', charts.DASH),
                     f'{busiest.get("tickets", 0)} aged tickets'),
                tile('Held by one person', show(concentration),
                     'share carried by the busiest',
                     'serious' if (concentration or 0) > 35 else 'good'),
                tile('Unassigned', unassigned, 'nobody owns these',
                     'serious' if unassigned else 'good'),
            ],
            'panels': [
                panel('Workload',
                      charts.hbar_chart([
                          {'label': a['agent'],
                           'value': int(a['tickets'] or 0),
                           'colour': charts.STATUS['serious']
                                     if a['agent'] == '(unassigned)'
                                     else charts.SERIES[0]}
                          for a in agents[:12]], label_w=150),
                      'aged tickets held, worst first', 'half',
                      footnote='Allocation, not performance. Whoever was '
                               'handed the hard queue appears at the top, '
                               'which is why this is not a leaderboard.'),
                panel('Who holds the old work',
                      charts.heatmap(
                          [a['agent'] for a in agents[:10]],
                          ['5-10d', '10-20d', '20-30d', '30+d'],
                          [[int(a['b1'] or 0), int(a['b2'] or 0),
                            int(a['b3'] or 0), int(a['b4'] or 0)]
                           for a in agents[:10]], label_w=150),
                      'count by age band', 'half',
                      footnote='The right-hand column is the one that '
                               'matters: most tickets and oldest tickets are '
                               'different problems.'),
                table_panel(
                    'By person',
                    ['Agent', 'Aged', 'Highest score', 'Average score',
                     'Avg idle (days)', 'SLA breached', 'Caller waiting',
                     '30+ days'],
                    [[a['agent'], a['tickets'], a['max_score'],
                      a['avg_score'], a['avg_idle'], a['breached'],
                      a['caller_waiting'], a['over_30']] for a in agents],
                    'caller waiting is the one column about responsiveness '
                    'rather than allocation', 'full',
                    numeric=(1, 2, 3, 4, 5, 6, 7),
                    empty='Nobody holds aged work in your queues.'),
            ],
        }

    # ===================================================================
    # E. Recommendation quality
    # ===================================================================

    def quality(self) -> Dict[str, Any]:
        since, until = self.window.since, self.window.until

        overall = self.one(f"""
            SELECT COUNT(*) AS judged,
                   SUM(decision IN {AGREED}) AS agreed,
                   SUM(decision = 'ACCEPTED') AS accepted,
                   SUM(decision = 'MODIFIED') AS modified,
                   SUM(decision = 'REJECTED') AS rejected,
                   SUM(decision = 'NOT_APPLICABLE') AS not_applicable
              FROM recommendation_feedback
             WHERE created_at BETWEEN %s AND %s
        """, (since, until))

        produced = self.one("""
            SELECT COUNT(*) AS n FROM recommendation
             WHERE created_at BETWEEN %s AND %s AND superseded = 0
        """, (since, until))

        by_action = self.query(f"""
            SELECT r.recommended_action AS action, COUNT(*) AS judged,
                   SUM(f.decision IN {AGREED}) AS agreed
              FROM recommendation_feedback f
              JOIN recommendation r ON r.id = f.recommendation_id
             WHERE f.created_at BETWEEN %s AND %s
             GROUP BY r.recommended_action ORDER BY judged DESC
        """, (since, until))

        calibration = self.query(f"""
            SELECT FLOOR(r.confidence * 10) AS bucket, COUNT(*) AS judged,
                   SUM(f.decision IN {AGREED}) AS agreed
              FROM recommendation_feedback f
              JOIN recommendation r ON r.id = f.recommendation_id
             WHERE f.created_at BETWEEN %s AND %s AND r.confidence >= 0.5
             GROUP BY bucket ORDER BY bucket
        """, (since, until))

        trend = self.query(f"""
            SELECT DATE(created_at) AS day, COUNT(*) AS judged,
                   SUM(decision IN {AGREED}) AS agreed
              FROM recommendation_feedback
             WHERE created_at BETWEEN %s AND %s
             GROUP BY DATE(created_at) ORDER BY day
        """, (since, until))

        cost = self.one("""
            SELECT COUNT(*) AS calls, ROUND(AVG(latency_ms)) AS latency,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(model = 'rule-fallback') AS fallbacks
              FROM recommendation WHERE created_at BETWEEN %s AND %s
        """, (since, until))

        judged = int(overall.get('judged') or 0)
        agreement = pct(overall.get('agreed'), judged)
        coverage = pct(judged, produced.get('n'))

        bins, stated, actual = [], [], []
        thin = 0
        for row in calibration:
            if int(row['judged'] or 0) < 3:
                # Three judgements is not a calibration point. Plotted, it
                # becomes a 0% or 100% bar that dominates the panel.
                thin += 1
                continue
            bucket = int(row['bucket'] or 0)
            bins.append(f'{bucket * 10}-{bucket * 10 + 10}%')
            stated.append(bucket * 10 + 5)
            actual.append(pct(row['agreed'], row['judged']) or 0)

        tokens = (int(cost.get('prompt_tokens') or 0)
                  + int(cost.get('completion_tokens') or 0))

        calibration_note = (
            'The panel the rest of this page depends on. If stated '
            'confidence is not honest, every other figure here is built on '
            'it.')
        if thin:
            calibration_note += (f' {thin} bucket(s) with fewer than three '
                                 f'judgements are omitted rather than drawn.')

        return {
            'tiles': [
                tile('Lead agreement', show(agreement),
                     f'{judged} judged in this range', tone_at(agreement, 80),
                     spark=[pct(r['agreed'], r['judged']) or 0
                            for r in trend]),
                tile('Review coverage', show(coverage),
                     'of recommendations judged by anyone',
                     tone_at(coverage, 50)),
                tile('Model calls', int(cost.get('calls') or 0),
                     f'{int(cost.get("latency") or 0)} ms average'),
                tile('Tokens used', f'{tokens:,}',
                     'prompt and completion combined'),
                tile('Rule fallbacks', int(cost.get('fallbacks') or 0),
                     'model unreachable, advice degraded',
                     'serious' if cost.get('fallbacks') else 'good'),
            ],
            'panels': [
                panel('Agreement over time',
                      charts.line_chart(
                          self.labels(trend),
                          [{'name': 'Agreement',
                            'values': [pct(r['agreed'], r['judged']) or 0
                                       for r in trend]}],
                          suffix='%', target=80, target_label='target 80%',
                          height=230),
                      'did the lead do what was suggested', 'half',
                      footnote='Counts accepted and accepted-with-edits '
                               'together: an edit means the direction was '
                               'right.'),
                panel('Confidence calibration',
                      charts.calibration_chart(bins, stated, actual,
                                               height=230),
                      'stated confidence against what happened', 'half',
                      footnote=calibration_note),
                panel('Precision by suggested step',
                      charts.hbar_chart([
                          {'label': ACTION_LABELS.get(r['action'],
                                                      r['action']),
                           'value': pct(r['agreed'], r['judged']) or 0,
                           'note': f'{r["judged"]} judged',
                           'colour': charts.STATUS['critical']
                                     if (pct(r['agreed'], r['judged']) or 0) < 70
                                     else charts.SERIES[0]}
                          for r in by_action], suffix='%', max_value=100,
                          label_w=175),
                      'which advice is good', 'half',
                      footnote='Hidden in the headline. One weak action can '
                               'sit behind a healthy overall figure.'),
                table_panel(
                    'Decisions recorded',
                    ['Outcome', 'Count', 'Share'],
                    [['Accepted as suggested', overall.get('accepted') or 0,
                      show(pct(overall.get('accepted'), judged))],
                     ['Accepted with edits', overall.get('modified') or 0,
                      show(pct(overall.get('modified'), judged))],
                     ['Rejected', overall.get('rejected') or 0,
                      show(pct(overall.get('rejected'), judged))],
                     ['Not applicable', overall.get('not_applicable') or 0,
                      show(pct(overall.get('not_applicable'), judged))]]
                    if judged else [],
                    'the raw split behind the agreement figure', 'half',
                    numeric=(1, 2),
                    empty='Nobody has recorded a verdict in this range, so '
                          'there is no agreement figure to report.'),
            ],
        }

    # ===================================================================
    # F. Knowledge base
    # ===================================================================

    def knowledge(self) -> Dict[str, Any]:
        since, until = self.window.since, self.window.until

        usage = self.one("""
            SELECT COUNT(DISTINCT k.recommendation_id) AS with_offer,
                   COUNT(DISTINCT CASE WHEN k.cited = 1
                                       THEN k.recommendation_id END) AS with_cite,
                   SUM(k.offered = 1) AS offered, SUM(k.cited = 1) AS cited,
                   SUM(k.cited = 1 AND k.offered = 0) AS invented,
                   SUM(k.source = 'text_search') AS fallback
              FROM recommendation_kb k
              JOIN recommendation r ON r.id = k.recommendation_id
             WHERE r.created_at BETWEEN %s AND %s
        """, (since, until))

        rated = self.one("""
            SELECT COUNT(*) AS rated, SUM(verdict = 'RELEVANT') AS relevant,
                   SUM(verdict IN ('IRRELEVANT', 'OUTDATED')) AS wrong
              FROM recommendation_kb
             WHERE cited = 1 AND verdict <> 'UNRATED'
               AND rated_at BETWEEN %s AND %s
        """, (since, until))

        produced = self.one("""
            SELECT COUNT(*) AS n FROM recommendation
             WHERE created_at BETWEEN %s AND %s AND superseded = 0
        """, (since, until))

        top = self.query("""
            SELECT k.kb_number, COUNT(*) AS offers, SUM(k.cited = 1) AS cites,
                   SUM(k.verdict = 'RELEVANT') AS good,
                   SUM(k.verdict IN ('IRRELEVANT', 'OUTDATED')) AS bad
              FROM recommendation_kb k
              JOIN recommendation r ON r.id = k.recommendation_id
             WHERE r.created_at BETWEEN %s AND %s
             GROUP BY k.kb_number ORDER BY cites DESC, offers DESC LIMIT 12
        """, (since, until))

        clause, params = self.scope()
        gaps = self.query(f"""
            SELECT COALESCE(NULLIF(category, ''), '(uncategorised)') AS category,
                   COUNT(*) AS aged, SUM(kb_available = 1) AS has_article,
                   SUM(kb_attached = 1) AS attached
              FROM v_current_board WHERE snoozed = 0 {clause}
             GROUP BY category HAVING aged >= 2
             ORDER BY (COUNT(*) - SUM(kb_available = 1)) DESC, aged DESC
             LIMIT 12
        """, params)

        reach = pct(usage.get('with_cite'), produced.get('n'))
        use = pct(usage.get('cited'), usage.get('offered'))
        relevance = pct(rated.get('relevant'), rated.get('rated'))

        return {
            'tiles': [
                tile('Reach', show(reach),
                     f'{int(usage.get("with_cite") or 0)} of '
                     f'{int(produced.get("n") or 0)} rested on an article',
                     tone_at(reach, 40)),
                tile('Retrieval used', show(use),
                     f'{int(usage.get("cited") or 0)} of '
                     f'{int(usage.get("offered") or 0)} earned their place',
                     tone_at(use, 33)),
                tile('Rated relevant', show(relevance),
                     f'{int(rated.get("rated") or 0)} judged by a lead'
                     if rated.get('rated') else 'nobody has rated one yet',
                     tone_at(relevance, 80)),
                tile('Fabricated citations', int(usage.get('invented') or 0),
                     'cited an article never supplied',
                     'critical' if usage.get('invented') else 'good'),
                tile('Keyword fallback', int(usage.get('fallback') or 0),
                     'index empty or stale when these ran',
                     'serious' if usage.get('fallback') else 'good'),
            ],
            'panels': [
                panel('The three questions',
                      charts.hbar_chart([
                          {'label': 'Reach', 'value': reach or 0,
                           'colour': charts.STATUS[
                               'good' if (reach or 0) >= 40 else 'serious']},
                          {'label': 'Retrieval used', 'value': use or 0,
                           'colour': charts.STATUS[
                               'good' if (use or 0) >= 33 else 'serious']},
                          {'label': 'Rated relevant', 'value': relevance or 0,
                           'colour': charts.STATUS[
                               'good' if (relevance or 0) >= 80
                               else 'serious']},
                      ], suffix='%', max_value=100, label_w=135),
                      'never rolled into one number', 'half',
                      footnote='High relevance on a tiny reach reads as '
                               'success and is a knowledge gap: the articles '
                               'it cites are right and it almost never finds '
                               'one.'),
                table_panel(
                    'Knowledge gaps - where to write next',
                    ['Category', 'Aged tickets', 'Have an article',
                     'Article attached'],
                    [[g['category'], g['aged'], g['has_article'],
                      g['attached']] for g in gaps],
                    'high volume with no article is a commission', 'half',
                    numeric=(1, 2, 3),
                    empty='No category has two or more aged tickets.'),
                table_panel(
                    'Most-used articles',
                    ['Article', 'Offered', 'Used', 'Rated right',
                     'Rated wrong'],
                    [[t['kb_number'], t['offers'], t['cites'], t['good'] or 0,
                      t['bad'] or 0] for t in top],
                    'both halves of the question', 'full',
                    numeric=(1, 2, 3, 4),
                    footnote='Offered often and used rarely is a retrieval '
                             'problem - it matches the wording and not the '
                             'fault. Used often and rated wrong is a content '
                             'problem.',
                    empty='No articles were retrieved in this range.'),
            ],
        }

    # ===================================================================
    # G. Service health
    # ===================================================================

    def health(self) -> Dict[str, Any]:
        since, until = self.window.since, self.window.until

        stale_mins = None
        try:
            watermark = self.db.get_sync_watermark('incident_delta')
            if watermark:
                stale_mins = int((utc_now() - watermark).total_seconds() // 60)
        except Exception:
            log.debug('No sync watermark available')

        mail = self.query("""
            SELECT run_type, status, COUNT(*) AS n,
                   SUM(ticket_count) AS tickets
              FROM digest_run WHERE sent_at BETWEEN %s AND %s
             GROUP BY run_type, status ORDER BY n DESC
        """, (since, until))

        alerts = self.query("""
            SELECT DATE(fired_at) AS day, COUNT(*) AS n
              FROM alert_log WHERE fired_at BETWEEN %s AND %s
             GROUP BY DATE(fired_at) ORDER BY day
        """, (since, until))

        adoption = self.query("""
            SELECT user_name, COUNT(*) AS verdicts,
                   MAX(created_at) AS last_seen
              FROM recommendation_feedback
             WHERE created_at BETWEEN %s AND %s
             GROUP BY user_name ORDER BY verdicts DESC LIMIT 12
        """, (since, until))

        accounts = self.one("""
            SELECT COUNT(*) AS total, SUM(role = 'ADMIN') AS admins,
                   SUM(role <> 'ADMIN' AND (assignment_groups IS NULL
                       OR TRIM(assignment_groups) = '')) AS stranded
              FROM advisor_user WHERE active = 1
        """)

        failures = sum(int(m['n'] or 0) for m in mail
                       if m['status'] == 'FAILED')
        suppressed = sum(int(m['n'] or 0) for m in mail
                         if m['status'] == 'SUPPRESSED')

        return {
            'tiles': [
                tile('Last sync',
                     f'{stale_mins} min ago' if stale_mins is not None
                     else charts.DASH,
                     'everything else is wrong if this is',
                     'critical' if (stale_mins or 0) > 60 else 'good'),
                tile('Mail failures', failures, 'in this range',
                     'critical' if failures else 'good'),
                tile('Suppressed', suppressed,
                     'shadow mode or mail disabled',
                     'warning' if suppressed else 'good'),
                tile('Active accounts', int(accounts.get('total') or 0),
                     f'{int(accounts.get("admins") or 0)} administrators'),
                tile('Accounts with no queue',
                     int(accounts.get('stranded') or 0),
                     'they see an empty board and get no mail',
                     'serious' if accounts.get('stranded') else 'good'),
            ],
            'panels': [
                panel('Alert volume',
                      charts.line_chart(
                          self.labels(alerts),
                          [{'name': 'Alerts fired',
                            'values': [int(a['n'] or 0) for a in alerts],
                            'area': True}], height=230),
                      'one per ticket per type per day', 'half',
                      footnote='A rising line against flat agreement means '
                               'alerting is becoming noise people filter '
                               'to a folder.'),
                table_panel(
                    'Mail delivery',
                    ['Message', 'Status', 'Count', 'Tickets'],
                    [[m['run_type'], m['status'], m['n'], m['tickets'] or 0]
                     for m in mail],
                    'suppressed is shadow mode working, not a failure',
                    'half', numeric=(2, 3),
                    empty='No mail attempted in this range.'),
                table_panel(
                    'Who is using it',
                    ['Person', 'Verdicts recorded', 'Last recorded'],
                    [[a['user_name'], a['verdicts'], stamp(a['last_seen'])]
                     for a in adoption],
                    'the leading indicator for the whole programme', 'full',
                    numeric=(1,),
                    footnote='If verdicts stop, the accuracy figure decays '
                             'and nothing else says so.',
                    empty='Nobody has recorded a verdict in this range. The '
                          'quality page cannot mean anything until they do.'),
            ],
        }

    # ===================================================================
    # H. Executive summary
    # ===================================================================

    def summary(self) -> Dict[str, Any]:
        rows = self.daily(
            'SUM(tickets) AS tickets, SUM(sla_breached) AS breached, '
            'SUM(band_30_plus) AS over30, SUM(newly_aged) AS newly, '
            'SUM(retired) AS retired')

        if not rows:
            return self._awaiting_rollup()

        since, until = self.window.since, self.window.until
        judged = self.one(f"""
            SELECT COUNT(*) AS judged, SUM(decision IN {AGREED}) AS agreed
              FROM recommendation_feedback
             WHERE created_at BETWEEN %s AND %s
        """, (since, until))

        first, last = rows[0], rows[-1]
        opened = sum(int(r['newly'] or 0) for r in rows)
        closed = sum(int(r['retired'] or 0) for r in rows)
        change = int(last['tickets'] or 0) - int(first['tickets'] or 0)
        agreement = pct(judged.get('agreed'), judged.get('judged'))
        labels = self.labels(rows)

        return {
            'tiles': [
                tile('Aged backlog', int(last['tickets'] or 0),
                     f'{abs(change)} {"fewer" if change <= 0 else "more"} '
                     f'than {self.window.days} days ago',
                     'good' if change <= 0 else 'serious',
                     spark=[int(r['tickets'] or 0) for r in rows]),
                tile('Tickets retired', closed,
                     f'against {opened} newly aged',
                     'good' if closed >= opened else 'serious'),
                tile('Oldest band', int(last['over30'] or 0),
                     'open more than 30 days',
                     'critical' if int(last['over30'] or 0)
                     > int(first['over30'] or 0) else 'good'),
                tile('Advice followed', show(agreement),
                     f'{int(judged.get("judged") or 0)} decisions recorded',
                     tone_at(agreement, 80)),
            ],
            'panels': [
                panel('Aged backlog over the period',
                      charts.line_chart(
                          labels,
                          [{'name': 'Aged tickets',
                            'values': [int(r['tickets'] or 0) for r in rows],
                            'area': True}], height=250),
                      f'{self.window.label.lower()}, your queues', 'full'),
                panel('SLA breaches carried',
                      charts.line_chart(
                          labels,
                          [{'name': 'Breached',
                            'values': [int(r['breached'] or 0) for r in rows],
                            'colour': charts.STATUS['critical'],
                            'area': True}], height=230),
                      'count in the backlog, not arrivals', 'half'),
                panel('Flow',
                      charts.bar_chart(labels, [
                          {'name': 'Newly aged',
                           'values': [int(r['newly'] or 0) for r in rows],
                           'colour': charts.SERIES[1]},
                          {'name': 'Retired',
                           'values': [int(r['retired'] or 0) for r in rows],
                           'colour': charts.SERIES[0]}], height=230),
                      'arrivals against departures', 'half',
                      legend=[{'name': 'Newly aged',
                               'colour': charts.SERIES[1]},
                              {'name': 'Retired',
                               'colour': charts.SERIES[0]}]),
            ],
            'caveat':
                'These figures describe what happened alongside this service, '
                'not what it caused. There is no counterfactual in the data - '
                'no record of what the backlog would have done without it - '
                'so nothing here is presented as a saving.',
        }

    # -- shared --------------------------------------------------------------

    def _awaiting_rollup(self) -> Dict[str, Any]:
        """Said in words rather than drawn as a flat line at zero."""
        return {
            'tiles': [],
            'panels': [panel(
                'Not enough history yet',
                charts.empty_chart('No rollup data for this range.', 640, 190),
                '', 'full',
                footnote='The nightly maintenance job builds the daily '
                         'rollup these trends read from. On a new deployment '
                         'the first rows appear after the first overnight '
                         'run; check that migration 009 has been applied.')],
        }
