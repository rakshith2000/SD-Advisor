"""Digest assembly.

Builds the per-lead and per-agent views that go out by email and render in the
UI. Three ideas drive the shape of this:

  * Rank, do not list. The board is sorted by attention score so the top five
    rows are the whole job on a normal day.
  * Show the delta. A lead who saw the same 40 rows yesterday needs to know
    which 6 are new or newly worse, so movement is computed explicitly.
  * Group by who is blocking. "9 of these are actually ours" is the sentence
    that saves the time.
"""

import datetime
import json
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from pipeline.scoring import top_reasons

log = get_logger('delivery.digest')

ACTION_LABELS = {
    'RESOLVE': 'Resolve now',
    'FOLLOW_UP_CALLER': 'Follow up with caller',
    'CHASE_VENDOR': 'Chase vendor',
    'REASSIGN': 'Reassign',
    'ESCALATE': 'Escalate',
    'AWAIT_DEPENDENCY': 'Waiting on dependency',
    'CLOSE_STALE': 'Close (no response)',
    'NO_ACTION_NEEDED': 'On track',
}

FLAG_LABELS = {
    'SLA_BREACHED': 'SLA breached',
    'SLA_JEOPARDY': 'SLA at risk',
    'VENDOR_SLA_BREACHED': 'Vendor SLA breached',
    'CRITICALLY_STALE': 'No action 4+ days',
    'STALE': 'No recent action',
    'CALLER_AWAITING_REPLY': 'Caller awaiting reply',
    'DEPENDENCY_CLEARED': 'Blocker already closed',
    'AUTO_CLOSE_CANDIDATE': 'Closure candidate',
    'PAST_EXPECTED_DURATION': 'Past expected time',
    'KB_NOT_ATTACHED': 'KB not attached',
    'NEVER_TOUCHED': 'Never actioned',
    'INJECTION_SUSPECTED': 'Suspicious content',
    'UNKNOWN_TARGET_GROUP': 'Unverified target group',
}

BALL_LABELS = {
    'AGENT': 'Us',
    'CALLER': 'Caller',
    'VENDOR': 'Vendor',
    'CHANGE': 'Change',
    'PROBLEM': 'Problem',
    'APPROVAL': 'Approval',
    'NONE': '-',
}


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return value
    if isinstance(value, (str, bytes)):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


