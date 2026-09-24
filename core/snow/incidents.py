"""Incident, history and related-record reads."""

import datetime
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from core.snow.base import ServiceNowClient, display_value, js_date

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


class IncidentReader:
    def __init__(self, client: ServiceNowClient, assignment_groups: Optional[List[str]] = None):
        self.client = client
        self.assignment_groups = [g for g in (assignment_groups or []) if g.strip()]

    # -- scope helpers -----------------------------------------------------

    def _group_clause(self) -> str:
        if not self.assignment_groups:
            return ''
        return '^assignment_groupIN' + ','.join(self.assignment_groups)

    def unresolvable_groups(self) -> List[str]:
        """Configured group names with no exactly-matching active sys_user_group.

        assignment_groupIN matches on the exact name. A near-miss - 'Service
        Desk' where the group is really 'IT Service Desk' - narrows every
        incident query to nothing and returns HTTP 200 with an empty result,
        so it presents as "no tickets today" rather than as an error. Worth
        one extra call at startup to turn that into a loud failure.
        """
        if not self.assignment_groups:
            return []

        rows = self.client.get('sys_user_group', {
            'sysparm_query': 'active=true^nameIN' + ','.join(self.assignment_groups),
            'sysparm_fields': 'name',
            'sysparm_display_value': 'true',
            'sysparm_limit': len(self.assignment_groups) + 10,
        })
        found = {display_value(r.get('name')) for r in rows}
        return [g for g in self.assignment_groups if g not in found]

    # -- aged backlog ------------------------------------------------------

    def get_aged_open_incidents(self, aged_after_days: int,
                                now: Optional[datetime.datetime] = None) -> List[Dict[str, Any]]:
        """Open, in-scope incidents opened more than N days ago."""
        now = now or datetime.datetime.now()
        cutoff = now - datetime.timedelta(days=aged_after_days)

        query = (
            f'active=true'
            f'^stateIN{OPEN_STATES}'
            f'^opened_at<{js_date(cutoff)}'
            f'{self._group_clause()}'
        )

        return self.client.get_all('incident', {
            'sysparm_query': query,
            'sysparm_fields': INCIDENT_FIELDS,
            'sysparm_display_value': 'true',
        })

    def get_changed_since(self, watermark: datetime.datetime) -> List[Dict[str, Any]]:
        """Delta poll: any in-scope open incident touched since the watermark.

        This is the near-real-time feed. It intentionally ignores the age
        threshold so a ticket crossing day 5 is already in our snapshot.
        """
        query = (
            f'active=true'
            f'^stateIN{OPEN_STATES}'
            f'^sys_updated_on>{js_date(watermark)}'
            f'{self._group_clause()}'
        )

        return self.client.get_all('incident', {
            'sysparm_query': query,
            'sysparm_fields': INCIDENT_FIELDS,
            'sysparm_display_value': 'true',
            'sysparm_query_order': 'sys_updated_on',
        })

    def get_by_number(self, number: str) -> Optional[Dict[str, Any]]:
        rows = self.client.get('incident', {
            'sysparm_query': f'number={number}',
            'sysparm_fields': INCIDENT_FIELDS,
            'sysparm_display_value': 'true',
            'sysparm_limit': 1,
        })
        return rows[0] if rows else None

    def get_closed_between(self, start: datetime.datetime,
                           end: datetime.datetime) -> List[Dict[str, Any]]:
        """Resolved incidents, used to build the similar-incident index."""
        query = (
            f'stateIN{CLOSED_STATES}'
            f'^resolved_atBETWEEN{js_date(start)}@{js_date(end)}'
            f'{self._group_clause()}'
        )
        return self.client.get_all('incident', {
            'sysparm_query': query,
            'sysparm_fields': INCIDENT_FIELDS,
            'sysparm_display_value': 'true',
        })

    def sample_resolved_within(self, days: int, limit: int = 1) -> List[Dict[str, Any]]:
        """Cheap existence probe for the backfill window - one page, no paging.

        Lets `doctor` distinguish "the backfill indexed nothing because there is
        nothing to index" from "the backfill indexed nothing because the query
        is wrong", which a zero return value alone cannot.
        """
        return self.client.get('incident', {
            'sysparm_query': (f'stateIN{CLOSED_STATES}'
                              f'^resolved_atRELATIVEGE@day@ago@{int(days)}'
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
            'sysparm_display_value': 'true',
        })

        history: List[Dict[str, Any]] = []
        seen = set()

        for event in rows:
            entry = {
                'user_name': (event.get('user_name') or '').strip(),
                'update_time': (event.get('update_time') or '').strip(),
                'field': (event.get('field') or '').strip(),
                'new': event.get('new') or '',
                'old': event.get('old') or '',
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

    # -- related records ---------------------------------------------------

    def get_change_state(self, number: str) -> Optional[Dict[str, Any]]:
        rows = self.client.get('change_request', {
            'sysparm_query': f'number={number}',
            'sysparm_fields': 'number,state,short_description,close_code,end_date,active',
            'sysparm_display_value': 'true',
            'sysparm_limit': 1,
        })
        return rows[0] if rows else None

    def get_problem_state(self, number: str) -> Optional[Dict[str, Any]]:
        rows = self.client.get('problem', {
            'sysparm_query': f'number={number}',
            'sysparm_fields': 'number,state,short_description,resolution_code,active',
            'sysparm_display_value': 'true',
            'sysparm_limit': 1,
        })
        return rows[0] if rows else None

    def get_attached_kb(self, sys_id: str) -> List[Dict[str, Any]]:
        return self.client.get('m2m_kb_task', {
            'sysparm_query': f'task={sys_id}',
            'sysparm_fields': 'kb_knowledge,task,kb_knowledge.short_description',
            'sysparm_display_value': 'true',
        })
