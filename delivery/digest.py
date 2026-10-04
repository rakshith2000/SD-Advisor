"""Digest assembly.

Builds the per-lead and per-agent views that go out by email and render in the
UI. Three ideas drive the shape of this:

  * Rank, do not list. The board is sorted by attention score so the top five
    rows are the whole job on a normal day.
  * Show the delta. A lead who saw the same 40 rows yesterday needs to know
    which 6 are new or newly worse, so movement is computed explicitly.
  * Group by pending action owner. "9 of these require Service Desk action"
    is the sentence that saves a lead the most time.
"""

import datetime
import json
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from pipeline.scoring import top_reasons
from pipeline.signals import SERVICE_DESK
from core.timeutil import format_duration, format_duration_days, utc_now

log = get_logger('delivery.digest')

ACTION_LABELS = {
    'RESOLVE': 'Resolve',
    'FOLLOW_UP_CALLER': 'Follow up with caller',
    'CHASE_VENDOR': 'Escalate to vendor',
    'REASSIGN': 'Reassign',
    'ESCALATE': 'Escalate',
    'AWAIT_DEPENDENCY': 'Awaiting dependency',
    'CLOSE_STALE': 'Close - no caller response',
    'NO_ACTION_NEEDED': 'No action required',
}

FLAG_LABELS = {
    'SLA_BREACHED': 'SLA breached',
    'SLA_AT_RISK': 'SLA at risk',
    'VENDOR_SLA_BREACHED': 'Vendor SLA breached',
    'PROLONGED_INACTIVITY': 'Prolonged inactivity',
    'NO_RECENT_ACTIVITY': 'No recent activity',
    'CALLER_AWAITING_REPLY': 'Caller awaiting response',
    'DEPENDENCY_CLEARED': 'Dependency already closed',
    'CLOSURE_CANDIDATE': 'Closure candidate',
    'EXPECTED_DURATION_EXCEEDED': 'Expected duration exceeded',
    'KB_ARTICLE_NOT_LINKED': 'Knowledge article not linked',
    'NO_ACTION_RECORDED': 'No action recorded',
    'INJECTION_SUSPECTED': 'Content requires review',
    'UNVERIFIED_TARGET_GROUP': 'Unverified target group',
}

# Values of pending_action_owner, as presented to leads and agents.
PENDING_ACTION_OWNER_LABELS = {
    'SERVICE_DESK': 'Service Desk',
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
            # "(unassigned)" is rendered where the underlying column is NULL or an
            # empty string; treat that label as a request for all such rows rather
            # than looking for the literal string.
            if agent == '(unassigned)':
                sql += ' AND (assigned_to IS NULL OR TRIM(assigned_to) = "")'
            else:
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
        item['pending_action_owner_label'] = PENDING_ACTION_OWNER_LABELS.get(
            row.get('pending_action_owner') or '', row.get('pending_action_owner') or '-')

        item['severity'] = self._severity(row.get('attention_score') or 0)
        item['confidence_pct'] = int(round(float(row.get('confidence') or 0) * 100))
        item['low_confidence'] = bool(row.get('confidence') is not None
                                      and float(row.get('confidence')) < 0.5)

        # Rendered from the exact minute columns, not from age_days - two
        # decimal places of a day cannot express "30 Mins".
        item['age_display'] = format_duration(row.get('age_minutes'))
        item['idle_display'] = format_duration(row.get('idle_minutes'))

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
        since = utc_now() - datetime.timedelta(hours=hours)
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
        by_owner: Dict[str, int] = {}
        by_action: Dict[str, int] = {}

        for row in rows:
            owner = row.get('pending_action_owner_label', '-')
            by_owner[owner] = by_owner.get(owner, 0) + 1
            action = row.get('action_label', 'Not analysed')
            by_action[action] = by_action.get(action, 0) + 1

        return {
            'total': len(rows),
            'service_desk_owned': sum(
                1 for r in rows if r.get('pending_action_owner') == SERVICE_DESK),
            'sla_breached': sum(1 for r in rows if r.get('sla_breached')),
            'sla_at_risk': sum(1 for r in rows if 'SLA_AT_RISK' in (r.get('risk_flags') or [])),
            'prolonged_inactivity': sum(1 for r in rows
                                        if 'PROLONGED_INACTIVITY' in (r.get('risk_flags') or [])),
            'caller_awaiting': sum(1 for r in rows if r.get('caller_replied_unanswered')),
            'dependency_cleared': sum(1 for r in rows if r.get('dependency_resolved')),
            'closure_candidates': sum(1 for r in rows if r.get('auto_close_candidate')),
            'needs_review': sum(1 for r in rows if r.get('low_confidence')),
            'by_pending_action_owner': by_owner,
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
                # An average across incidents, so there is no minute column to
                # draw on; derived from days and therefore coarser.
                'avg_idle_display': format_duration_days(
                    sum(idles) / len(idles) if idles else 0.0),
                'sla_breached': sum(1 for t in tickets if t.get('sla_breached')),
                'prolonged_inactivity': sum(1 for t in tickets
                                        if 'PROLONGED_INACTIVITY' in (t.get('risk_flags') or [])),
                'caller_awaiting': sum(1 for t in tickets if t.get('caller_replied_unanswered')),
                'kb_gaps': sum(1 for t in tickets
                               if 'KB_ARTICLE_NOT_LINKED' in (t.get('risk_flags') or [])),
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
                   AND m.resolved_on >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 30 DAY)
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
            'generated_at': utc_now(),
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
            'closure_candidates': [r for r in rows if r.get('auto_close_candidate')][:10],
            'base_url': self.base_url,
            'shadow_mode': self.settings.shadow_mode,
            'reasons': {r['incident_number']: self._reasons_for(r) for r in priority_rows},
        }

    def build_agent_digest(self, agent: str, max_tickets: int = 15) -> Dict[str, Any]:
        rows = self.board_rows(agent=agent)
        return {
            'generated_at': utc_now(),
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
