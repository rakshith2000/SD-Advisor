"""Tests for the attention score and the supporting pure helpers.

The score decides what a lead reads first, so the properties that matter are:
it stays in range, it is fully attributable, and a worse ticket always scores
higher than a better one.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.redaction import Redactor
from pipeline.retrieval import scope_key
from pipeline.scoring import (DEFAULT_WEIGHTS, compute_attention_score,
                              top_reasons)

THRESHOLDS = {'aged_after_days': 5, 'critical_stagnation_days': 4,
              'stagnation_days': 2, 'sla_jeopardy_pct': 75}


def base_ticket(**overrides):
    ticket = {'priority': '3 - Medium', 'reassignment_count': 0, 'reopen_count': 0}
    ticket.update(overrides)
    return ticket


def base_signals(**overrides):
    signals = {
        'age_days': 6.0, 'idle_days': 0.5, 'sla_breached': False,
        'sla_pct_consumed': 10.0, 'sla_time_left_mins': 5000.0,
        'caller_replied_unanswered': False, 'dependency_resolved': False,
        'auto_close_candidate': False, 'last_agent_action_at': 'set',
        'p90_overrun': False, 'expected_resolution_hours': None,
    }
    signals.update(overrides)
    return signals


class TestScoreBounds:
    def test_stays_within_zero_and_one_hundred(self):
        worst = compute_attention_score(
            base_ticket(priority='1 - Critical', reassignment_count=9, reopen_count=4),
            base_signals(age_days=90, idle_days=60, sla_breached=True,
                         sla_pct_consumed=400.0, caller_replied_unanswered=True,
                         dependency_resolved=True, auto_close_candidate=True,
                         last_agent_action_at=None, p90_overrun=True,
                         expected_resolution_hours=4.0),
            DEFAULT_WEIGHTS, THRESHOLDS)
        assert 0 <= worst['score'] <= 100

    def test_a_healthy_ticket_scores_low(self):
        result = compute_attention_score(
            base_ticket(priority='4 - Low'),
            base_signals(age_days=5.1, idle_days=0.1, sla_pct_consumed=5.0),
            DEFAULT_WEIGHTS, THRESHOLDS)
        assert result['score'] < 20

    def test_a_breached_stale_ticket_scores_high(self):
        result = compute_attention_score(
            base_ticket(priority='1 - Critical'),
            base_signals(age_days=20, idle_days=9, sla_breached=True,
                         sla_pct_consumed=200.0, caller_replied_unanswered=True),
            DEFAULT_WEIGHTS, THRESHOLDS)
        assert result['score'] >= 70


class TestMonotonicity:
    """More idle, older, or closer to breach must never score lower."""

    def test_more_idle_scores_higher(self):
        low = compute_attention_score(base_ticket(), base_signals(idle_days=1),
                                      DEFAULT_WEIGHTS, THRESHOLDS)['score']
        high = compute_attention_score(base_ticket(), base_signals(idle_days=4),
                                       DEFAULT_WEIGHTS, THRESHOLDS)['score']
        assert high > low

    def test_older_scores_higher(self):
        low = compute_attention_score(base_ticket(), base_signals(age_days=6),
                                      DEFAULT_WEIGHTS, THRESHOLDS)['score']
        high = compute_attention_score(base_ticket(), base_signals(age_days=14),
                                       DEFAULT_WEIGHTS, THRESHOLDS)['score']
        assert high > low

    def test_higher_priority_scores_higher(self):
        low = compute_attention_score(base_ticket(priority='4 - Low'), base_signals(),
                                      DEFAULT_WEIGHTS, THRESHOLDS)['score']
        high = compute_attention_score(base_ticket(priority='1 - Critical'), base_signals(),
                                       DEFAULT_WEIGHTS, THRESHOLDS)['score']
        assert high > low

    def test_breach_outranks_jeopardy(self):
        jeopardy = compute_attention_score(
            base_ticket(), base_signals(sla_pct_consumed=80.0),
            DEFAULT_WEIGHTS, THRESHOLDS)['score']
        breached = compute_attention_score(
            base_ticket(), base_signals(sla_breached=True, sla_pct_consumed=80.0),
            DEFAULT_WEIGHTS, THRESHOLDS)['score']
        assert breached > jeopardy


class TestAttribution:
    def test_every_component_is_reported(self):
        result = compute_attention_score(base_ticket(), base_signals(),
                                         DEFAULT_WEIGHTS, THRESHOLDS)
        assert set(result['breakdown']) == set(DEFAULT_WEIGHTS)

    def test_points_sum_to_the_score(self):
        result = compute_attention_score(
            base_ticket(priority='2 - High'),
            base_signals(idle_days=3, age_days=11, sla_pct_consumed=60.0),
            DEFAULT_WEIGHTS, THRESHOLDS)
        total = sum(part['points'] for part in result['breakdown'].values())
        assert result['score'] == pytest.approx(round(total), abs=1)

    def test_a_disabled_component_contributes_nothing(self):
        weights = dict(DEFAULT_WEIGHTS, stagnation=0)
        result = compute_attention_score(base_ticket(), base_signals(idle_days=30),
                                         weights, THRESHOLDS)
        assert result['breakdown']['stagnation']['points'] == 0

    def test_top_reasons_are_ordered_and_nonzero(self):
        result = compute_attention_score(
            base_ticket(priority='1 - Critical'),
            base_signals(idle_days=8, sla_breached=True), DEFAULT_WEIGHTS, THRESHOLDS)
        reasons = top_reasons(result['breakdown'])
        assert reasons
        assert len(reasons) <= 3
        assert 'churn' not in ' '.join(reasons)   # zero contributors are excluded


class TestBlockedStale:
    def test_cleared_dependency_lifts_the_score(self):
        plain = compute_attention_score(base_ticket(), base_signals(),
                                        DEFAULT_WEIGHTS, THRESHOLDS)['score']
        cleared = compute_attention_score(base_ticket(),
                                          base_signals(dependency_resolved=True),
                                          DEFAULT_WEIGHTS, THRESHOLDS)['score']
        assert cleared > plain

    def test_never_touched_lifts_the_score(self):
        plain = compute_attention_score(base_ticket(), base_signals(),
                                        DEFAULT_WEIGHTS, THRESHOLDS)['score']
        untouched = compute_attention_score(base_ticket(),
                                            base_signals(last_agent_action_at=None),
                                            DEFAULT_WEIGHTS, THRESHOLDS)['score']
        assert untouched > plain


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

class TestRedaction:
    def test_email_is_replaced_and_restored(self):
        redactor = Redactor()
        scrubbed = redactor.scrub('Contact jamie.fox@kohlerco.com for access')
        assert 'jamie.fox@kohlerco.com' not in scrubbed
        assert '[[EMAIL_1]]' in scrubbed
        assert redactor.restore(scrubbed) == 'Contact jamie.fox@kohlerco.com for access'

    def test_the_same_value_reuses_one_placeholder(self):
        redactor = Redactor()
        scrubbed = redactor.scrub('a@b.com then a@b.com again')
        assert scrubbed.count('[[EMAIL_1]]') == 2
        assert '[[EMAIL_2]]' not in scrubbed

    def test_credential_assignment_is_caught(self):
        redactor = Redactor()
        scrubbed = redactor.scrub('the password is Hunter2Rocks!')
        assert 'Hunter2Rocks!' not in scrubbed

    def test_phone_numbers_are_masked(self):
        redactor = Redactor()
        assert '312-245-9129' not in redactor.scrub('Call 312-245-9129 to confirm')

    def test_disabled_redactor_is_a_pass_through(self):
        redactor = Redactor(enabled=False)
        text = 'Contact jamie.fox@kohlerco.com'
        assert redactor.scrub(text) == text

    def test_nested_structures_are_scrubbed(self):
        redactor = Redactor()
        result = redactor.scrub_mapping({
            'summary': 'ping a@b.com',
            'events': [{'text': 'also a@b.com'}],
            'count': 3,
        })
        assert 'a@b.com' not in result['summary']
        assert 'a@b.com' not in result['events'][0]['text']
        assert result['count'] == 3

    def test_restore_is_a_no_op_when_nothing_was_redacted(self):
        redactor = Redactor()
        assert redactor.restore('plain text') == 'plain text'


# ---------------------------------------------------------------------------
# misc helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('category,subcategory,expected', [
    ('Network', 'VPN', 'network|vpn'),
    ('  PC - Software ', ' Adobe ', 'pc - software|adobe'),
    ('', '', 'unknown|unknown'),
])
def test_scope_key_normalises(category, subcategory, expected):
    assert scope_key(category, subcategory) == expected
