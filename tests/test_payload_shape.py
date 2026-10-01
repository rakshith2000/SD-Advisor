"""Tests that every reader survives a real sysparm_display_value=all payload.

The defect these exist for: switching the incident reads from
display_value=true to display_value=all changes the shape of EVERY field, not
only the datetimes. Under `all` a plain string such as `number` arrives as
{'display_value': 'INC123', 'value': 'INC123'}, so

    (row.get('number') or '').strip()

raises AttributeError: 'dict' object has no attribute 'strip'. The timestamp
handling was updated and the string handling was not, and the existing fakes
returned flat dicts, so the whole suite passed while sync was broken.

Every fixture here is shaped the way ServiceNow actually responds.
"""

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.snow.incidents import IncidentReader
from ops.index_builder import IndexBuilder
from pipeline.signals import evaluate_dependency
from pipeline.sync import flatten_incident

# US/Central is five hours behind UTC, matching the live integration user.
UTC = '2026-10-01 16:05:49'
LOCAL = '2026-10-01 11:05:49'


def plain(text):
    """A string field: both representations identical."""
    return {'display_value': text, 'value': text}


def reference(label, sys_id):
    """A reference field: a human label and a sys_id, plus an API link."""
    return {'display_value': label, 'value': sys_id,
            'link': f'https://example.service-now.com/api/now/table/x/{sys_id}'}


def choice(label, code):
    """A choice field: a label and its underlying code."""
    return {'display_value': label, 'value': code}


def moment(utc=UTC, local=LOCAL):
    """A datetime: UTC in value, the user's timezone in display_value."""
    return {'display_value': local, 'value': utc}


def incident_payload(**over):
    row = {
        'sys_id': plain('abc123def456'),
        'number': plain('INC0012345'),
        'short_description': plain('Laptop will not connect to VPN'),
        'description': plain('User cannot reach the corporate network.'),
        'caller_id': reference('Jamie Fox', 'cal111'),
        'assigned_to': reference('Asha Rao', 'usr222'),
        'assignment_group': reference('Service Desk', 'grp333'),
        'state': choice('In Progress', '2'),
        'hold_reason': choice('', ''),
        'priority': choice('3 - Moderate', '3'),
        'impact': choice('2 - Medium', '2'),
        'urgency': choice('3 - Low', '3'),
        'category': choice('Network', 'network'),
        'subcategory': choice('VPN', 'vpn'),
        'cmdb_ci': reference('VPN-GATEWAY-01', 'ci444'),
        'contact_type': choice('Phone', 'phone'),
        'rfc': plain(''),
        'problem_id': plain(''),
        'opened_at': moment('2026-06-08 15:28:40', '2026-06-08 10:28:40'),
        'sys_created_on': moment('2026-06-08 15:28:40', '2026-06-08 10:28:40'),
        'sys_updated_on': moment(),
        'reassignment_count': plain('2'),
        'reopen_count': plain('0'),
        'active': plain('true'),
        'resolved_at': plain(''),
        'closed_at': plain(''),
        'close_code': plain(''),
        'close_notes': plain(''),
    }
    row.update(over)
    return row


class TestFlattenIncident:
    def test_does_not_raise_on_an_all_shaped_payload(self):
        """The exact production failure."""
        assert flatten_incident(incident_payload())['incident_number'] == 'INC0012345'

    def test_every_value_is_a_scalar_not_a_dict(self):
        """A dict reaching a VARCHAR column is an insert error at best."""
        record = flatten_incident(incident_payload())
        offenders = {k: v for k, v in record.items() if isinstance(v, dict)}
        assert offenders == {}

    def test_strings_come_through_intact(self):
        record = flatten_incident(incident_payload())
        assert record['sys_id'] == 'abc123def456'
        assert record['short_description'] == 'Laptop will not connect to VPN'
        assert record['description'] == 'User cannot reach the corporate network.'

    def test_references_use_the_label_not_the_sys_id(self):
        record = flatten_incident(incident_payload())
        assert record['assignment_group'] == 'Service Desk'
        assert record['assigned_to'] == 'Asha Rao'
        assert record['caller_name'] == 'Jamie Fox'
        assert record['ci'] == 'VPN-GATEWAY-01'

    def test_the_caller_sys_id_is_captured_separately(self):
        assert flatten_incident(incident_payload())['caller_sys_id'] == 'cal111'

    def test_choices_use_the_label(self):
        record = flatten_incident(incident_payload())
        assert record['state'] == 'In Progress'
        assert record['priority'] == '3 - Moderate'

    def test_datetimes_use_the_utc_side(self):
        """The whole point of display_value=all: UTC, not the user's zone."""
        record = flatten_incident(incident_payload())
        assert record['sys_updated_on'] == datetime.datetime(2026, 10, 1, 16, 5, 49)
        assert record['opened_at'] == datetime.datetime(2026, 6, 8, 15, 28, 40)

    def test_the_display_hour_never_reaches_the_record(self):
        record = flatten_incident(incident_payload())
        assert record['sys_updated_on'].hour == 16      # UTC
        assert record['sys_updated_on'].hour != 11      # US/Central

    def test_counts_parse_from_the_dict_form(self):
        record = flatten_incident(incident_payload())
        assert record['reassignment_count'] == 2
        assert record['reopen_count'] == 0

    def test_empty_fields_do_not_become_the_string_none(self):
        record = flatten_incident(incident_payload())
        assert record['rfc'] == ''
        assert record['hold_reason'] == ''

    def test_opened_at_falls_back_to_sys_created_on(self):
        row = incident_payload(opened_at=plain(''))
        assert flatten_incident(row)['opened_at'] == datetime.datetime(2026, 6, 8, 15, 28, 40)

    def test_a_flat_payload_still_works(self):
        """Backward compatibility: display_value=true responses must not break."""
        record = flatten_incident({
            'number': 'INC0012345', 'sys_id': 'abc', 'short_description': 'x',
            'state': 'In Progress', 'opened_at': '2026-06-08 15:28:40',
            'sys_updated_on': '2026-10-01 16:05:49',
        })
        assert record['incident_number'] == 'INC0012345'
        assert record['opened_at'] == datetime.datetime(2026, 6, 8, 15, 28, 40)


