"""Tests for SLA classification and summarisation.

Fixtures use the real Kohler contract_sla definitions, because the whole point
of the explicit map is that it matches the instance it was built for. If
someone renames a definition in ServiceNow these tests still pass (sys_id
keyed) - which is the property we want.

SLA state feeds sla_jeopardy, the joint-largest component of the attention
score, so a misclassification here silently reorders the entire board.
"""

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.snow.sla import (IGNORE, OTHER, RESOLUTION, RESPONSE, VENDOR,
                           SlaReader, SlaRecord, _duration_to_minutes,
                           classify_by_name)

# The real definitions, as returned by contract_sla on the Kohler instance.
P1_RESP = '6a3dab1e1b60e410f8eb0f6e6e4bcba2'
P1_INC_RESP = '342465c72b2a4350793af829ce91bf75'
P2_RESP = '481eab9a1b60e410f8eb0f6e6e4bcb02'
P3_RESP = '288ee35e1b60e410f8eb0f6e6e4bcb76'
P4_RESP = '3eeea35e1b60e410f8eb0f6e6e4bcb86'
P1_RESL = 'c1e472d21beca410f8eb0f6e6e4bcb65'
P1_MAJOR_RESL = '0d8ec5072be24350793af829ce91bf83'
P2_RESL = 'd606b61a1beca410f8eb0f6e6e4bcb9b'
P3_RESL = '4edbfeda1beca410f8eb0f6e6e4bcbe2'
P4_RESL = 'ffbc3e1e1beca410f8eb0f6e6e4bcbae'
VENDOR_RESL = '10b7e3ab1be47090f8eb0f6e6e4bcb13'
IAR = '4a0a1f0dff03211001b9ffffffffff78'

P2_INC_RESP = '7d4429c72b2a4350793af829ce91bf34'

# Durations are the production values as at 2026-09-24. They are documentation
# for the scorer but load-bearing for sla-map's drift check.
DEFINITIONS = {
    P1_RESP: {'kind': 'RESPONSE', 'name': 'Priority 1(Critical) Response',
              'duration': '15 Minutes'},
    P1_INC_RESP: {'kind': 'RESPONSE', 'name': 'Priority 1(Critical) Incident Response',
                  'duration': '15 Minutes'},
    P2_RESP: {'kind': 'RESPONSE', 'name': 'Priority 2 (High) Response',
              'duration': '30 Minutes'},
    P2_INC_RESP: {'kind': 'RESPONSE', 'name': 'Priority 2 (Critical) Incident Response',
                  'duration': '30 Minutes'},
    P3_RESP: {'kind': 'RESPONSE', 'name': 'Priority 3 (Medium) Response',
              'duration': '4 Hours'},
    P4_RESP: {'kind': 'RESPONSE', 'name': 'Priority 4 (Low) Response',
              'duration': '1 Day'},
    P1_RESL: {'kind': 'RESOLUTION', 'name': 'Priority 1 (Critical) Resolution',
              'duration': '4 Hours'},
    P1_MAJOR_RESL: {'kind': 'RESOLUTION', 'name': 'P1 (Major Incident) Resolution',
                    'duration': '4 Hours'},
    P2_RESL: {'kind': 'RESOLUTION', 'name': 'Priority 2 (High) Resolution',
              'duration': '6 Hours'},
    P3_RESL: {'kind': 'RESOLUTION', 'name': 'Priority 3 (Medium) Resolution',
              'duration': '2 Days'},
    P4_RESL: {'kind': 'RESOLUTION', 'name': 'Priority 4 (Low) Resolution',
              'duration': '3 Days'},
    VENDOR_RESL: {'kind': 'VENDOR', 'name': 'Vendor Resolution',
                  'duration': '10 Days'},
    IAR: {'kind': 'IGNORE', 'name': 'ITSM IAR SLA', 'duration': '1 Day'},
}


