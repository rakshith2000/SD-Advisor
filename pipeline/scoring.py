"""Attention Score - the ranking that actually saves the lead time.

Deliberately deterministic and fully decomposed: every point is attributable
to a named component, so a lead can ask "why is this ticket top of my list"
and get an arithmetic answer rather than a model's opinion.

Weights live in attention_weightage and are editable from the UI. The
"revert to defaults if the weights do not sum to 100" guard is borrowed from
the existing audit tool - it is a good idea and stops a half-finished edit
silently distorting every score.
"""

import copy
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from pipeline.signals import priority_weight

log = get_logger('pipeline.scoring')

DEFAULT_WEIGHTS: Dict[str, int] = {
    'sla_jeopardy': 25,
    'stagnation': 25,
    'age': 10,
    'priority': 10,
    'churn': 5,
    'blocked_stale': 15,
    'p90_overrun': 10,
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

def _sla_component(signals: Dict[str, Any]) -> float:
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


def _stagnation_component(signals: Dict[str, Any], thresholds: Dict[str, Any]) -> float:
    idle = float(signals.get('idle_days') or 0.0)
    critical = float(thresholds.get('critical_stagnation_days', 4) or 4)
    # Linear to the critical threshold, then saturated.
    return max(0.0, min(100.0, (idle / critical) * 100.0))


def _age_component(signals: Dict[str, Any], thresholds: Dict[str, Any]) -> float:
    age = float(signals.get('age_days') or 0.0)
    aged_after = float(thresholds.get('aged_after_days', 5) or 5)
    if age <= aged_after:
        return 0.0
    # Another full threshold-width past the trigger reaches 100.
    return max(0.0, min(100.0, ((age - aged_after) / aged_after) * 100.0))


def _priority_component(ticket: Dict[str, Any]) -> float:
    return float(priority_weight(ticket.get('priority')))


def _churn_component(ticket: Dict[str, Any]) -> float:
    reassignments = int(ticket.get('reassignment_count') or 0)
    reopens = int(ticket.get('reopen_count') or 0)
    return max(0.0, min(100.0, reassignments * 20.0 + reopens * 30.0))


def _blocked_stale_component(signals: Dict[str, Any]) -> float:
    """The "should not still be sitting there" signals."""
    score = 0.0
    if signals.get('caller_replied_unanswered'):
        score += 60.0
    if signals.get('dependency_resolved'):
        score += 60.0
    if signals.get('auto_close_candidate'):
        score += 30.0
    if signals.get('last_agent_action_at') is None:
        score += 40.0
    # The vendor has blown their target and nobody has escalated. Contributes
    # here rather than to sla_jeopardy, which is reserved for the
    # customer-facing commitment.
    if signals.get('vendor_sla_breached'):
        score += 40.0
    return min(100.0, score)


def _p90_component(signals: Dict[str, Any]) -> float:
    if not signals.get('p90_overrun'):
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
        'sla_jeopardy': _sla_component(signals),
        'stagnation': _stagnation_component(signals, thresholds),
        'age': _age_component(signals, thresholds),
        'priority': _priority_component(ticket),
        'churn': _churn_component(ticket),
        'blocked_stale': _blocked_stale_component(signals),
        'p90_overrun': _p90_component(signals),
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
    """Plain-English 'why is this ranked here' for the email and UI."""
    labels = {
        'sla_jeopardy': 'SLA at risk',
        'stagnation': 'no recent agent action',
        'age': 'ticket age',
        'priority': 'business priority',
        'churn': 'reassignment churn',
        'blocked_stale': 'stalled despite being actionable',
        'p90_overrun': 'past expected resolution time',
    }
    ranked = sorted(breakdown.items(), key=lambda kv: kv[1]['points'], reverse=True)
    return [labels.get(name, name) for name, data in ranked if data['points'] > 0][:limit]
