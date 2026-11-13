"""Incident, history and related-record reads."""

import datetime
import re
from typing import Any, Dict, List, Optional, Sequence

from core.logging_setup import get_logger
from core.snow.base import (DISPLAY_AND_VALUE, ServiceNowClient,
                            display_value, query_ts, utc_value)
from core.timeutil import utc_now

log = get_logger('core.snow.incidents')

INCIDENT_FIELDS = ','.join([
    'sys_id', 'number', 'caller_id', 'opened_by', 'sys_created_by',
    'sys_created_on', 'opened_at', 'sys_updated_on', 'state', 'hold_reason',
    'assigned_to', 'assignment_group', 'cmdb_ci', 'impact', 'urgency', 'priority',
    'category', 'subcategory', 'u_function', 'business_service', 'contact_type',
    'short_description', 'description', 'comments', 'work_notes',
    'rfc', 'problem_id', 'parent_incident', 'child_incidents',
    'reopen_count', 'reassignment_count', 'active', 'location',
    'resolved_at', 'resolved_by', 'close_code', 'close_notes',
])

# ServiceNow incident state values: 1 New, 2 In Progress, 3 On Hold,
# 6 Resolved, 7 Closed, 8 Canceled.
OPEN_STATES = '1,2,3'
CLOSED_STATES = '6,7'

IMAGE_CONTENT_TYPES = {
    'image/avif', 'image/bmp', 'image/gif', 'image/jpeg',
    'image/png', 'image/webp',
}