class FakeSettings:
    def __init__(self, values):
        self._values = values

    def get(self, dotted, default=None):
        return self._values.get(dotted, default)


def reader(definitions=None, include_types=('SLA',)):
    return SlaReader(client=None, settings=FakeSettings({
        'sla.definitions': DEFINITIONS if definitions is None else definitions,
        'sla.include_types': list(include_types),
    }))


def record(kind, *, breached=False, minutes_left=600.0, pct=50.0,
           stage='in_progress', active=True, name='x', sys_id='x'):
    return SlaRecord(kind=kind, name=name, sys_id=sys_id, stage=stage,
                     has_breached=breached, percentage=pct,
                     time_left_minutes=minutes_left,
                     planned_end=datetime.datetime(2026, 9, 30, 12, 0),
                     active=active)


# ---------------------------------------------------------------------------
# duration parsing
# ---------------------------------------------------------------------------

class TestDurationParsing:
    def test_matches_the_servicenow_display_value(self):
        """'1970-01-03 22:44:51' is the real payload for '2 Days 22 Hours
        44 Minutes'. 2*1440 + 22*60 + 44 + 51/60 = 4244.85."""
        assert _duration_to_minutes('1970-01-03 22:44:51') == pytest.approx(4244.85)

    def test_epoch_is_zero(self):
        assert _duration_to_minutes('1970-01-01 00:00:00') == 0.0

    def test_blank_is_none(self):
        assert _duration_to_minutes('') is None
        assert _duration_to_minutes(None) is None


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

class TestClassification:
    @pytest.mark.parametrize('sys_id,expected', [
        (P1_RESP, RESPONSE), (P1_INC_RESP, RESPONSE), (P2_RESP, RESPONSE),
        (P3_RESP, RESPONSE), (P4_RESP, RESPONSE),
        (P1_RESL, RESOLUTION), (P1_MAJOR_RESL, RESOLUTION), (P2_RESL, RESOLUTION),
        (P3_RESL, RESOLUTION), (P4_RESL, RESOLUTION),
        (VENDOR_RESL, VENDOR), (IAR, IGNORE),
    ])
    def test_every_real_definition_classifies(self, sys_id, expected):
        assert reader().classify(sys_id, DEFINITIONS[sys_id]['name']) == expected

    def test_config_wins_over_the_name(self):
        """'Vendor Resolution' contains 'resolution', so the token fallback
        would call it RESOLUTION. The explicit map must override that."""
        assert classify_by_name('Vendor Resolution') == RESOLUTION
        assert reader().classify(VENDOR_RESL, 'Vendor Resolution') == VENDOR

    def test_iar_is_ignored_despite_an_unhelpful_name(self):
        assert classify_by_name('ITSM IAR SLA') == OTHER
        assert reader().classify(IAR, 'ITSM IAR SLA') == IGNORE

    def test_rename_in_servicenow_does_not_change_the_kind(self):
        assert reader().classify(VENDOR_RESL, 'Completely Different Name') == VENDOR

    def test_name_lookup_when_the_sys_id_is_unknown(self):
        assert reader().classify('', 'Priority 3 (Medium) Resolution') == RESOLUTION

    def test_unconfigured_definition_falls_back_to_tokens(self):
        r = reader(definitions={})
        assert r.classify('new-sys-id', 'Priority 5 Resolution') == RESOLUTION
        assert r.classify('new-sys-id', 'Priority 5 Response') == RESPONSE

    def test_wholly_unrecognised_definition_is_other(self):
        assert reader(definitions={}).classify('zz', 'Gold Tier Target') == OTHER

    def test_source_is_reported_for_verification(self):
        assert reader().classify_with_source(P3_RESL, 'x')[1] == 'config:sys_id'
        assert reader().classify_with_source('', 'Vendor Resolution')[1] == 'config:name'
        assert reader(definitions={}).classify_with_source(
            'q', 'Something Resolution')[1] == 'name-token'
        assert reader(definitions={}).classify_with_source('q', 'Gold Tier')[1] == 'unmatched'

    def test_invalid_kind_in_config_is_dropped_not_fatal(self):
        r = reader(definitions={'abc': {'kind': 'NONSENSE', 'name': 'Bad'}})
        assert r.classify('abc', 'Something Resolution') == RESOLUTION   # fell through

    def test_shorthand_string_form_is_accepted(self):
        r = reader(definitions={'abc': 'VENDOR'})
        assert r.classify('abc', 'anything') == VENDOR


