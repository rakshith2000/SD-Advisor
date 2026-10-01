"""Structured output contract for the advisor.

The existing audit tool asks the model to embed the literal string 'Boolean 1'
and then does a substring test on the prose. That fails open in both
directions ("this is not Boolean 1" scores as a pass) and gives no way to
express uncertainty. Here the model is constrained to a JSON schema, the
action is an enum, and confidence is explicit - so a bad answer is a wrong
enum value rather than an unparseable sentence.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class RecommendedAction(str, Enum):
    """Values are retained verbatim for compatibility with stored rows and the
    structured-output contract; only their presentation labels were
    formalised. See delivery.digest.ACTION_LABELS."""

    RESOLVE = 'RESOLVE'                      # sufficient evidence to resolve
    FOLLOW_UP_CALLER = 'FOLLOW_UP_CALLER'    # request information from the caller
    CHASE_VENDOR = 'CHASE_VENDOR'            # escalate to the third party
    REASSIGN = 'REASSIGN'                    # incorrect assignment group
    ESCALATE = 'ESCALATE'                    # requires lead or senior involvement
    AWAIT_DEPENDENCY = 'AWAIT_DEPENDENCY'    # legitimately awaiting a change or problem
    CLOSE_STALE = 'CLOSE_STALE'              # follow-ups exhausted, no caller response
    NO_ACTION_NEEDED = 'NO_ACTION_NEEDED'    # progressing satisfactorily


class PendingActionOwner(str, Enum):
    """The party responsible for the next action.

    SERVICE_DESK denotes the team rather than a named individual, because an
    unassigned incident is still Service Desk work.
    """
    SERVICE_DESK = 'SERVICE_DESK'
    CALLER = 'CALLER'
    VENDOR = 'VENDOR'
    CHANGE = 'CHANGE'
    PROBLEM = 'PROBLEM'
    APPROVAL = 'APPROVAL'
    NONE = 'NONE'


class EvidenceType(str, Enum):
    KB = 'kb'
    SIMILAR_INCIDENT = 'similar_incident'
    WORK_NOTE = 'work_note'
    SLA = 'sla'
    SIGNAL = 'signal'


class Evidence(BaseModel):
    type: EvidenceType
    ref: str = Field(description='KB number, INC number, or signal name')
    note: str = Field(default='', description='One line on why this supports the recommendation')


class Recommendation(BaseModel):
    """What the model must return for a single aged ticket."""

    recommended_action: RecommendedAction
    confidence: float = Field(ge=0.0, le=1.0)
    pending_action_owner: PendingActionOwner
    rationale: str = Field(description='Two to three sentences a lead can assess at a glance')
    evidence: List[Evidence] = Field(default_factory=list)
    suggested_target_group: Optional[str] = Field(
        default=None, description='Only when recommended_action is REASSIGN')
    draft_work_note: str = Field(
        default='', description='Ready-to-paste internal work note for the agent')
    draft_caller_message: str = Field(
        default='', description='Ready-to-send message to the caller, empty if not applicable')
    lead_feedback: str = Field(
        default='', description='A single coaching sentence for the lead to relay to the agent')
    risk_flags: List[str] = Field(default_factory=list)

    @field_validator('rationale')
    @classmethod
    def _rationale_present(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError('rationale must not be empty')
        return value.strip()

    @field_validator('risk_flags')
    @classmethod
    def _cap_flags(cls, value: List[str]) -> List[str]:
        return [str(v).strip().upper().replace(' ', '_') for v in value][:8]


def recommendation_json_schema() -> dict:
    """Schema in the shape Azure OpenAI structured outputs expects.

    additionalProperties must be false and every property must be required,
    which is stricter than pydantic's default export - hence the fixups.
    """
    schema = Recommendation.model_json_schema()
    _harden(schema)
    return {
        'name': 'aged_ticket_recommendation',
        'strict': True,
        'schema': schema,
    }


def _harden(node: object) -> None:
    """Recursively enforce the strict-mode requirements in place."""
    if isinstance(node, dict):
        if node.get('type') == 'object' or 'properties' in node:
            node['additionalProperties'] = False
            if 'properties' in node:
                node['required'] = list(node['properties'].keys())
        # strict mode rejects these annotations
        for key in ('default', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum'):
            node.pop(key, None)
        for value in node.values():
            _harden(value)
    elif isinstance(node, list):
        for item in node:
            _harden(item)


# --- Validation applied after the model responds -------------------------

def sanitise(rec: Recommendation, valid_groups: List[str]) -> Recommendation:
    """Post-validate model output against real-world constraints.

    This is the prompt-injection backstop: even if a ticket comment persuades
    the model to suggest a target group, that group has to exist in the
    customer's actual assignment-group list or it is dropped.
    """
    if rec.recommended_action != RecommendedAction.REASSIGN:
        rec.suggested_target_group = None
    elif rec.suggested_target_group:
        lowered = {g.lower(): g for g in valid_groups}
        match = lowered.get(rec.suggested_target_group.strip().lower())
        if match:
            rec.suggested_target_group = match
        else:
            # Unknown group - keep the advice but demote it to a human decision.
            rec.suggested_target_group = None
            rec.recommended_action = RecommendedAction.ESCALATE
            rec.confidence = min(rec.confidence, 0.5)
            rec.risk_flags = list(dict.fromkeys(rec.risk_flags + ['UNVERIFIED_TARGET_GROUP']))

    if rec.recommended_action == RecommendedAction.NO_ACTION_NEEDED:
        rec.draft_caller_message = ''

    return rec
