"""Tests for deterministic signal extraction.

These are the numbers a lead will be shown and asked to trust, so they get
real coverage. Everything under test is a pure function - no database, no
network, no model - which is exactly why the signal layer was written that
way.

    python -m pytest tests -q
"""

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.signals import (build_timeline, classify_actor, count_followups,
                              determine_ball_in_court, evaluate_dependency,
                              extract_signals, last_caller_activity,
                              last_meaningful_agent_action, priority_weight)

NOW = datetime.datetime(2026, 9, 23, 12, 0, 0)
SYSTEM_ACCOUNTS = ['system', 'guest', 'cac.rest']


def ts(days_ago: float) -> str:
    return (NOW - datetime.timedelta(days=days_ago)).strftime('%Y-%m-%d %H:%M:%S')


def event(field, new, days_ago, user='Asha Rao', old=''):
    return {'field': field, 'new': new, 'old': old,
            'update_time': ts(days_ago), 'user_name': user}


def make_ticket(**overrides):
    ticket = {
        'incident_number': 'INC1000001',
        'sys_id': 'abc123',
        'short_description': 'Laptop will not connect to VPN',
        'description': 'User cannot reach the corporate network from home.',
        'caller_name': 'Jamie Fox',
        'assigned_to': 'Asha Rao',
        'assignment_group': 'Service Desk',
        'state': 'On Hold',
        'hold_reason': 'Awaiting Caller',
        'priority': '3 - Medium',
        'category': 'Network',
        'subcategory': 'VPN',
        'opened_at': ts(9),
        'reassignment_count': 1,
        'reopen_count': 0,
        'rfc': '',
        'problem_id': '',
        'contact_type': 'email',
    }
    ticket.update(overrides)
    return ticket


NO_SLA = {'sla_breached': False, 'sla_pct_consumed': None,
          'sla_time_left_mins': None, 'projected_breach_at': None}


# ---------------------------------------------------------------------------
# actor classification - the basis of every other signal
# ---------------------------------------------------------------------------

class TestClassifyActor:
    def test_named_agent_is_an_agent(self):
        assert classify_actor('Asha Rao', 'Jamie Fox', SYSTEM_ACCOUNTS) == 'AGENT'

    def test_caller_is_recognised_case_insensitively(self):
        assert classify_actor('jamie fox', 'Jamie Fox', SYSTEM_ACCOUNTS) == 'CALLER'

    def test_configured_system_account(self):
        assert classify_actor('cac.rest', 'Jamie Fox', SYSTEM_ACCOUNTS) == 'SYSTEM'

    def test_integration_suffix_is_system_even_when_unconfigured(self):
        # Kohler's integration users all end .rest; catching the pattern means a
        # new one does not silently reset every idle clock.
        assert classify_actor('newint.rest', 'Jamie Fox', []) == 'SYSTEM'

    def test_blank_user_is_system(self):
        assert classify_actor('', 'Jamie Fox', SYSTEM_ACCOUNTS) == 'SYSTEM'


# ---------------------------------------------------------------------------
# timeline construction
# ---------------------------------------------------------------------------

class TestBuildTimeline:
    def test_system_noise_fields_are_dropped(self):
        history = [
            event('sys_mod_count', '5', 1),
            event('business_duration', '1970-01-02 00:00:00', 1),
            event('work_notes', 'Called the user, no answer', 1),
        ]
        timeline = build_timeline(history, 'Jamie Fox', SYSTEM_ACCOUNTS)
        assert len(timeline) == 1
        assert timeline[0]['field'] == 'work_notes'

    def test_no_op_field_changes_are_dropped(self):
        history = [event('priority', '3 - Medium', 1, old='3 - Medium')]
        assert build_timeline(history, 'Jamie Fox', SYSTEM_ACCOUNTS) == []

    def test_empty_comments_are_dropped(self):
        history = [event('comments', '   ', 1)]
        assert build_timeline(history, 'Jamie Fox', SYSTEM_ACCOUNTS) == []

    def test_sorted_oldest_first(self):
        history = [event('work_notes', 'second', 1), event('work_notes', 'first', 5)]
        timeline = build_timeline(history, 'Jamie Fox', SYSTEM_ACCOUNTS)
        assert [e['text'] for e in timeline] == ['first', 'second']

    def test_roles_are_tagged(self):
        history = [
            event('comments', 'any update?', 2, user='Jamie Fox'),
            event('work_notes', 'chasing', 1, user='Asha Rao'),
        ]
        timeline = build_timeline(history, 'Jamie Fox', SYSTEM_ACCOUNTS)
        assert [e['role'] for e in timeline] == ['CALLER', 'AGENT']