# ---------------------------------------------------------------------------
# query construction
# ---------------------------------------------------------------------------

class TestQuery:
    def test_type_filter_dot_walks_to_contract_sla(self):
        assert reader()._task_query('INC1') == 'task.number=INC1^sla.typeINSLA'

    def test_multiple_types(self):
        q = reader(include_types=('SLA', 'OLA'))._task_query('INC1')
        assert q == 'task.number=INC1^sla.typeINSLA,OLA'

    def test_no_filter_when_unconfigured(self):
        assert reader(include_types=())._task_query('INC1') == 'task.number=INC1'


# ---------------------------------------------------------------------------
# live-record preference
# ---------------------------------------------------------------------------

class TestLiveRecords:
    def test_active_flag_makes_a_record_live(self):
        assert record(RESOLUTION, active=True, stage='completed').is_live is True

    def test_stage_in_progress_makes_a_record_live(self):
        assert record(RESOLUTION, active=False, stage='in_progress').is_live is True

    def test_display_form_of_the_stage_also_works(self):
        assert record(RESOLUTION, active=False, stage='In Progress').is_live is True

    def test_completed_and_inactive_is_not_live(self):
        assert record(RESOLUTION, active=False, stage='completed').is_live is False

    def test_superseded_breach_does_not_mask_a_healthy_current_sla(self):
        """The priority-change case: ServiceNow cancels the old task_sla and
        creates a new one. Without preferring live records, the cancelled
        breached record would report the ticket as breached."""
        summary = SlaReader.summarise([
            record(RESOLUTION, breached=True, minutes_left=-500.0,
                   stage='cancelled', active=False, name='old P2 Resolution'),
            record(RESOLUTION, breached=False, minutes_left=900.0,
                   stage='in_progress', active=True, name='new P3 Resolution'),
        ])
        assert summary['sla_breached'] is False
        assert summary['sla_time_left_mins'] == 900.0

    def test_falls_back_to_completed_when_nothing_is_live(self):
        summary = SlaReader.summarise([
            record(RESOLUTION, breached=True, stage='completed', active=False),
        ])
        assert summary['sla_breached'] is True


# ---------------------------------------------------------------------------
# summarise precedence
# ---------------------------------------------------------------------------

