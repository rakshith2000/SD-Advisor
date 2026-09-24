"""Deterministic signal extraction.

This is the heart of the tool and the part that must never be wrong, so it is
written as pure functions over plain dicts: no network, no database, no LLM.
Everything here is unit-testable and every number it produces can be explained
to a lead without saying "the AI decided".

The key judgement encoded here is what counts as a *meaningful agent action*.
Getting that wrong is what makes these tools nag about tickets that are
actually being worked, so system accounts, SLA recalculations and the caller's
own updates are all excluded from the idle clock.
"""

import datetime
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

TS_FORMAT = '%Y-%m-%d %H:%M:%S'

# History fields that represent a human progressing the ticket.
AGENT_ACTION_FIELDS = {
    'work_notes', 'comments', 'additional_comments', 'state', 'assigned_to',
    'assignment_group', 'hold_reason', 'priority', 'impact', 'urgency',
    'category', 'subcategory', 'close_notes', 'close_code', 'cmdb_ci',
    'short_description', 'description', 'problem_id', 'rfc',
}

# Fields that churn on their own and must never reset the idle clock.
SYSTEM_NOISE_FIELDS = {
    'sys_updated_on', 'sys_updated_by', 'sys_mod_count', 'business_duration',
    'calendar_duration', 'business_stc', 'calendar_stc', 'time_worked',
    'sla_due', 'made_sla', 'escalation', 'reassignment_count', 'reopen_count',
    'activity_due', 'work_start', 'work_end', 'upon_reject', 'upon_approval',
}

CUSTOMER_VISIBLE_FIELDS = {'comments', 'additional_comments'}

HOLD_REASON_BLOCKER = {
    'awaiting caller': 'CALLER',
    'awaiting customer': 'CALLER',
    'awaiting user': 'CALLER',
    'awaiting user info': 'CALLER',
    'awaiting vendor': 'VENDOR',
    'awaiting third party': 'VENDOR',
    'awaiting change': 'CHANGE',
    'awaiting problem': 'PROBLEM',
    'awaiting approval': 'APPROVAL',
    'awaiting evidence': 'CALLER',
}

RESOLVED_DEPENDENCY_STATES = {
    'closed', 'closed complete', 'closed successful', 'closed incomplete',
    'closed skipped', 'resolved', 'completed', 'review', 'cancelled', 'canceled',
}

PRIORITY_WEIGHT = {
    '1': 100, '2': 80, '3': 50, '4': 25, '5': 10,
}