class IncidentReader:
    def __init__(self, client: ServiceNowClient, assignment_groups: Optional[List[str]] = None):
        self.client = client
        self.assignment_groups = [g for g in (assignment_groups or []) if g.strip()]

    # -- scope helpers -----------------------------------------------------

    def _group_clause(self) -> str:
        """Scope to the configured queues, dot-walking to the group's name.

        assignment_group stores a sys_id, so `assignment_groupIN Desk Name`
        compares a 32-character hex string against a display name and matches
        nothing, whatever the name is. Dot-walking with `assignment_group.name`
        joins to sys_user_group and compares the name for real.

        (LIKE is the exception - CONTAINS on a reference field resolves to the
        display value - which is why a substring probe appears to work while
        the equality form silently returns nothing.)
        """
        if not self.assignment_groups:
            return ''
        return '^assignment_group.nameIN' + ','.join(self.assignment_groups)

    def unresolvable_groups(self) -> List[str]:
        """Configured group names with no exactly-matching active sys_user_group.

        The scope clause matches on the exact name. A near-miss - 'Service
        Desk' where the group is really 'IT Service Desk' - narrows the
        incident query to nothing and returns HTTP 200 with an empty result,
        so it presents as "no tickets today" rather than as an error. Worth
        one extra call at startup to turn that into a loud failure.
        """
        if not self.assignment_groups:
            return []

        rows = self.client.get('sys_user_group', {
            'sysparm_query': 'active=true^nameIN' + ','.join(self.assignment_groups),
            'sysparm_fields': 'name',
            'sysparm_display_value': DISPLAY_AND_VALUE,
            'sysparm_limit': len(self.assignment_groups) + 10,
        })
        found = {display_value(r.get('name')) for r in rows}
        return [g for g in self.assignment_groups if g not in found]

    # -- aged backlog ------------------------------------------------------

    def get_aged_open_incidents(self, aged_after_days: int,
                                now: Optional[datetime.datetime] = None) -> List[Dict[str, Any]]:
        """Open, in-scope incidents opened more than N days ago."""
        now = now or utc_now()
        cutoff = now - datetime.timedelta(days=aged_after_days)

        query = (
            f'active=true'
            f'^stateIN{OPEN_STATES}'
            f'^opened_at<{query_ts(cutoff)}'
            f'{self._group_clause()}'
        )

        return self.client.get_all('incident', {
            'sysparm_query': query,
            'sysparm_fields': INCIDENT_FIELDS,
            'sysparm_display_value': DISPLAY_AND_VALUE,
        })

    def get_changed_since(self, watermark: datetime.datetime) -> List[Dict[str, Any]]:
        """Delta poll: any in-scope open incident touched since the watermark.

        This is the near-real-time feed. It intentionally ignores the age
        threshold so a ticket crossing day 5 is already in our snapshot.
        """
        query = (
            f'active=true'
            f'^stateIN{OPEN_STATES}'
            f'^sys_updated_on>{query_ts(watermark)}'
            f'{self._group_clause()}'
        )

        return self.client.get_all('incident', {
            'sysparm_query': query,
            'sysparm_fields': INCIDENT_FIELDS,
            'sysparm_display_value': DISPLAY_AND_VALUE,
            'sysparm_query_order': 'sys_updated_on',
        })

    def get_by_number(self, number: str) -> Optional[Dict[str, Any]]:
        rows = self.client.get('incident', {
            'sysparm_query': f'number={number}',
            'sysparm_fields': INCIDENT_FIELDS,
            'sysparm_display_value': DISPLAY_AND_VALUE,
            'sysparm_limit': 1,
        })
        return rows[0] if rows else None

    def get_closed_between(self, start: datetime.datetime,
                           end: datetime.datetime) -> List[Dict[str, Any]]:
        """Resolved incidents, used to build the similar-incident index."""
        query = (
            f'stateIN{CLOSED_STATES}'
            f'^resolved_atBETWEEN{query_ts(start)}@{query_ts(end)}'
            f'{self._group_clause()}'
        )
        return self.client.get_all('incident', {
            'sysparm_query': query,
            'sysparm_fields': INCIDENT_FIELDS,
            'sysparm_display_value': DISPLAY_AND_VALUE,
        })

    def sample_resolved_within(self, days: int, limit: int = 1,
                               now: Optional[datetime.datetime] = None
                               ) -> List[Dict[str, Any]]:
        """Cheap existence probe for the backfill window - one page, no paging.

        Lets `doctor` distinguish "the backfill indexed nothing because there is
        nothing to index" from "the backfill indexed nothing because the query
        is wrong", which a zero return value alone cannot.

        Deliberately mirrors get_closed_between's clause shape rather than using
        the shorter RELATIVEGE form: an unparseable condition is *dropped* by
        ServiceNow, not rejected, so a probe built on syntax the instance does
        not recognise would match every record and always pass.
        """
        now = now or utc_now()
        cutoff = now - datetime.timedelta(days=days)
        return self.client.get('incident', {
            'sysparm_query': (f'stateIN{CLOSED_STATES}'
                              f'^resolved_at>={query_ts(cutoff)}'
                              f'{self._group_clause()}'),
            'sysparm_fields': 'number',
            'sysparm_limit': limit,
        })

    # -- history -----------------------------------------------------------

    def get_history(self, sys_id: str) -> List[Dict[str, Any]]:
        """Field-level audit trail, deduped and sorted oldest first.

        ServiceNow emits duplicate history lines when several fields change in
        one update; the fingerprint drops exact repeats without losing genuine
        repeated transitions (those differ by update_time).
        """
        rows = self.client.get_all('sys_history_line', {
            'sysparm_query': f'set.id={sys_id}',
            'sysparm_display_value': DISPLAY_AND_VALUE,
        })

        history: List[Dict[str, Any]] = []
        seen = set()

        for event in rows:
            entry = {
                'user_name': display_value(event.get('user_name')),
                # UTC, so it can be compared against utc_now(). display_value
                # here would be the integration user's timezone AND date
                # format, which pipeline.signals cannot safely compare.
                'update_time': utc_value(event.get('update_time')),
                'field': display_value(event.get('field')),
                'new': display_value(event.get('new')),
                'old': display_value(event.get('old')),
            }
            fingerprint = (
                entry['user_name'], entry['update_time'],
                entry['field'], str(entry['new']), str(entry['old']),
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            history.append(entry)

        history.sort(key=lambda e: e.get('update_time') or '')
        return history

    def get_image_attachments(self, sys_id: str) -> List[Dict[str, str]]:
        """Image attachments belonging to this incident, newest first."""
        if not re.fullmatch(r'[0-9a-fA-F]{32}', sys_id or ''):
            raise ValueError('sys_id must be a ServiceNow sys_id')

        rows = self.client.get_all('sys_attachment', {
            'sysparm_query': f'table_name=incident^table_sys_id={sys_id}',
            'sysparm_fields': 'sys_id,file_name,content_type,sys_created_on,sys_created_by',
            'sysparm_display_value': DISPLAY_AND_VALUE,
        })

        attachments = []
        for row in rows:
            attachment = self._image_attachment(row)
            if attachment is not None:
                attachments.append(attachment)
        attachments.sort(key=lambda item: item['created_at'], reverse=True)
        return attachments

    def get_image_attachment(self, sys_id: str,
                             attachment_id: str) -> Optional[Dict[str, str]]:
        """Return one supported image only when it belongs to this incident."""
        for value, name in ((sys_id, 'sys_id'), (attachment_id, 'attachment_id')):
            if not re.fullmatch(r'[0-9a-fA-F]{32}', value or ''):
                raise ValueError(f'{name} must be a ServiceNow sys_id')

        rows = self.client.get('sys_attachment', {
            'sysparm_query': (f'sys_id={attachment_id}^table_name=incident'
                              f'^table_sys_id={sys_id}'),
            'sysparm_fields': 'sys_id,file_name,content_type,sys_created_on,sys_created_by',
            'sysparm_display_value': DISPLAY_AND_VALUE,
            'sysparm_limit': 1,
        })
        return self._image_attachment(rows[0]) if rows else None

    @staticmethod
    def _image_attachment(row: Dict[str, Any]) -> Optional[Dict[str, str]]:
        content_type = IncidentReader._attachment_field(row.get('content_type'))
        content_type = content_type.split(';', 1)[0].strip().lower()
        attachment_id = IncidentReader._attachment_field(row.get('sys_id'))
        if (content_type not in IMAGE_CONTENT_TYPES
                or not re.fullmatch(r'[0-9a-fA-F]{32}', attachment_id)):
            return None
        return {
            'sys_id': attachment_id,
            'file_name': (IncidentReader._attachment_field(row.get('file_name'))
                          or 'Image attachment'),
            'content_type': content_type,
            'created_at': utc_value(row.get('sys_created_on')),
            'created_by': (IncidentReader._attachment_field(row.get('sys_created_by'))
                           or 'system'),
        }

    @staticmethod
    def _attachment_field(value: Any) -> str:
        """Flatten scalar or display_value=all attachment metadata."""
        if isinstance(value, dict):
            value = value.get('display_value') or value.get('value') or ''
        return str(value or '').strip()

    # -- related records ---------------------------------------------------

    @staticmethod
    def _flatten_related(row: Dict[str, Any], fields: Sequence[str],
                         timestamps: Sequence[str] = ()) -> Dict[str, Any]:
        """Collapse a dependency record to plain strings.

        Flattened here rather than at the point of use so pipeline.signals
        stays free of ServiceNow payload shapes.
        """
        flat = {f: display_value(row.get(f)) for f in fields}
        flat.update({f: utc_value(row.get(f)) for f in timestamps})
        return flat

    def get_change_state(self, number: str) -> Optional[Dict[str, Any]]:
        rows = self.client.get('change_request', {
            'sysparm_query': f'number={number}',
            'sysparm_fields': 'number,state,short_description,close_code,end_date,active',
            'sysparm_display_value': DISPLAY_AND_VALUE,
            'sysparm_limit': 1,
        })
        if not rows:
            return None
        return self._flatten_related(
            rows[0], ('number', 'state', 'short_description', 'close_code', 'active'),
            timestamps=('end_date',))

    def get_problem_state(self, number: str) -> Optional[Dict[str, Any]]:
        rows = self.client.get('problem', {
            'sysparm_query': f'number={number}',
            'sysparm_fields': 'number,state,short_description,resolution_code,active',
            'sysparm_display_value': DISPLAY_AND_VALUE,
            'sysparm_limit': 1,
        })
        if not rows:
            return None
        return self._flatten_related(
            rows[0], ('number', 'state', 'short_description', 'resolution_code', 'active'))

    def get_attached_kb(self, sys_id: str) -> List[Dict[str, Any]]:
        return self.client.get('m2m_kb_task', {
            'sysparm_query': f'task={sys_id}',
            'sysparm_fields': 'kb_knowledge,task,kb_knowledge.short_description',
            'sysparm_display_value': DISPLAY_AND_VALUE,
        })