class TestSummarise:
    def test_ignored_records_are_dropped_entirely(self):
        summary = SlaReader.summarise([
            record(IGNORE, breached=True, minutes_left=-100.0, name='ITSM IAR SLA'),
        ])
        assert summary['sla_breached'] is False
        assert summary['sla_pct_consumed'] is None

    def test_ignored_record_is_not_used_as_a_resolution_proxy(self):
        """Before the explicit map, IAR classified as OTHER and could stand in
        for a missing resolution SLA."""
        summary = SlaReader.summarise([
            record(RESPONSE, breached=False, name='P3 Response'),
            record(IGNORE, breached=True, minutes_left=-50.0, name='ITSM IAR SLA'),
        ])
        assert summary['sla_breached'] is False

    def test_vendor_breach_is_reported_separately(self):
        summary = SlaReader.summarise([
            record(RESOLUTION, breached=False, minutes_left=600.0),
            record(VENDOR, breached=True, minutes_left=-4000.0, name='Vendor Resolution'),
        ])
        assert summary['sla_breached'] is False          # not our breach
        assert summary['vendor_sla_breached'] is True    # but still surfaced
        assert summary['sla_time_left_mins'] == 600.0    # vendor did not win the pick

    def test_vendor_only_ticket_reports_no_customer_breach(self):
        summary = SlaReader.summarise([
            record(VENDOR, breached=True, minutes_left=-9000.0, name='Vendor Resolution'),
        ])
        assert summary['sla_breached'] is False
        assert summary['vendor_sla_breached'] is True

    def test_resolution_is_preferred_over_response(self):
        summary = SlaReader.summarise([
            record(RESPONSE, breached=True, minutes_left=-10.0, name='P1 Response'),
            record(RESOLUTION, breached=False, minutes_left=300.0, name='P1 Resolution'),
        ])
        assert summary['sla_breached'] is False
        assert summary['sla_time_left_mins'] == 300.0

    def test_response_used_only_when_nothing_else_exists(self):
        summary = SlaReader.summarise([
            record(RESPONSE, breached=True, minutes_left=-10.0),
        ])
        assert summary['sla_breached'] is True

    def test_closest_to_breaching_wins_among_resolutions(self):
        summary = SlaReader.summarise([
            record(RESOLUTION, breached=False, minutes_left=5000.0, pct=10.0),
            record(RESOLUTION, breached=False, minutes_left=45.0, pct=92.0),
        ])
        assert summary['sla_time_left_mins'] == 45.0
        assert summary['sla_pct_consumed'] == 92.0

    def test_breached_outranks_merely_close(self):
        summary = SlaReader.summarise([
            record(RESOLUTION, breached=False, minutes_left=20.0),
            record(RESOLUTION, breached=True, minutes_left=-200.0),
        ])
        assert summary['sla_breached'] is True
        assert summary['sla_time_left_mins'] == -200.0

    def test_no_records_returns_a_safe_empty_summary(self):
        summary = SlaReader.summarise([])
        assert summary['sla_breached'] is False
        assert summary['vendor_sla_breached'] is False
        assert summary['sla_pct_consumed'] is None
        assert summary['sla_records'] == []

    def test_every_record_is_retained_for_audit(self):
        summary = SlaReader.summarise([
            record(RESOLUTION), record(VENDOR), record(IGNORE),
        ])
        assert len(summary['sla_records']) == 3


# ---------------------------------------------------------------------------
# downstream wiring
# ---------------------------------------------------------------------------

class TestDownstream:
    def test_vendor_breach_raises_a_risk_flag_but_not_sla_breached(self):
        from pipeline.signals import extract_signals
        signals = extract_signals(
            {'incident_number': 'INC1', 'opened_at': '2026-09-14 08:00:00',
             'priority': '3 - Medium'},
            [],
            SlaReader.summarise([
                record(RESOLUTION, breached=False, minutes_left=800.0),
                record(VENDOR, breached=True, minutes_left=-4000.0),
            ]),
            system_accounts=[], now=datetime.datetime(2026, 9, 23, 12, 0))

        assert 'VENDOR_SLA_BREACHED' in signals['risk_flags']
        assert 'SLA_BREACHED' not in signals['risk_flags']

    def test_vendor_breach_lifts_the_score_via_blocked_stale(self):
        from pipeline.scoring import DEFAULT_WEIGHTS, compute_attention_score
        ticket = {'priority': '3 - Medium', 'reassignment_count': 0, 'reopen_count': 0}
        base = {'age_days': 8.0, 'idle_days': 1.0, 'sla_breached': False,
                'sla_pct_consumed': 20.0, 'last_agent_action_at': 'set'}

        without = compute_attention_score(ticket, base, DEFAULT_WEIGHTS, {})['score']
        with_vendor = compute_attention_score(
            ticket, dict(base, vendor_sla_breached=True), DEFAULT_WEIGHTS, {})['score']
        assert with_vendor > without

    def test_flag_has_a_display_label(self):
        from delivery.digest import FLAG_LABELS
        assert FLAG_LABELS['VENDOR_SLA_BREACHED'] == 'Vendor SLA breached'


