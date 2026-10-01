"""Tests for configuration loading and the threshold vocabulary bridge.

The risk these cover: when a threshold key is renamed, a configuration file
written against the old name stops being read. `settings.get` then returns the
hard-coded default, scoring behaviour changes, and nothing in the logs explains
why. The bridge accepts the superseded names and says so.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import (LEGACY_THRESHOLD_KEYS, ConfigError, Settings,
                         normalise_thresholds)

CURRENT = {
    'aged_after_days': 5,
    'inactivity_days': 2,
    'prolonged_inactivity_days': 4,
    'sla_risk_pct': 75,
    'closure_followup_count': 3,
    'closure_silence_days': 5,
}


class TestThresholdBridge:
    def test_current_keys_pass_through_unchanged(self):
        assert normalise_thresholds(CURRENT) == CURRENT

    def test_empty_and_none_are_safe(self):
        assert normalise_thresholds({}) == {}
        assert normalise_thresholds(None) == {}

    def test_does_not_mutate_the_caller_dict(self):
        source = {'stagnation_days': 3}
        normalise_thresholds(source)
        assert source == {'stagnation_days': 3}

    @pytest.mark.parametrize('legacy,current', sorted(LEGACY_THRESHOLD_KEYS.items()))
    def test_every_superseded_key_is_carried_over(self, legacy, current):
        result = normalise_thresholds({legacy: 42})
        assert result == {current: 42}, f'{legacy} was not mapped to {current}'

    @pytest.mark.parametrize('legacy,current', sorted(LEGACY_THRESHOLD_KEYS.items()))
    def test_the_rename_is_logged(self, legacy, current, caplog):
        """Silence here is the failure mode - the operator must be told."""
        with caplog.at_level('WARNING'):
            normalise_thresholds({legacy: 9})
        assert legacy in caplog.text
        assert current in caplog.text

    def test_the_current_name_wins_when_both_are_present(self):
        result = normalise_thresholds({'stagnation_days': 2, 'inactivity_days': 7})
        assert result == {'inactivity_days': 7}

    def test_a_conflict_is_logged(self, caplog):
        with caplog.at_level('WARNING'):
            normalise_thresholds({'sla_jeopardy_pct': 60, 'sla_risk_pct': 80})
        assert 'superseded' in caplog.text

    def test_a_wholly_superseded_file_still_produces_every_value(self):
        """A conf.json untouched since before the rename must keep working."""
        legacy_file = {
            'aged_after_days': 7,
            'stagnation_days': 3,
            'critical_stagnation_days': 6,
            'sla_jeopardy_pct': 80,
            'auto_close_followups': 4,
            'auto_close_silence_days': 10,
        }
        result = normalise_thresholds(legacy_file)
        assert result == {
            'aged_after_days': 7,
            'inactivity_days': 3,
            'prolonged_inactivity_days': 6,
            'sla_risk_pct': 80,
            'closure_followup_count': 4,
            'closure_silence_days': 10,
        }
        assert not any(k in result for k in LEGACY_THRESHOLD_KEYS)

    def test_zero_is_carried_over_not_treated_as_absent(self):
        """0 is falsy but a legitimate threshold value."""
        assert normalise_thresholds({'inactivity_days': 0}) == {'inactivity_days': 0}
        assert normalise_thresholds({'stagnation_days': 0}) == {'inactivity_days': 0}


class TestSettingsAccessors:
    def settings(self, raw):
        return Settings(raw, Path('conf.json'))

    def test_dotted_lookup(self):
        s = self.settings({'a': {'b': {'c': 1}}})
        assert s.get('a.b.c') == 1

    def test_missing_path_returns_the_default(self):
        s = self.settings({'a': {}})
        assert s.get('a.b.c', 'fallback') == 'fallback'

    def test_require_raises_on_a_missing_key(self):
        with pytest.raises(ConfigError, match='servicenow.url'):
            self.settings({}).require('servicenow.url')

    def test_require_raises_on_an_empty_value(self):
        with pytest.raises(ConfigError):
            self.settings({'servicenow': {'url': ''}}).require('servicenow.url')

    def test_assignment_groups_drops_blank_entries(self):
        s = self.settings({'servicenow': {'assignment_groups': ['IT Service Desk', '', '  ']}})
        assert s.assignment_groups == ['IT Service Desk']

    def test_system_accounts_are_lowercased(self):
        s = self.settings({'servicenow': {'system_accounts': ['System', 'CAC.Rest']}})
        assert s.system_accounts == ['system', 'cac.rest']
