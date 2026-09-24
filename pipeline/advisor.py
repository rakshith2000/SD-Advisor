"""The recommendation step.

One structured LLM call per ticket, keyed on a hash of everything that went
into the prompt. If nothing material has changed since the last analysis the
cached recommendation is reused - which matters a lot here, because an aged
ticket is by definition one that mostly is not changing. In steady state this
keeps real model calls to the tickets that actually moved.
"""

import datetime
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from core.llm.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from core.llm.schemas import (Recommendation, RecommendedAction,
                              recommendation_json_schema, sanitise)
from core.logging_setup import get_logger
from core.redaction import Redactor

log = get_logger('pipeline.advisor')

_SCHEMA = recommendation_json_schema()

# Fields whose change should invalidate a cached recommendation.
_CACHE_KEYS = (
    'state', 'hold_reason', 'assignment_group', 'assigned_to', 'priority',
)
_CACHE_SIGNALS = (
    'ball_in_court', 'caller_replied_unanswered', 'dependency_resolved',
    'auto_close_candidate', 'sla_breached', 'followup_count',
)


def compute_input_hash(ticket: Dict[str, Any], signals: Dict[str, Any],
                       timeline_length: int, evidence_refs: List[str]) -> str:
    """Stable fingerprint of the analysis inputs.

    Deliberately excludes continuously-drifting values such as age_days and
    idle_days: if those were included, every single poll would look like a
    change and the cache would never hit. Stagnation crossing a threshold is
    caught by the risk flags instead, which are included.
    """
    payload = {
        'prompt_version': PROMPT_VERSION,
        'ticket': {k: ticket.get(k) for k in _CACHE_KEYS},
        'signals': {k: signals.get(k) for k in _CACHE_SIGNALS},
        'flags': sorted(signals.get('risk_flags') or []),
        'timeline_length': timeline_length,
        'evidence': sorted(evidence_refs),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


class Advisor:
    def __init__(self, db, llm, settings, valid_groups: Optional[List[str]] = None):
        self.db = db
        self.llm = llm
        self.settings = settings
        self.valid_groups = valid_groups or []
        self.redact = bool(settings.get('runtime.redact_before_llm', True))
        self.min_confidence = float(settings.get('llm.min_confidence_to_surface', 0.55))

    # -- cache -------------------------------------------------------------

    def cached(self, incident_number: str, input_hash: str) -> Optional[Dict[str, Any]]:
        return self.db.query_one(
            'SELECT * FROM recommendation '
            ' WHERE incident_number = %s AND input_hash = %s AND superseded = 0 '
            ' ORDER BY id DESC LIMIT 1',
            (incident_number, input_hash))

    # -- main --------------------------------------------------------------

    def recommend(self, ticket: Dict[str, Any], signals: Dict[str, Any],
                  similar: List[Dict[str, Any]], kb_articles: List[Dict[str, Any]],
                  force: bool = False) -> Tuple[Dict[str, Any], bool]:
        """Return (recommendation_row, came_from_cache)."""
        number = ticket['incident_number']
        timeline = signals.get('_timeline') or []

        evidence_refs = ([s.get('number', '') for s in similar]
                         + [a.get('number', '') for a in kb_articles])
        input_hash = compute_input_hash(ticket, signals, len(timeline), evidence_refs)

        if not force:
            hit = self.cached(number, input_hash)
            if hit:
                return hit, True

        redactor = Redactor(enabled=self.redact)

        safe_ticket = redactor.scrub_mapping({
            **{k: v for k, v in ticket.items() if k != '_raw'},
        })
        safe_timeline = [redactor.scrub_mapping(event) for event in timeline]
        safe_similar = [redactor.scrub_mapping(item) for item in similar]
        safe_kb = [redactor.scrub_mapping(item) for item in kb_articles]

        prompt_signals = {k: v for k, v in signals.items() if not k.startswith('_')}

        user_prompt = build_user_prompt(
            ticket=safe_ticket,
            signals=prompt_signals,
            timeline=safe_timeline,
            similar=safe_similar,
            kb_articles=safe_kb,
            available_groups=self.valid_groups,
        )

        parsed, meta = self.llm.structured(SYSTEM_PROMPT, user_prompt, _SCHEMA)

        recommendation = sanitise(Recommendation(**parsed), self.valid_groups)

        # Restore redacted values so drafted text is usable as-is.
        recommendation.rationale = redactor.restore(recommendation.rationale)
        recommendation.draft_work_note = redactor.restore(recommendation.draft_work_note)
        recommendation.draft_caller_message = redactor.restore(recommendation.draft_caller_message)
        recommendation.lead_feedback = redactor.restore(recommendation.lead_feedback)

        row = self._persist(number, recommendation, input_hash, meta,
                            redactions=redactor.redaction_count)
        return row, False

    # -- persistence -------------------------------------------------------

    def _persist(self, number: str, rec: Recommendation, input_hash: str,
                 meta: Dict[str, Any], redactions: int = 0) -> Dict[str, Any]:
        # Anything below the confidence floor is stored but not pushed at
        # leads; it shows up in the UI under "needs a human look".
        surfaced = int(rec.confidence >= self.min_confidence)

        self.db.update('recommendation', {'superseded': 1},
                       conditions=[{'col': 'incident_number', 'op': 'eq', 'val': number},
                                   {'col': 'superseded', 'op': 'eq', 'val': 0}])

        record = {
            'incident_number': number,
            'created_at': datetime.datetime.now(),
            'model': meta.get('model'),
            'prompt_version': PROMPT_VERSION,
            'input_hash': input_hash,
            'recommended_action': rec.recommended_action.value,
            'confidence': round(float(rec.confidence), 3),
            'rationale': rec.rationale,
            'suggested_target_group': rec.suggested_target_group,
            'draft_work_note': rec.draft_work_note,
            'draft_caller_message': rec.draft_caller_message,
            'lead_feedback': rec.lead_feedback,
            'risk_flags': json.dumps(rec.risk_flags),
            'evidence': json.dumps([e.model_dump(mode='json') for e in rec.evidence]),
            'surfaced': surfaced,
            'superseded': 0,
            'latency_ms': meta.get('latency_ms'),
            'prompt_tokens': meta.get('prompt_tokens'),
            'completion_tokens': meta.get('completion_tokens'),
        }

        self.db.upsert('recommendation', record, update_columns=[
            'created_at', 'model', 'prompt_version', 'recommended_action', 'confidence',
            'rationale', 'suggested_target_group', 'draft_work_note',
            'draft_caller_message', 'lead_feedback', 'risk_flags', 'evidence',
            'surfaced', 'superseded', 'latency_ms', 'prompt_tokens', 'completion_tokens',
        ])

        stored = self.db.query_one(
            'SELECT * FROM recommendation WHERE incident_number = %s AND input_hash = %s',
            (number, input_hash))

        log.info('%s -> %s (confidence %.2f, %d redactions, %sms)',
                 number, rec.recommended_action.value, rec.confidence,
                 redactions, meta.get('latency_ms'))

        return stored or record

    # -- fallback ----------------------------------------------------------

    def deterministic_fallback(self, ticket: Dict[str, Any],
                               signals: Dict[str, Any]) -> Dict[str, Any]:
        """Used when the LLM is unavailable.

        Degraded but honest: the board still ranks and still says something
        sensible rather than showing an error where a recommendation was.
        """
        flags = signals.get('risk_flags') or []

        if signals.get('dependency_resolved'):
            action, reason = (RecommendedAction.RESOLVE,
                              'The blocking record has closed, so this ticket can move again.')
        elif signals.get('caller_replied_unanswered'):
            action, reason = (RecommendedAction.FOLLOW_UP_CALLER,
                              'The caller has replied and is awaiting a response from the agent.')
        elif signals.get('auto_close_candidate'):
            action, reason = (RecommendedAction.CLOSE_STALE,
                              'Follow-ups have been made and the caller has not responded.')
        elif signals.get('ball_in_court') == 'VENDOR':
            action, reason = (RecommendedAction.CHASE_VENDOR,
                              'The ticket is waiting on a third party.')
        elif 'CRITICALLY_STALE' in flags:
            action, reason = (RecommendedAction.ESCALATE,
                              'No agent activity for an extended period.')
        else:
            action, reason = (RecommendedAction.NO_ACTION_NEEDED,
                              'No deterministic trigger fired for this ticket.')

        return {
            'incident_number': ticket['incident_number'],
            'recommended_action': action.value,
            'confidence': 0.4,
            'rationale': reason + ' (rule-based fallback - the language model was unavailable.)',
            'suggested_target_group': None,
            'draft_work_note': '',
            'draft_caller_message': '',
            'lead_feedback': '',
            'risk_flags': json.dumps(flags),
            'evidence': json.dumps([]),
            'surfaced': 1,
            'model': 'rule-fallback',
            'created_at': datetime.datetime.now(),
        }