class DigestBuilder:
    def __init__(self, context):
        self.ctx = context
        self.db = context.db
        self.settings = context.settings
        self.base_url = str(context.settings.get('web.base_url', '')).rstrip('/')

    # -- data --------------------------------------------------------------

    def board_rows(self, assignment_groups: Optional[List[str]] = None,
                   agent: Optional[str] = None,
                   include_snoozed: bool = False,
                   min_score: int = 0) -> List[Dict[str, Any]]:
        sql = 'SELECT * FROM v_current_board WHERE 1 = 1'
        params: List[Any] = []

        if not include_snoozed:
            sql += ' AND snoozed = 0'
        if min_score:
            sql += ' AND attention_score >= %s'
            params.append(min_score)
        if assignment_groups:
            sql += ' AND assignment_group IN (' + ', '.join(['%s'] * len(assignment_groups)) + ')'
            params.extend(assignment_groups)
        if agent:
            sql += ' AND assigned_to = %s'
            params.append(agent)

        sql += ' ORDER BY attention_score DESC, age_days DESC'

        rows = self.db.query(sql, params)
        return [self.decorate(row) for row in rows]

    def decorate(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Add display-ready fields. Kept out of SQL so the UI and the email
        render identically from one code path."""
        item = dict(row)

        flags = _as_list(row.get('risk_flags'))
        item['risk_flags'] = flags
        item['flag_labels'] = [FLAG_LABELS.get(f, f.replace('_', ' ').title()) for f in flags]

        action = row.get('recommended_action') or ''
        item['action_label'] = ACTION_LABELS.get(action, action.replace('_', ' ').title() or 'Not analysed')
        item['ball_label'] = BALL_LABELS.get(row.get('ball_in_court') or '', row.get('ball_in_court') or '-')

        item['severity'] = self._severity(row.get('attention_score') or 0)
        item['confidence_pct'] = int(round(float(row.get('confidence') or 0) * 100))
        item['low_confidence'] = bool(row.get('confidence') is not None
                                      and float(row.get('confidence')) < 0.5)

        item['age_days_display'] = f"{float(row.get('age_days') or 0):.0f}"
        item['idle_days_display'] = f"{float(row.get('idle_days') or 0):.1f}"

        item['url'] = f"{self.base_url}/ticket/{row['incident_number']}" if self.base_url else ''
        return item

    @staticmethod
    def _severity(score: Any) -> str:
        value = int(score or 0)
        if value >= 70:
            return 'critical'
        if value >= 45:
            return 'high'
        if value >= 25:
            return 'medium'
        return 'low'

    # -- movement ----------------------------------------------------------

    def movement_since(self, rows: List[Dict[str, Any]],
                       hours: int = 24) -> Dict[str, List[Dict[str, Any]]]:
        """Split the board into new / worsening / unchanged.

        Leads should be able to read only the first two buckets.
        """
        since = datetime.datetime.now() - datetime.timedelta(hours=hours)
        numbers = [r['incident_number'] for r in rows]
        if not numbers:
            return {'new': [], 'worsening': [], 'steady': []}

        placeholders = ', '.join(['%s'] * len(numbers))
        prior = self.db.query(f"""
            SELECT s.incident_number, MAX(s.attention_score) AS prior_score
              FROM ticket_signal s
             WHERE s.incident_number IN ({placeholders})
               AND s.computed_at < %s
             GROUP BY s.incident_number
        """, numbers + [since])

        prior_scores = {r['incident_number']: int(r['prior_score'] or 0) for r in prior}

        buckets: Dict[str, List[Dict[str, Any]]] = {'new': [], 'worsening': [], 'steady': []}

        for row in rows:
            number = row['incident_number']
            current = int(row.get('attention_score') or 0)

            if number not in prior_scores:
                row['delta'] = None
                buckets['new'].append(row)
                continue

            delta = current - prior_scores[number]
            row['delta'] = delta
            if delta >= 10:
                buckets['worsening'].append(row)
            else:
                buckets['steady'].append(row)

        return buckets

    # -- aggregation -------------------------------------------------------

    def summarise(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        by_ball: Dict[str, int] = {}
        by_action: Dict[str, int] = {}

        for row in rows:
            by_ball[row.get('ball_label', '-')] = by_ball.get(row.get('ball_label', '-'), 0) + 1
            label = row.get('action_label', 'Not analysed')
            by_action[label] = by_action.get(label, 0) + 1

        return {
            'total': len(rows),
            'ours': sum(1 for r in rows if r.get('ball_in_court') == 'AGENT'),
            'sla_breached': sum(1 for r in rows if r.get('sla_breached')),
            'sla_at_risk': sum(1 for r in rows if 'SLA_JEOPARDY' in (r.get('risk_flags') or [])),
            'critically_stale': sum(1 for r in rows
                                    if 'CRITICALLY_STALE' in (r.get('risk_flags') or [])),
            'caller_awaiting': sum(1 for r in rows if r.get('caller_replied_unanswered')),
            'dependency_cleared': sum(1 for r in rows if r.get('dependency_resolved')),
            'close_candidates': sum(1 for r in rows if r.get('auto_close_candidate')),
            'needs_review': sum(1 for r in rows if r.get('low_confidence')),
            'by_ball': by_ball,
            'by_action': by_action,
        }

    def by_agent(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Per-agent rollup, sorted worst first. Feeds the coaching view."""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            agent = (row.get('assigned_to') or '(unassigned)').strip() or '(unassigned)'
            grouped.setdefault(agent, []).append(row)

        rollup = []
        for agent, tickets in grouped.items():
            scores = [int(t.get('attention_score') or 0) for t in tickets]
            idles = [float(t.get('idle_days') or 0) for t in tickets]
            rollup.append({
                'agent': agent,
                'ticket_count': len(tickets),
                'max_score': max(scores) if scores else 0,
                'avg_score': int(round(sum(scores) / len(scores))) if scores else 0,
                'avg_idle_days': round(sum(idles) / len(idles), 1) if idles else 0.0,
                'sla_breached': sum(1 for t in tickets if t.get('sla_breached')),
                'critically_stale': sum(1 for t in tickets
                                        if 'CRITICALLY_STALE' in (t.get('risk_flags') or [])),
                'caller_awaiting': sum(1 for t in tickets if t.get('caller_replied_unanswered')),
                'kb_gaps': sum(1 for t in tickets
                               if 'KB_NOT_ATTACHED' in (t.get('risk_flags') or [])),
                'tickets': sorted(tickets, key=lambda t: -(t.get('attention_score') or 0)),
            })

        return sorted(rollup, key=lambda r: (-r['max_score'], -r['ticket_count']))

    # -- coaching join with the existing audit tool ------------------------

    def compliance_context(self, agents: List[str]) -> Dict[str, Dict[str, Any]]:
        """Pull each agent's recent compliance average from the audit database.

        Read-only and entirely optional: if the audit DB is not configured the
        digest simply omits the column.
        """
        audit_db = self.ctx.audit_db
        if audit_db is None or not agents:
            return {}

        placeholders = ', '.join(['%s'] * len(agents))
        try:
            rows = audit_db.query(f"""
                SELECT m.resolved_by                AS agent,
                       ROUND(AVG(c.compliance_score)) AS avg_score,
                       COUNT(DISTINCT m.incident_number) AS audited
                  FROM incident_master_table m
                  JOIN incident_compliance_table c
                    ON c.incident_number = m.incident_number
                   AND c.customer_id     = m.customer_id
                 WHERE m.resolved_by IN ({placeholders})
                   AND m.resolved_on >= DATE_SUB(NOW(), INTERVAL 30 DAY)
                   AND c.compliance_score >= 0
                 GROUP BY m.resolved_by
            """, agents)
        except Exception:
            log.debug('Compliance context unavailable')
            return {}

        return {r['agent']: {'avg_score': int(r['avg_score'] or 0),
                             'audited': int(r['audited'] or 0)} for r in rows}

    # -- top-level build ---------------------------------------------------

    def build_lead_digest(self, assignment_groups: Optional[List[str]] = None,
                          max_tickets: int = 25) -> Dict[str, Any]:
        rows = self.board_rows(assignment_groups=assignment_groups)
        buckets = self.movement_since(rows)
        agents = self.by_agent(rows)

        compliance = self.compliance_context([a['agent'] for a in agents][:40])
        for entry in agents:
            entry['compliance'] = compliance.get(entry['agent'])

        priority_rows = rows[:max_tickets]

        return {
            'generated_at': datetime.datetime.now(),
            'groups': assignment_groups or self.settings.assignment_groups,
            'summary': self.summarise(rows),
            'movement': {
                'new': buckets['new'][:10],
                'worsening': buckets['worsening'][:10],
                'new_count': len(buckets['new']),
                'worsening_count': len(buckets['worsening']),
            },
            'tickets': priority_rows,
            'truncated': max(0, len(rows) - len(priority_rows)),
            'agents': agents,
            'close_candidates': [r for r in rows if r.get('auto_close_candidate')][:10],
            'base_url': self.base_url,
            'shadow_mode': self.settings.shadow_mode,
            'reasons': {r['incident_number']: self._reasons_for(r) for r in priority_rows},
        }

    def build_agent_digest(self, agent: str, max_tickets: int = 15) -> Dict[str, Any]:
        rows = self.board_rows(agent=agent)
        return {
            'generated_at': datetime.datetime.now(),
            'agent': agent,
            'summary': self.summarise(rows),
            'tickets': rows[:max_tickets],
            'truncated': max(0, len(rows) - max_tickets),
            'base_url': self.base_url,
            'shadow_mode': self.settings.shadow_mode,
            'reasons': {r['incident_number']: self._reasons_for(r) for r in rows[:max_tickets]},
        }

    def _reasons_for(self, row: Dict[str, Any]) -> List[str]:
        breakdown = self.db.query_one(
            'SELECT score_breakdown FROM ticket_signal '
            ' WHERE incident_number = %s ORDER BY id DESC LIMIT 1',
            (row['incident_number'],))
        if not breakdown or not breakdown.get('score_breakdown'):
            return []
        raw = breakdown['score_breakdown']
        if isinstance(raw, (str, bytes)):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                return []
        return top_reasons(raw or {})