# ---------------------------------------------------------------------------
# duration drift (sla-map)
# ---------------------------------------------------------------------------

class FakeContractSlaClient:
    """Serves contract_sla rows the way sysparm_display_value=true returns them."""

    def __init__(self, rows):
        self.rows = rows

    def get_all(self, table, params, max_records=None):
        assert table == 'contract_sla'
        return list(self.rows)


def definitions_reader(rows, definitions=None):
    r = SlaReader(client=FakeContractSlaClient(rows),
                  settings=FakeSettings({
                      'sla.definitions': DEFINITIONS if definitions is None else definitions,
                      'sla.include_types': ['SLA'],
                  }))
    return r


def row(sys_id, name, duration, type_='SLA'):
    return {'sys_id': sys_id, 'name': name, 'duration': duration, 'type': type_}


class TestDurationDrift:
    """Retuning an SLA in ServiceNow changes sla_pct_consumed for every ticket
    under it and silently reorders the board. Nothing else would notice."""

    def test_matching_duration_is_not_drift(self):
        entries = definitions_reader(
            [row(P3_RESL, 'Priority 3 (Medium) Resolution', '2 Days')]).get_definitions()
        assert entries[0]['duration_drift'] is False
        assert entries[0]['recorded_duration'] == '2 Days'

    def test_changed_duration_is_drift(self):
        entries = definitions_reader(
            [row(P3_RESL, 'Priority 3 (Medium) Resolution', '4 Days')]).get_definitions()
        assert entries[0]['duration_drift'] is True
        assert entries[0]['recorded_duration'] == '2 Days'
        assert entries[0]['duration'] == '4 Days'

    def test_unrecorded_duration_is_unverified_not_drift(self):
        """A definition with no recorded duration cannot have drifted."""
        entries = definitions_reader(
            [row('deadbeef' * 4, 'Some New Resolution SLA', '9 Days')]).get_definitions()
        assert entries[0]['duration_drift'] is False
        assert entries[0]['recorded_duration'] == ''

    def test_sys_id_match_is_case_insensitive(self):
        entries = definitions_reader(
            [row(P3_RESL.upper(), 'Priority 3 (Medium) Resolution', '2 Days')]
        ).get_definitions()
        assert entries[0]['duration_drift'] is False

    def test_every_production_definition_is_recorded(self):
        """The whole prod map, served back unchanged, must show zero drift and
        zero unverified entries - this is the state the config ships in."""
        rows = [row(sid, e['name'], e['duration']) for sid, e in DEFINITIONS.items()]
        entries = definitions_reader(rows).get_definitions()

        assert len(entries) == 13
        assert [e for e in entries if e['duration_drift']] == []
        assert [e for e in entries if not e['recorded_duration']] == []
        assert {e['source'] for e in entries} == {'config:sys_id'}

    def test_production_kind_split(self):
        """6 response, 5 resolution, 1 vendor, 1 ignored."""
        rows = [row(sid, e['name'], e['duration']) for sid, e in DEFINITIONS.items()]
        entries = definitions_reader(rows).get_definitions()
        counts = {}
        for entry in entries:
            counts[entry['kind']] = counts.get(entry['kind'], 0) + 1
        assert counts == {RESPONSE: 6, RESOLUTION: 5, VENDOR: 1, IGNORE: 1}


class TestDocumentationKeysAreIgnored:
    def test_underscore_keys_do_not_become_definitions(self):
        """conf.json carries _comment/_kinds/_duration alongside real sys_ids."""
        r = reader({
            '_comment': 'not a definition',
            '_duration': 'also not a definition',
            P3_RESL: {'kind': 'RESOLUTION', 'name': 'Priority 3 (Medium) Resolution'},
        })
        assert r.classify(P3_RESL, '') == RESOLUTION
        assert len(r._by_sys_id) == 1
