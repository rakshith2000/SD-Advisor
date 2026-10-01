"""Attention Score - the ranking that determines what a lead reviews first.

Deliberately deterministic and fully decomposed: every point is attributable
to a named component, so a lead asking why an incident is ranked highest
receives an arithmetic answer rather than a model's opinion.

Weights live in attention_weightage and are editable from the UI. The
"revert to defaults if the weights do not sum to 100" guard is borrowed from
the existing audit tool: it is a sound safeguard and prevents an incomplete edit
silently distorting every score.
"""

import copy
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from pipeline.signals import priority_weight

log = get_logger('pipeline.scoring')

DEFAULT_WEIGHTS: Dict[str, int] = {
    'sla_risk': 25,
    'inactivity_duration': 25,
    'ticket_age': 10,
    'business_priority': 10,
    'reassignment_activity': 5,
    'unactioned_delay': 15,
    'duration_overrun': 10,
}


def load_weights(db) -> Dict[str, int]:
    """Read tunable weights, falling back to defaults when they are unusable."""
    weights = copy.deepcopy(DEFAULT_WEIGHTS)

    try:
        rows = db.retrieve('attention_weightage', conditions=[
            {'col': 'enabled', 'op': 'eq', 'val': 1}])
    except Exception:
        log.exception('Could not read attention_weightage; using defaults')
        return weights

    configured = {
        str(row['component']): int(row['weightage'])
        for row in rows
        if str(row['component']) in DEFAULT_WEIGHTS
    }

    if not configured:
        return weights

    merged = dict(weights)
    merged.update(configured)

    total = sum(merged.values())
    if total != 100:
        log.warning('attention_weightage sums to %s, not 100 - reverting to defaults', total)
        return weights

    return merged


# ---------------------------------------------------------------------------
# component scores, each normalised to 0-100
# ---------------------------------------------------------------------------

def _sla_risk_component(signals: Dict[str, Any]) -> float:
    if signals.get('sla_breached'):
        return 100.0

    pct = signals.get('sla_pct_consumed')
    if pct is not None:
        return max(0.0, min(100.0, float(pct)))

    # No percentage available - fall back to time remaining.
    left = signals.get('sla_time_left_mins')
    if left is None:
        return 0.0
    if left <= 0:
        return 100.0
    if left <= 60:
        return 90.0
    if left <= 240:
        return 70.0
    if left <= 1440:
        return 40.0
    return 10.0


def _inactivity_duration_component(signals: Dict[str, Any], thresholds: Dict[str, Any]) -> float:
    idle = float(signals.get('idle_days') or 0.0)
    critical = float(thresholds.get('prolonged_inactivity_days', 4) or 4)
    # Linear to the critical threshold, then saturated.
    return max(0.0, min(100.0, (idle / critical) * 100.0))


def _ticket_age_component(signals: Dict[str, Any], thresholds: Dict[str, Any]) -> float:
    age = float(signals.get('age_days') or 0.0)
    aged_after = float(thresholds.get('aged_after_days', 5) or 5)
    if age <= aged_after:
        return 0.0
    # Another full threshold-width past the trigger reaches 100.
    return max(0.0, min(100.0, ((age - aged_after) / aged_after) * 100.0))


def _business_priority_component(ticket: Dict[str, Any]) -> float:
    return float(priority_weight(ticket.get('priority')))


def _reassignment_activity_component(ticket: Dict[str, Any]) -> float:
    reassignments = int(ticket.get('reassignment_count') or 0)
    reopens = int(ticket.get('reopen_count') or 0)
    return max(0.0, min(100.0, reassignments * 20.0 + reopens * 30.0))


def _unactioned_delay_component(signals: Dict[str, Any]) -> float:
    """Indicators that an incident is actionable but has not been progressed."""
    score = 0.0
    if signals.get('caller_replied_unanswered'):
        score += 60.0
    if signals.get('dependency_resolved'):
        score += 60.0
    if signals.get('auto_close_candidate'):
        score += 30.0
    if signals.get('last_agent_action_at') is None:
        score += 40.0
    # The vendor has exceeded its target and no escalation has been raised.
    # Contributes here rather than to sla_risk, which is reserved for the
    # customer-facing commitment.
    if signals.get('vendor_sla_breached'):
        score += 40.0
    return min(100.0, score)


def _duration_overrun_component(signals: Dict[str, Any]) -> float:
    if not signals.get('duration_overrun'):
        return 0.0
    expected = signals.get('expected_resolution_hours')
    age_hours = float(signals.get('age_days') or 0.0) * 24.0
    if not expected or float(expected) <= 0:
        return 100.0
    overrun_ratio = age_hours / float(expected)
    # 1x expected = 50, 2x or more = 100.
    return max(0.0, min(100.0, 50.0 * overrun_ratio))


# ---------------------------------------------------------------------------

def compute_attention_score(ticket: Dict[str, Any], signals: Dict[str, Any],
                            weights: Optional[Dict[str, int]] = None,
                            thresholds: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return {'score': int, 'breakdown': {component: {...}}}."""
    weights = weights or DEFAULT_WEIGHTS
    thresholds = thresholds or {}

    components = {
        'sla_risk': _sla_risk_component(signals),
        'inactivity_duration': _inactivity_duration_component(signals, thresholds),
        'ticket_age': _ticket_age_component(signals, thresholds),
        'business_priority': _business_priority_component(ticket),
        'reassignment_activity': _reassignment_activity_component(ticket),
        'unactioned_delay': _unactioned_delay_component(signals),
        'duration_overrun': _duration_overrun_component(signals),
    }

    breakdown: Dict[str, Any] = {}
    total = 0.0

    for name, raw in components.items():
        weight = int(weights.get(name, 0))
        contribution = raw * weight / 100.0
        total += contribution
        breakdown[name] = {
            'raw': round(raw, 1),
            'weight': weight,
            'points': round(contribution, 1),
        }

    return {
        'score': int(round(max(0.0, min(100.0, total)))),
        'breakdown': breakdown,
    }


def top_reasons(breakdown: Dict[str, Any], limit: int = 3) -> List[str]:
    """The ranking rationale shown to leads, in plain language.

    Rendered mid-sentence as "ranked for X, Y, Z", hence the lower case.
    """
    labels = {
        'sla_risk': 'SLA breach risk',
        'inactivity_duration': 'no recent Service Desk action',
        'ticket_age': 'ticket age',
        'business_priority': 'business priority',
        'reassignment_activity': 'reassignment activity',
        'unactioned_delay': 'actionable but not progressed',
        'duration_overrun': 'expected resolution time exceeded',
    }
    ranked = sorted(breakdown.items(), key=lambda kv: kv[1]['points'], reverse=True)
    return [labels.get(name, name) for name, data in ranked if data['points'] > 0][:limit]