# ---------------------------------------------------------------------------
# the idle clock
# ---------------------------------------------------------------------------

class TestIdleClock:
    def test_last_agent_action_ignores_caller_and_system(self):
        history = [
            event('work_notes', 'agent note', 6, user='Asha Rao'),
            event('comments', 'caller reply', 2, user='Jamie Fox'),
            event('state', 'On Hold', 1, user='cac.rest'),
        ]
        timeline = build_timeline(history, 'Jamie Fox', SYSTEM_ACCOUNTS)
        assert last_meaningful_agent_action(timeline) == NOW - datetime.timedelta(days=6)

    def test_idle_runs_from_creation_when_never_touched(self):
        ticket = make_ticket(opened_at=ts(9))
        signals = extract_signals(ticket, [], NO_SLA,
                                  system_accounts=SYSTEM_ACCOUNTS, now=NOW)
        assert signals['idle_days'] == pytest.approx(9.0, abs=0.01)
        assert signals['last_agent_action_at'] is None
        assert 'NEVER_TOUCHED' in signals['risk_flags']

    def test_system_update_does_not_reset_the_clock(self):
        """The bug this guards against: an SLA recalculation making a ticket
        that nobody has touched for a week look freshly worked."""
        history = [
            event('work_notes', 'real work', 7, user='Asha Rao'),
            event('sys_updated_on', ts(0.1), 0.1, user='system'),
            event('business_duration', 'x', 0.1, user='system'),
        ]
        signals = extract_signals(make_ticket(), history, NO_SLA,
                                  system_accounts=SYSTEM_ACCOUNTS, now=NOW)
        assert signals['idle_days'] == pytest.approx(7.0, abs=0.01)

    def test_caller_comment_does_not_reset_the_clock(self):
        history = [
            event('work_notes', 'asked for details', 5, user='Asha Rao'),
            event('comments', 'here are the details', 1, user='Jamie Fox'),
        ]
        signals = extract_signals(make_ticket(), history, NO_SLA,
                                  system_accounts=SYSTEM_ACCOUNTS, now=NOW)
        assert signals['idle_days'] == pytest.approx(5.0, abs=0.01)


# ---------------------------------------------------------------------------
# caller replied but nobody answered
# ---------------------------------------------------------------------------

class TestCallerReplied:
    def test_detected_when_caller_spoke_last(self):
        history = [
            event('work_notes', 'awaiting info', 5, user='Asha Rao'),
            event('comments', 'here is the info', 1, user='Jamie Fox'),
        ]
        signals = extract_signals(make_ticket(), history, NO_SLA,
                                  system_accounts=SYSTEM_ACCOUNTS, now=NOW)
        assert signals['caller_replied_unanswered'] is True
        assert 'CALLER_AWAITING_REPLY' in signals['risk_flags']

    def test_not_flagged_when_agent_replied_after(self):
        history = [
            event('comments', 'here is the info', 3, user='Jamie Fox'),
            event('work_notes', 'thanks, investigating', 1, user='Asha Rao'),
        ]
        signals = extract_signals(make_ticket(), history, NO_SLA,
                                  system_accounts=SYSTEM_ACCOUNTS, now=NOW)
        assert signals['caller_replied_unanswered'] is False

    def test_overrides_awaiting_caller_hold_reason(self):
        """A ticket on hold 'awaiting caller' where the caller has already
        replied is ours, not theirs. This is the reframing the whole board
        depends on."""
        history = [
            event('work_notes', 'need more info', 5, user='Asha Rao'),
            event('comments', 'sent it over', 1, user='Jamie Fox'),
        ]
        signals = extract_signals(make_ticket(hold_reason='Awaiting Caller'),
                                  history, NO_SLA,
                                  system_accounts=SYSTEM_ACCOUNTS, now=NOW)
        assert signals['ball_in_court'] == 'AGENT'


# ---------------------------------------------------------------------------
# ball in court
# ---------------------------------------------------------------------------