_FOLLOWUP_HINT = re.compile(
    r'\b(follow[ -]?up|following up|gentle reminder|reminder|chas(?:e|ing)|'
    r'awaiting your|any update|please confirm|please respond|still waiting|'
    r'second attempt|third attempt|no response)\b',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def parse_ts(value: Any) -> Optional[datetime.datetime]:
    if not value or not str(value).strip():
        return None
    try:
        return datetime.datetime.strptime(str(value).strip(), TS_FORMAT)
    except ValueError:
        return None


def _days_between(later: Optional[datetime.datetime],
                  earlier: Optional[datetime.datetime]) -> Optional[float]:
    if later is None or earlier is None:
        return None
    return round(max(0.0, (later - earlier).total_seconds()) / 86400.0, 2)


def classify_actor(user_name: str, caller_name: str,
                   system_accounts: Sequence[str]) -> str:
    """AGENT | CALLER | SYSTEM."""
    name = (user_name or '').strip().lower()
    if not name:
        return 'SYSTEM'
    if name in {a.lower() for a in system_accounts}:
        return 'SYSTEM'
    if name.endswith('.rest') or name.startswith('system') or name in ('guest', 'admin'):
        return 'SYSTEM'
    if caller_name and name == caller_name.strip().lower():
        return 'CALLER'
    return 'AGENT'


def build_timeline(history: List[Dict[str, Any]], caller_name: str,
                   system_accounts: Sequence[str]) -> List[Dict[str, Any]]:
    """Flatten history into a readable, role-tagged activity list.

    Only communication and state-changing events are kept - the raw history is
    mostly field churn nobody needs to read.
    """
    interesting = AGENT_ACTION_FIELDS
    timeline: List[Dict[str, Any]] = []

    for event in history:
        field = (event.get('field') or '').strip().lower()
        if field in SYSTEM_NOISE_FIELDS or field not in interesting:
            continue

        actor = (event.get('user_name') or '').strip()
        role = classify_actor(actor, caller_name, system_accounts)
        new_value = str(event.get('new') or '').strip()

        if field in ('comments', 'additional_comments', 'work_notes'):
            if not new_value:
                continue
            kind = 'customer comment' if field in CUSTOMER_VISIBLE_FIELDS else 'work note'
            text = new_value
        else:
            old_value = str(event.get('old') or '').strip()
            if old_value == new_value:
                continue
            kind = f'{field} changed'
            text = f'{old_value or "(empty)"} -> {new_value or "(empty)"}'

        timeline.append({
            'when': event.get('update_time') or '',
            'actor': actor or 'system',
            'role': role,
            'field': field,
            'kind': kind,
            'text': text,
        })

    timeline.sort(key=lambda e: e['when'])
    return timeline


# ---------------------------------------------------------------------------
# individual signals
# ---------------------------------------------------------------------------

def last_meaningful_agent_action(timeline: List[Dict[str, Any]]) -> Optional[datetime.datetime]:
    for event in reversed(timeline):
        if event['role'] != 'AGENT':
            continue
        if event['field'] in SYSTEM_NOISE_FIELDS:
            continue
        stamp = parse_ts(event['when'])
        if stamp:
            return stamp
    return None


def last_caller_activity(timeline: List[Dict[str, Any]]) -> Optional[datetime.datetime]:
    for event in reversed(timeline):
        if event['role'] == 'CALLER':
            stamp = parse_ts(event['when'])
            if stamp:
                return stamp
    return None


def determine_ball_in_court(ticket: Dict[str, Any], timeline: List[Dict[str, Any]],
                            caller_replied_unanswered: bool,
                            dependency_resolved: bool) -> str:
    """Who owes the next action.

    This is the field leads care about most: it turns "40 aged tickets" into
    "9 that are actually ours".
    """
    # A dependency that has already closed puts the ball straight back with us,
    # regardless of what the hold reason still says.
    if dependency_resolved:
        return 'AGENT'

    # A caller who replied and got no answer is always our move.
    if caller_replied_unanswered:
        return 'AGENT'

    hold_reason = (ticket.get('hold_reason') or '').strip().lower()
    if hold_reason:
        for needle, blocker in HOLD_REASON_BLOCKER.items():
            if needle in hold_reason:
                return blocker

    state = (ticket.get('state') or '').strip().lower()
    if state == 'new' or not (ticket.get('assigned_to') or '').strip():
        return 'AGENT'

    if timeline:
        last_role = timeline[-1]['role']
        if last_role == 'CALLER':
            return 'AGENT'

    return 'AGENT'


def count_followups(timeline: List[Dict[str, Any]],
                    since: Optional[datetime.datetime] = None) -> Tuple[int, Optional[datetime.datetime]]:
    """Customer-visible agent comments that read like a chase.

    Used for the auto-close policy - "three documented attempts, no reply".
    """
    count = 0
    last_at: Optional[datetime.datetime] = None

    for event in timeline:
        if event['role'] != 'AGENT':
            continue
        if event['field'] not in CUSTOMER_VISIBLE_FIELDS:
            continue

        stamp = parse_ts(event['when'])
        if since and stamp and stamp < since:
            continue
        if not _FOLLOWUP_HINT.search(event.get('text') or ''):
            continue

        count += 1
        if stamp:
            last_at = stamp

    return count, last_at


def evaluate_dependency(ticket: Dict[str, Any],
                        change_record: Optional[Dict[str, Any]],
                        problem_record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Detect a ticket held for a dependency that has already been closed.

    These are invisible in a normal queue review and often account for the
    oldest tickets on the board.
    """
    result: Dict[str, Any] = {
        'dependency_ref': None,
        'dependency_state': None,
        'dependency_resolved': False,
    }

    hold_reason = (ticket.get('hold_reason') or '').strip().lower()
    record = None

    if 'change' in hold_reason and change_record:
        record = change_record
    elif 'problem' in hold_reason and problem_record:
        record = problem_record
    else:
        record = change_record or problem_record

    if not record:
        return result

    state = str(record.get('state') or '').strip()
    result['dependency_ref'] = record.get('number')
    result['dependency_state'] = state
    result['dependency_resolved'] = state.lower() in RESOLVED_DEPENDENCY_STATES
    return result


def priority_weight(priority: str) -> int:
    match = re.match(r'\s*(\d)', str(priority or ''))
    return PRIORITY_WEIGHT.get(match.group(1), 25) if match else 25


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------

def extract_signals(ticket: Dict[str, Any], history: List[Dict[str, Any]],
                    sla_summary: Dict[str, Any], *,
                    change_record: Optional[Dict[str, Any]] = None,
                    problem_record: Optional[Dict[str, Any]] = None,
                    attached_kb: Optional[List[Dict[str, Any]]] = None,
                    kb_available: bool = False,
                    baseline: Optional[Dict[str, float]] = None,
                    system_accounts: Sequence[str] = (),
                    thresholds: Optional[Dict[str, Any]] = None,
                    now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Compute every deterministic signal for one ticket.

    Returns a flat dict that maps directly onto ticket_signal columns, plus a
    '_timeline' key the prompt builder consumes.
    """
    now = now or datetime.datetime.now()
    thresholds = thresholds or {}
    caller_name = ticket.get('caller_name') or ''

    timeline = build_timeline(history, caller_name, system_accounts)

    opened_at = parse_ts(ticket.get('opened_at')) or now
    agent_action_at = last_meaningful_agent_action(timeline)
    caller_activity_at = last_caller_activity(timeline)

    # Idle from the last agent action; if an agent has never touched it, idle
    # runs from creation - an untouched ticket is the worst case, not a gap.
    idle_from = agent_action_at or opened_at

    caller_replied_unanswered = bool(
        caller_activity_at and (agent_action_at is None or caller_activity_at > agent_action_at)
    )

    dependency = evaluate_dependency(ticket, change_record, problem_record)

    ball_in_court = determine_ball_in_court(
        ticket, timeline, caller_replied_unanswered, dependency['dependency_resolved'])

    followup_count, last_followup_at = count_followups(timeline)

    age_days = _days_between(now, opened_at) or 0.0
    idle_days = _days_between(now, idle_from) or 0.0

    state_changes = [e for e in timeline if e['field'] == 'state']
    state_since = parse_ts(state_changes[-1]['when']) if state_changes else opened_at
    days_in_state = _days_between(now, state_since) or age_days

    # Auto-close eligibility: enough documented chases, caller silent since.
    silence_days = thresholds.get('auto_close_silence_days', 5)
    required_followups = thresholds.get('auto_close_followups', 3)
    days_since_last_followup = _days_between(now, last_followup_at)
    auto_close_candidate = bool(
        ball_in_court == 'CALLER'
        and followup_count >= required_followups
        and not caller_replied_unanswered
        and days_since_last_followup is not None
        and days_since_last_followup >= silence_days
    )

    expected_hours = (baseline or {}).get('p90_hours')
    p90_overrun = bool(expected_hours and (age_days * 24.0) > float(expected_hours))

    signals: Dict[str, Any] = {
        'age_days': age_days,
        'idle_days': idle_days,
        'days_in_state': days_in_state,
        'last_agent_action_at': agent_action_at,
        'last_caller_activity_at': caller_activity_at,

        'ball_in_court': ball_in_court,
        'caller_replied_unanswered': caller_replied_unanswered,

        'dependency_ref': dependency['dependency_ref'],
        'dependency_state': dependency['dependency_state'],
        'dependency_resolved': dependency['dependency_resolved'],

        'followup_count': followup_count,
        'days_since_last_followup': days_since_last_followup,
        'auto_close_candidate': auto_close_candidate,

        'sla_breached': bool(sla_summary.get('sla_breached')),
        'sla_pct_consumed': sla_summary.get('sla_pct_consumed'),
        'sla_time_left_mins': sla_summary.get('sla_time_left_mins'),
        'projected_breach_at': sla_summary.get('projected_breach_at'),
        'vendor_sla_breached': bool(sla_summary.get('vendor_sla_breached')),

        'expected_resolution_hours': expected_hours,
        'p90_overrun': p90_overrun,

        'kb_available': bool(kb_available),
        'kb_attached': bool(attached_kb),

        '_timeline': timeline,
    }

    signals['risk_flags'] = derive_risk_flags(signals, thresholds)
    return signals


def derive_risk_flags(signals: Dict[str, Any], thresholds: Dict[str, Any]) -> List[str]:
    """Human-readable flags. These drive the stream-tier alerts and the UI chips."""
    flags: List[str] = []

    jeopardy_pct = thresholds.get('sla_jeopardy_pct', 75)
    stagnation = thresholds.get('stagnation_days', 2)
    critical_stagnation = thresholds.get('critical_stagnation_days', 4)

    if signals.get('sla_breached'):
        flags.append('SLA_BREACHED')
    elif (signals.get('sla_pct_consumed') or 0) >= jeopardy_pct:
        flags.append('SLA_JEOPARDY')

    if signals.get('idle_days', 0) >= critical_stagnation:
        flags.append('CRITICALLY_STALE')
    elif signals.get('idle_days', 0) >= stagnation:
        flags.append('STALE')

    # A third-party overrun is not our SLA breach, but it is the evidence that
    # makes chasing the vendor urgent - so it gets its own flag.
    if signals.get('vendor_sla_breached'):
        flags.append('VENDOR_SLA_BREACHED')

    if signals.get('caller_replied_unanswered'):
        flags.append('CALLER_AWAITING_REPLY')

    if signals.get('dependency_resolved'):
        flags.append('DEPENDENCY_CLEARED')

    if signals.get('auto_close_candidate'):
        flags.append('AUTO_CLOSE_CANDIDATE')

    if signals.get('p90_overrun'):
        flags.append('PAST_EXPECTED_DURATION')

    if signals.get('kb_available') and not signals.get('kb_attached'):
        flags.append('KB_NOT_ATTACHED')

    if signals.get('last_agent_action_at') is None:
        flags.append('NEVER_TOUCHED')

    return flags