class FakeClient:
    def __init__(self, rows):
        self.rows = rows

    def get(self, table, params):
        return list(self.rows)

    def get_all(self, table, params, max_records=None):
        return list(self.rows)


class TestHistory:
    def test_update_time_is_utc_and_fields_are_scalars(self):
        reader = IncidentReader(FakeClient([{
            'user_name': plain('asha.rao'),
            'update_time': moment(),
            'field': plain('work_notes'),
            'new': plain('Called the user back'),
            'old': plain(''),
        }]), [])
        event = reader.get_history('abc')[0]

        assert not any(isinstance(v, dict) for v in event.values())
        assert event['update_time'] == UTC        # not the 11:05 display value
        assert event['user_name'] == 'asha.rao'
        assert event['field'] == 'work_notes'
        assert event['new'] == 'Called the user back'

    def test_duplicate_lines_are_still_deduped(self):
        line = {'user_name': plain('asha.rao'), 'update_time': moment(),
                'field': plain('state'), 'new': plain('2'), 'old': plain('1')}
        reader = IncidentReader(FakeClient([line, dict(line)]), [])
        assert len(reader.get_history('abc')) == 1


class TestRelatedRecords:
    def test_change_state_is_flattened(self):
        reader = IncidentReader(FakeClient([{
            'number': plain('CHG0044321'),
            'state': choice('Closed Complete', '3'),
            'short_description': plain('Firewall rule change'),
            'close_code': choice('Successful', 'successful'),
            'end_date': moment(),
            'active': plain('false'),
        }]), [])
        record = reader.get_change_state('CHG0044321')

        assert not any(isinstance(v, dict) for v in record.values())
        assert record['state'] == 'Closed Complete'
        assert record['number'] == 'CHG0044321'
        assert record['end_date'] == UTC

    def test_a_flattened_change_drives_the_dependency_signal(self):
        """End to end: the reader's output must satisfy evaluate_dependency,
        which compares state against RESOLVED_DEPENDENCY_STATES in lower case.
        An unflattened dict would stringify to something that never matches."""
        reader = IncidentReader(FakeClient([{
            'number': plain('CHG0044321'),
            'state': choice('Closed Complete', '3'),
            'short_description': plain('Firewall rule change'),
            'close_code': choice('Successful', 'successful'),
            'end_date': moment(),
            'active': plain('false'),
        }]), [])
        result = evaluate_dependency(
            {'hold_reason': 'Awaiting Change'},
            change_record=reader.get_change_state('CHG0044321'),
            problem_record=None)

        assert result['dependency_resolved'] is True
        assert result['dependency_ref'] == 'CHG0044321'
        assert result['dependency_state'] == 'Closed Complete'

    def test_problem_state_is_flattened(self):
        reader = IncidentReader(FakeClient([{
            'number': plain('PRB0001234'),
            'state': choice('Resolved', '6'),
            'short_description': plain('Recurring VPN drops'),
            'resolution_code': choice('Fix Applied', 'fix_applied'),
            'active': plain('false'),
        }]), [])
        record = reader.get_problem_state('PRB0001234')
        assert not any(isinstance(v, dict) for v in record.values())
        assert record['state'] == 'Resolved'

    def test_missing_record_is_none(self):
        reader = IncidentReader(FakeClient([]), [])
        assert reader.get_change_state('CHG0000000') is None
        assert reader.get_problem_state('PRB0000000') is None


class FakeContext:
    def __init__(self, rows):
        self.incidents = IncidentReader(FakeClient(rows), [])
        self.db = None
        self.index = None
        self.llm = None


class TestIndexBuilderPayload:
    def resolved_row(self):
        return incident_payload(
            state=choice('Resolved', '6'),
            resolved_at=moment('2026-06-10 15:28:40', '2026-06-10 10:28:40'),
            close_code=choice('Solution provided', 'solution_provided'),
            close_notes=plain('Reissued the VPN certificate and confirmed with the user.'),
        )

    def test_fetch_resolved_handles_the_all_shape(self):
        records = IndexBuilder(FakeContext([self.resolved_row()])).fetch_resolved(180)
        assert len(records) == 1
        record = records[0]
        assert not any(isinstance(v, dict) for v in record.values())
        assert record['number'] == 'INC0012345'
        assert record['category'] == 'Network'
        assert record['close_notes'].startswith('Reissued')

    def test_the_ci_field_is_the_ci_not_the_assignment_group(self):
        """A prior defect: cmdb_ci was populated from assignment_group."""
        record = IndexBuilder(FakeContext([self.resolved_row()])).fetch_resolved(180)[0]
        assert record['ci'] == 'VPN-GATEWAY-01'
        assert record['assignment_group'] == 'Service Desk'

    def test_resolution_hours_computed_from_utc_not_display(self):
        """Both stamps are five hours off in display_value; taking one side
        from each would produce a five-hour error in the duration."""
        record = IndexBuilder(FakeContext([self.resolved_row()])).fetch_resolved(180)[0]
        assert record['resolution_hours'] == pytest.approx(48.0)

    def test_a_row_with_no_number_is_dropped(self):
        row = self.resolved_row()
        row['number'] = plain('')
        assert IndexBuilder(FakeContext([row])).fetch_resolved(180) == []