class TestBallInCourt:
    @pytest.mark.parametrize('hold_reason,expected', [
        ('Awaiting Caller', 'CALLER'),
        ('Awaiting Customer', 'CALLER'),
        ('Awaiting Vendor', 'VENDOR'),
        ('Awaiting Change', 'CHANGE'),
        ('Awaiting Problem', 'PROBLEM'),
        ('Awaiting Approval', 'APPROVAL'),
    ])
    def test_maps_hold_reasons(self, hold_reason, expected):
        ticket = make_ticket(hold_reason=hold_reason)
        assert determine_ball_in_court(ticket, [], False, False) == expected

    def test_unknown_hold_reason_falls_back_to_agent(self):
        ticket = make_ticket(hold_reason='Something bespoke')
        assert determine_ball_in_court(ticket, [], False, False) == 'AGENT'

    def test_unassigned_ticket_is_ours(self):
        ticket = make_ticket(assigned_to='', hold_reason='')
        assert determine_ball_in_court(ticket, [], False, False) == 'AGENT'

    def test_cleared_dependency_returns_the_ball_to_us(self):
        ticket = make_ticket(hold_reason='Awaiting Change')
        assert determine_ball_in_court(ticket, [], False, True) == 'AGENT'


# ---------------------------------------------------------------------------
# dependency detection
# ---------------------------------------------------------------------------

class TestDependency:
    def test_closed_change_is_flagged_as_resolved(self):
        ticket = make_ticket(hold_reason='Awaiting Change', rfc='CHG0044321')
        change = {'number': 'CHG0044321', 'state': 'Closed Complete'}
        result = evaluate_dependency(ticket, change, None)
        assert result['dependency_resolved'] is True
        assert result['dependency_ref'] == 'CHG0044321'

    def test_open_change_is_not_flagged(self):
        ticket = make_ticket(hold_reason='Awaiting Change', rfc='CHG0044321')
        change = {'number': 'CHG0044321', 'state': 'Implement'}
        assert evaluate_dependency(ticket, change, None)['dependency_resolved'] is False

    def test_no_dependency_is_clean(self):
        assert evaluate_dependency(make_ticket(), None, None) == {
            'dependency_ref': None, 'dependency_state': None, 'dependency_resolved': False}

    def test_cleared_dependency_reaches_the_risk_flags(self):
        ticket = make_ticket(hold_reason='Awaiting Change', rfc='CHG0044321')
        signals = extract_signals(
            ticket, [], NO_SLA,
            change_record={'number': 'CHG0044321', 'state': 'Closed Complete'},
            system_accounts=SYSTEM_ACCOUNTS, now=NOW)
        assert 'DEPENDENCY_CLEARED' in signals['risk_flags']
        assert signals['ball_in_court'] == 'AGENT'


# ---------------------------------------------------------------------------
# follow-ups and auto-close
# ---------------------------------------------------------------------------

class TestFollowUps:
    def test_counts_only_customer_visible_agent_chases(self):
        history = [
            event('comments', 'Just following up on this', 8, user='Asha Rao'),
            event('comments', 'Gentle reminder, any update?', 6, user='Asha Rao'),
            event('work_notes', 'following up internally', 5, user='Asha Rao'),
            event('comments', 'unrelated status note', 4, user='Asha Rao'),
        ]
        timeline = build_timeline(history, 'Jamie Fox', SYSTEM_ACCOUNTS)
        count, last_at = count_followups(timeline)
        assert count == 2
        assert last_at == NOW - datetime.timedelta(days=6)

    def test_auto_close_candidate_when_chased_and_silent(self):
        history = [
            event('comments', 'Following up, please confirm', 12, user='Asha Rao'),
            event('comments', 'Second attempt, any update?', 10, user='Asha Rao'),
            event('comments', 'Third reminder, please respond', 8, user='Asha Rao'),
        ]
        signals = extract_signals(
            make_ticket(hold_reason='Awaiting Caller'), history, NO_SLA,
            system_accounts=SYSTEM_ACCOUNTS, now=NOW,
            thresholds={'auto_close_followups': 3, 'auto_close_silence_days': 5})
        assert signals['auto_close_candidate'] is True
        assert 'AUTO_CLOSE_CANDIDATE' in signals['risk_flags']

    def test_not_a_candidate_when_the_caller_has_since_replied(self):
        history = [
            event('comments', 'Following up, please confirm', 12, user='Asha Rao'),
            event('comments', 'Second attempt, any update?', 10, user='Asha Rao'),
            event('comments', 'Third reminder, please respond', 8, user='Asha Rao'),
            event('comments', 'Sorry, here it is', 1, user='Jamie Fox'),
        ]
        signals = extract_signals(
            make_ticket(hold_reason='Awaiting Caller'), history, NO_SLA,
            system_accounts=SYSTEM_ACCOUNTS, now=NOW,
            thresholds={'auto_close_followups': 3, 'auto_close_silence_days': 5})
        assert signals['auto_close_candidate'] is False

    def test_not_a_candidate_below_the_attempt_threshold(self):
        history = [event('comments', 'Following up please', 10, user='Asha Rao')]
        signals = extract_signals(
            make_ticket(hold_reason='Awaiting Caller'), history, NO_SLA,
            system_accounts=SYSTEM_ACCOUNTS, now=NOW,
            thresholds={'auto_close_followups': 3, 'auto_close_silence_days': 5})
        assert signals['auto_close_candidate'] is False


