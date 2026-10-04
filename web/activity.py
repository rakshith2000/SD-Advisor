"""Activity stream grouping for the ticket page.

ServiceNow records one audit row per field, so a single save of an incident
arrives here as a dozen separate events that all share a timestamp and a user.
Rendering those one per line is accurate and almost unreadable: creating an
incident produces twelve lines before anybody has done anything, and the one
line that matters - the work note - is pushed off the screen.

This module reassembles them the way ServiceNow's own activity stream does:
one card per save. Three kinds of card, because they are read for different
reasons and should not be skimmed as one list:

    Field changes       what the record now says
    Additional comments what the caller can see
    Work notes          what the team said internally

Only field changes are merged. Two journal entries written in the same second
remain two cards, because they are two things somebody wrote.

Presentation only. Nothing here feeds a signal, a score or the idle clock -
the grouping is derived from the timeline after the fact and thrown away when
the page is rendered.
"""

import re
from typing import Any, Dict, List, Optional, Sequence

CARD_FIELD_CHANGES = 'field_changes'
CARD_COMMENT = 'comment'
CARD_WORK_NOTE = 'work_note'

CARD_LABELS = {
    CARD_FIELD_CHANGES: 'Field changes',
    CARD_COMMENT: 'Additional comments',
    CARD_WORK_NOTE: 'Work notes',
}

COMMENT_FIELDS = {'comments', 'additional_comments'}
WORK_NOTE_FIELDS = {'work_notes'}

# Kept out of the field-changes card. The description is a paragraph rather
# than a value, and on the creation entry it alone is longer than every other
# row put together - which is the reason the grouped view is worth having.
# It is still on the ticket page under the heading, so nothing is lost.
HIDDEN_FIELDS = {'description'}

# ServiceNow's own labels where they differ from the column name. Anything not
# listed falls through to a readable form of the column, so a field added to
# the tracked set later still renders sensibly rather than disappearing.
FIELD_LABELS = {
    'assigned_to': 'Assigned to',
    'assignment_group': 'Assignment group',
    'business_service': 'Service',
    'caller_id': 'Caller',
    'category': 'Category',
    'close_code': 'Resolution code',
    'close_notes': 'Resolution notes',
    'closed_at': 'Closed',
    'closed_by': 'Closed by',
    'cmdb_ci': 'Configuration item',
    'contact_type': 'Contact type',
    'hold_reason': 'On hold reason',
    'impact': 'Impact',
    'incident_state': 'Incident state',
    'location': 'Location',
    'opened_at': 'Opened',
    'parent_incident': 'Parent incident',
    'priority': 'Priority',
    'problem_id': 'Problem',
    'reassignment_count': 'Reassignment count',
    'reopen_count': 'Reopen count',
    'resolved_at': 'Resolved',
    'resolved_by': 'Resolved by',
    'rfc': 'Change Request',
    'severity': 'Severity',
    'short_description': 'Short description',
    'state': 'State',
    'subcategory': 'Subcategory',
    'u_assigned_region': 'Assigned Region',
    'urgency': 'Urgency',
}

EMPTY_VALUE = '(empty)'

# Strict on purpose. A lenient parse would catch '3 - Medium' as a date in
# some locales and render a priority as a timestamp.
_TIMESTAMP = re.compile(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$')


def field_label(name: Optional[str]) -> str:
    """ServiceNow's label for a column, or a readable fallback.

    The fallback matters more than the table: every instance carries custom
    columns, and ours are the ones nobody can enumerate in advance. A leading
    `u_` is ServiceNow's convention for a customer-added field and is dropped,
    so u_assigned_region reads as a label rather than as a column name.
    """
    key = (name or '').strip().lower()
    if not key:
        return 'Field'
    if key in FIELD_LABELS:
        return FIELD_LABELS[key]

    cleaned = key[2:] if key.startswith('u_') else key
    cleaned = cleaned.replace('_', ' ').strip()
    if not cleaned:
        return 'Field'
    return cleaned[:1].upper() + cleaned[1:]


def initials(name: Optional[str]) -> str:
    """Two letters for the avatar. '?' rather than blank for an unnamed actor,
    so the circle never renders empty."""
    parts = [p for p in re.split(r'[\s._-]+', (name or '').strip()) if p]
    if not parts:
        return '?'
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()


def card_kind(field: Optional[str]) -> str:
    key = (field or '').strip().lower()
    if key in COMMENT_FIELDS:
        return CARD_COMMENT
    if key in WORK_NOTE_FIELDS:
        return CARD_WORK_NOTE
    return CARD_FIELD_CHANGES


def _is_timestamp(value: str) -> bool:
    return bool(_TIMESTAMP.match((value or '').strip()))


def group_activity(timeline: Sequence[Dict[str, Any]],
                   limit: int = 60) -> List[Dict[str, Any]]:
    """Collapse a flat timeline into ServiceNow-style cards, newest first.

    `timeline` is what pipeline.signals.build_timeline returns: oldest first,
    one entry per changed field. Adjacency is what makes the merge safe - the
    timeline is sorted by timestamp, so every event belonging to one save sits
    next to its siblings.
    """
    groups: List[Dict[str, Any]] = []

    for event in timeline:
        field = (event.get('field') or '').strip().lower()
        kind = card_kind(field)

        if kind == CARD_FIELD_CHANGES and field in HIDDEN_FIELDS:
            continue

        when = event.get('when') or ''
        actor = event.get('actor') or 'system'

        current = groups[-1] if groups else None
        mergeable = (kind == CARD_FIELD_CHANGES
                     and current is not None
                     and current['kind'] == CARD_FIELD_CHANGES
                     and current['when'] == when
                     and current['actor'] == actor)

        if not mergeable:
            current = {
                'kind': kind,
                'label': CARD_LABELS[kind],
                'when': when,
                'actor': actor,
                'initials': initials(actor),
                'role': event.get('role') or 'SYSTEM',
                'changes': [],
                'text': '',
            }
            groups.append(current)

        if kind == CARD_FIELD_CHANGES:
            value = str(event.get('new') or '').strip()
            previous = str(event.get('old') or '').strip()
            current['changes'].append({
                'field': field,
                'label': field_label(field),
                'value': value,
                'previous': previous,
                # Rendered through the display-timezone filter rather than
                # shown raw, so an 'Opened' row does not contradict every
                # other time on the page by being eight hours out.
                'is_timestamp': _is_timestamp(value),
                'previous_is_timestamp': _is_timestamp(previous),
            })
        else:
            current['text'] = event.get('text') or ''

    for group in groups:
        # Alphabetical, as ServiceNow lists them. The audit order is the order
        # the columns happen to sit in the table, which is meaningless to a
        # reader and different between two otherwise identical saves.
        group['changes'].sort(key=lambda change: change['label'].lower())

    groups.reverse()
    return groups[:limit] if limit else groups
