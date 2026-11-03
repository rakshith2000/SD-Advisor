"""Daily rollup of ticket_signal, for the insight pages.

One row per queue per day, built from the last signal each ticket had that
day. See db/migrations/009_insights_rollup.sql for why this exists rather
than the pages querying the raw table directly.

Rebuilt rather than appended. Each nightly run recomputes a trailing window
and upserts, so a day that was built while the sync was behind gets corrected
on the next run instead of being wrong permanently. That costs a few seconds;
not doing it costs a chart nobody can explain six weeks later.
"""

import datetime
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from core.timeutil import utc_now

log = get_logger('pipeline.rollup')

# How far back each nightly run recomputes. Long enough to repair a day built
# from an incomplete sync, short enough to stay quick.
DEFAULT_WINDOW_DAYS = 7

# Backfill ceiling. ticket_signal is pruned at 180 days, so asking for more
# produces empty rows that read as a quiet period rather than as an absence
# of data - the more misleading of the two.
MAX_BACKFILL_DAYS = 180


def percentile(values: List[float], fraction: float) -> Optional[float]:
    """Nearest-rank percentile.

    No numpy: this sorts a handful of values per queue per day, and importing
    an array library to order twelve numbers is not a trade worth making.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1,
                       int(round(fraction * (len(ordered) - 1)))))
    return round(float(ordered[index]), 2)


class DailyRollup:
    def __init__(self, context):
        self.ctx = context
        self.db = context.db

    # -- public ------------------------------------------------------------

    def run(self, days: int = DEFAULT_WINDOW_DAYS,
            end: Optional[datetime.date] = None) -> Dict[str, Any]:
        """Recompute the trailing `days` days, inclusive of today."""
        days = max(1, min(int(days), MAX_BACKFILL_DAYS))
        last = end or utc_now().date()
        first = last - datetime.timedelta(days=days - 1)

        written = 0
        for offset in range(days):
            written += self.build_day(first + datetime.timedelta(days=offset))

        log.info('Rollup: %d queue-days written for %s..%s',
                 written, first, last)
        return {'from': str(first), 'to': str(last), 'rows': written}

    def build_day(self, day: datetime.date) -> int:
        rows = self._last_signal_of_day(day)
        if not rows:
            return 0

        flow = self._flow(day)
        by_group: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            group = (row.get('assignment_group') or '').strip() or '(none)'
            by_group.setdefault(group, []).append(row)

        now = utc_now()
        written = 0
        for group, items in by_group.items():
            record = self._aggregate(day, group, items)
            record.update(flow.get(group, {'newly_aged': 0, 'retired': 0}))
            record['computed_at'] = now
            self.db.upsert('signal_daily', record, update_columns=[
                k for k in record if k not in ('day', 'assignment_group')])
            written += 1
        return written

    # -- internals ----------------------------------------------------------

    def _last_signal_of_day(self, day: datetime.date) -> List[Dict[str, Any]]:
        """One row per ticket: the final signal it carried that day."""
        return self.db.query("""
            SELECT s.incident_number, w.assignment_group, w.assigned_to,
                   s.age_days, s.idle_days, s.attention_score,
                   s.pending_action_owner, s.sla_breached, s.sla_pct_consumed,
                   s.caller_replied_unanswered, s.auto_close_candidate,
                   s.dependency_resolved, s.kb_available, s.kb_attached,
                   s.risk_flags
              FROM ticket_signal s
              JOIN watched_ticket w ON w.incident_number = s.incident_number
             WHERE DATE(s.computed_at) = %s
               AND s.id = (SELECT MAX(s2.id) FROM ticket_signal s2
                            WHERE s2.incident_number = s.incident_number
                              AND DATE(s2.computed_at) = %s)
        """, (day, day))

    def _flow(self, day: datetime.date) -> Dict[str, Dict[str, int]]:
        """Arrivals and departures, by queue.

        An arrival is the first day a ticket appears in ticket_signal at all.
        A departure is counted on the day after its last appearance, because
        the last day is one it was still present for. "Retired" therefore
        covers resolved and reassigned-away alike.
        """
        yesterday = day - datetime.timedelta(days=1)
        rows = self.db.query("""
            SELECT w.assignment_group,
                   SUM(b.first_day = %s) AS newly_aged,
                   SUM(b.last_day  = %s) AS departed
              FROM (SELECT incident_number,
                           DATE(MIN(computed_at)) AS first_day,
                           DATE(MAX(computed_at)) AS last_day
                      FROM ticket_signal
                     GROUP BY incident_number) b
              JOIN watched_ticket w ON w.incident_number = b.incident_number
             WHERE b.first_day = %s OR b.last_day = %s
             GROUP BY w.assignment_group
        """, (day, yesterday, day, yesterday))

        return {
            ((r['assignment_group'] or '').strip() or '(none)'): {
                'newly_aged': int(r['newly_aged'] or 0),
                'retired': int(r['departed'] or 0),
            } for r in rows}

    @staticmethod
    def _aggregate(day: datetime.date, group: str,
                   items: List[Dict[str, Any]]) -> Dict[str, Any]:
        ages = [float(i['age_days'] or 0) for i in items]
        idles = [float(i['idle_days'] or 0) for i in items]
        scores = [int(i['attention_score'] or 0) for i in items]

        def flagged(name: str) -> int:
            """risk_flags is JSON - a list from some drivers, a string from
            others. Both shapes are handled rather than assumed."""
            total = 0
            for item in items:
                raw = item.get('risk_flags')
                if isinstance(raw, list):
                    total += 1 if name in raw else 0
                elif isinstance(raw, str):
                    total += 1 if f'"{name}"' in raw else 0
            return total

        return {
            'day': day,
            'assignment_group': group[:150],
            'tickets': len(items),
            'team_owned': sum(
                1 for i in items
                if i.get('pending_action_owner') == 'SERVICE_DESK'),
            'sla_breached': sum(1 for i in items if i.get('sla_breached')),
            'sla_at_risk': flagged('SLA_AT_RISK'),
            'prolonged_inactivity': flagged('PROLONGED_INACTIVITY'),
            'caller_awaiting': sum(
                1 for i in items if i.get('caller_replied_unanswered')),
            'closure_candidates': sum(
                1 for i in items if i.get('auto_close_candidate')),
            'dependency_cleared': sum(
                1 for i in items if i.get('dependency_resolved')),
            'unassigned': sum(
                1 for i in items if not (i.get('assigned_to') or '').strip()),
            # A gap is an article being available and not attached. "No
            # article exists" is a different finding and belongs on the
            # knowledge page, not here.
            'kb_gap': sum(1 for i in items
                          if i.get('kb_available') and not i.get('kb_attached')),

            'band_5_10': sum(1 for a in ages if a < 10),
            'band_10_20': sum(1 for a in ages if 10 <= a < 20),
            'band_20_30': sum(1 for a in ages if 20 <= a < 30),
            'band_30_plus': sum(1 for a in ages if a >= 30),

            'median_age_days': percentile(ages, 0.5),
            'p90_age_days': percentile(ages, 0.9),
            'median_idle_days': percentile(idles, 0.5),
            'avg_score': round(sum(scores) / len(scores), 2) if scores else None,
            'max_score': max(scores) if scores else None,
        }