# ---------------------------------------------------------------------------
# SLA and baselines pass through intact
# ---------------------------------------------------------------------------

class TestSlaAndBaselines:
    def test_breach_reaches_the_flags(self):
        sla = {'sla_breached': True, 'sla_pct_consumed': 140.0,
               'sla_time_left_mins': -300.0, 'projected_breach_at': None}
        signals = extract_signals(make_ticket(), [], sla,
                                  system_accounts=SYSTEM_ACCOUNTS, now=NOW)
        assert 'SLA_BREACHED' in signals['risk_flags']

    def test_jeopardy_below_breach(self):
        sla = {'sla_breached': False, 'sla_pct_consumed': 82.0,
               'sla_time_left_mins': 120.0, 'projected_breach_at': None}
        signals = extract_signals(make_ticket(), [], sla,
                                  system_accounts=SYSTEM_ACCOUNTS, now=NOW,
                                  thresholds={'sla_jeopardy_pct': 75})
        assert 'SLA_JEOPARDY' in signals['risk_flags']
        assert 'SLA_BREACHED' not in signals['risk_flags']

    def test_p90_overrun_uses_the_category_baseline(self):
        # 9 days old = 216 hours, well past a 48-hour p90.
        signals = extract_signals(make_ticket(), [], NO_SLA,
                                  baseline={'p90_hours': 48.0},
                                  system_accounts=SYSTEM_ACCOUNTS, now=NOW)
        assert signals['p90_overrun'] is True
        assert 'PAST_EXPECTED_DURATION' in signals['risk_flags']

    def test_no_baseline_means_no_overrun_claim(self):
        signals = extract_signals(make_ticket(), [], NO_SLA,
                                  system_accounts=SYSTEM_ACCOUNTS, now=NOW)
        assert signals['p90_overrun'] is False


# ---------------------------------------------------------------------------
# KB coaching flag
# ---------------------------------------------------------------------------

def test_kb_available_but_not_attached_is_flagged():
    signals = extract_signals(make_ticket(), [], NO_SLA, attached_kb=[],
                              kb_available=True,
                              system_accounts=SYSTEM_ACCOUNTS, now=NOW)
    assert 'KB_NOT_ATTACHED' in signals['risk_flags']


def test_attached_kb_clears_the_flag():
    signals = extract_signals(make_ticket(), [], NO_SLA,
                              attached_kb=[{'kb_knowledge': 'KB0010001'}],
                              kb_available=True,
                              system_accounts=SYSTEM_ACCOUNTS, now=NOW)
    assert 'KB_NOT_ATTACHED' not in signals['risk_flags']


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('priority,expected', [
    ('1 - Critical', 100), ('2 - High', 80), ('3 - Medium', 50),
    ('3 - Moderate', 50), ('4 - Low', 25), ('5 - Planning', 10),
    ('', 25), (None, 25), ('nonsense', 25),
])
def test_priority_weight(priority, expected):
    assert priority_weight(priority) == expected


def test_extract_signals_is_total_on_an_empty_ticket():
    """A ticket with nothing on it must still produce a full signal set -
    a KeyError here would take out the whole nightly pass."""
    signals = extract_signals({'incident_number': 'INC0', 'opened_at': ts(6)},
                              [], NO_SLA, system_accounts=[], now=NOW)
    for key in ('age_days', 'idle_days', 'ball_in_court', 'risk_flags',
                'followup_count', 'auto_close_candidate'):
        assert key in signals


def test_last_caller_activity_none_when_caller_silent():
    timeline = build_timeline([event('work_notes', 'note', 1)], 'Jamie Fox', SYSTEM_ACCOUNTS)
    assert last_caller_activity(timeline) is None
