"""Settings an administrator can change without a shell.

Three things here, in order of how badly they fail.

**The allow-list is a security boundary.** conf.json holds the Vault address,
the database host and the OIDC client registration alongside the thresholds.
A page built as "everything except the bits we remembered to hide" would
expose the next key somebody adds to the file. These tests assert the shape
of the registry rather than its contents, so a key added later is checked by
the same rule.

**A value must not be stored before it has been parsed.** Everything arrives
from an HTML form as a string. "eight" must be refused, not stored and then
fail at the point of use - where the stack trace names a scoring function and
not the person who typed it.

**An override must be visible.** The one new way to be confused by this
service is to read conf.json over SSH and believe it. Anything overridden has
to say so, show the file value beside it, and be resettable.

    python -m pytest tests/test_admin_settings.py -q
"""

import datetime
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import admin_settings
from core.admin_settings import (BOOL, FORBIDDEN_PREFIXES, INT, LIST, LIVE,
                                 NUMBER, RESTART, SPECS, SettingError, Spec)
from core.config import Settings
from core.settings_store import SettingsOverlay

FILE_CONFIG = {
    'thresholds': {'aged_after_days': 5, 'inactivity_days': 2,
                   'prolonged_inactivity_days': 4, 'sla_risk_pct': 75,
                   'closure_followup_count': 3, 'closure_silence_days': 5,
                   'digest_max_tickets_per_agent': 25},
    'servicenow': {'assignment_groups': ['Service Desk'],
                   'url': 'https://example.service-now.com'},
    'runtime': {'shadow_mode': True},
    'mail': {'enabled': True, 'from_name': 'Aged Ticket Advisor',
             'subject_prefix': '[ATA]', 'always_bcc': []},
    'display': {'team_label': 'Assigned team', 'timezone': 'America/Chicago',
                'allow_user_timezone': True, 'datetime_format': '%d %b %Y %H:%M'},
    'scheduler': {'stream_interval_minutes': 10,
                  'recommendation_interval_minutes': 60,
                  'hourly_full_sync': True},
    'auth': {'role_requests': {'enabled': True, 'expire_after_days': 14,
                               'reject_cooldown_days': 30,
                               'require_justification_chars': 20}},
    'llm': {'min_confidence_to_surface': 0.55, 'min_similarity': 0.55},
    'vault': {'url': 'https://vault.example', 'paths': {'smtp': 'x'}},
    'database': {'host': 'db.example'},
}


class FakeDb:
    """The three operations the overlay performs, and no more."""

    def __init__(self, broken=False):
        self.rows = {}
        self.audit = []
        self.broken = broken

    def query(self, sql, params=None):
        if self.broken:
            raise RuntimeError('table runtime_setting does not exist')
        if 'setting_audit' in sql:
            return list(reversed(self.audit))
        return [{'setting_key': k, 'value_json': v}
                for k, v in sorted(self.rows.items())]

    def upsert(self, table, data, update_columns=None):
        assert table == 'runtime_setting'
        self.rows[data['setting_key']] = data['value_json']
        return 1

    def delete(self, table, conditions):
        self.rows.pop(conditions[0]['val'], None)
        return 1

    def insert(self, table, data):
        assert table == 'setting_audit'
        self.audit.append(dict(data))
        return 1


@pytest.fixture
def db():
    return FakeDb()


@pytest.fixture
def settings(db):
    s = Settings(json.loads(json.dumps(FILE_CONFIG)), Path('conf.json'))
    s.attach_overlay(SettingsOverlay(db, allowed=admin_settings.editable_keys()))
    return s


# ---------------------------------------------------------------------------
# the allow-list
# ---------------------------------------------------------------------------

class TestTheAllowList:
    def test_no_credential_bearing_key_is_editable(self):
        """The boundary. Asserted against prefixes rather than a list of
        today's keys, so a key added next year is covered by the same rule."""
        for spec in SPECS:
            for prefix in FORBIDDEN_PREFIXES:
                assert not spec.key.startswith(prefix), (
                    f'{spec.key} is editable and must not be')

    def test_the_overlay_refuses_a_key_outside_the_registry(self, db):
        overlay = SettingsOverlay(db, allowed=admin_settings.editable_keys())
        with pytest.raises(Exception):
            overlay.set('vault.url', 'https://evil.example', 'root')

    def test_a_stored_row_outside_the_registry_is_not_read_back(self, db):
        """Removing a key from SPECS disables its override without needing a
        migration to delete the row - and a row written by some future bug
        cannot take effect retroactively."""
        db.rows['vault.url'] = json.dumps('https://evil.example')
        overlay = SettingsOverlay(db, allowed=admin_settings.editable_keys())
        assert overlay.current() == {}

    def test_settings_get_falls_through_for_an_unmanaged_key(self, settings):
        assert settings.get('vault.url') == 'https://vault.example'

    def test_every_spec_has_a_section_that_exists(self):
        for spec in SPECS:
            assert spec.section in admin_settings.SECTION_KEYS, spec.key

    def test_every_section_has_at_least_one_setting(self):
        """A card on the hub that opens an empty panel reads as a broken
        page."""
        for key in admin_settings.SECTION_KEYS:
            assert admin_settings.section_specs(key), key

    def test_every_section_has_a_label_blurb_and_icon(self):
        for entry in admin_settings.SECTIONS:
            key, label, blurb, icon = entry
            assert key and label and blurb and icon

    def test_keys_are_unique(self):
        keys = [spec.key for spec in SPECS]
        assert len(keys) == len(set(keys))

    def test_every_numeric_setting_is_bounded(self):
        """An unbounded percentage accepts 10000 and silently reorders the
        whole board."""
        for spec in SPECS:
            if spec.kind in (INT, NUMBER):
                assert spec.minimum is not None, spec.key
                assert spec.maximum is not None, spec.key

    def test_the_dangerous_ones_carry_a_warning(self):
        for key in ('runtime.shadow_mode', 'servicenow.assignment_groups',
                    'mail.always_bcc'):
            assert admin_settings.BY_KEY[key].warning, key


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

class TestParsing:
    def spec(self, key):
        return admin_settings.BY_KEY[key]

    def test_a_number_arrives_as_a_string_and_is_stored_as_a_number(self):
        assert self.spec('thresholds.aged_after_days').parse('7') == 7

    def test_words_are_refused_rather_than_stored(self):
        with pytest.raises(SettingError, match='not a whole number'):
            self.spec('thresholds.aged_after_days').parse('eight')

    def test_a_value_below_the_floor_is_refused(self):
        with pytest.raises(SettingError, match='at least 1'):
            self.spec('thresholds.aged_after_days').parse('0')

    def test_a_value_above_the_ceiling_is_refused(self):
        with pytest.raises(SettingError, match='at most 100'):
            self.spec('thresholds.sla_risk_pct').parse('150')

    def test_a_fraction_is_kept_as_one(self):
        assert self.spec('llm.min_confidence_to_surface').parse('0.75') == 0.75

    def test_a_blank_number_is_refused_not_zeroed(self):
        """Zero is a meaningful value for several of these, so defaulting a
        blank to it would be a silent change rather than an error."""
        with pytest.raises(SettingError, match='cannot be blank'):
            self.spec('thresholds.inactivity_days').parse('')

    def test_a_list_splits_on_newlines_and_commas(self):
        assert self.spec('servicenow.assignment_groups').parse(
            'Service Desk\nDesktop Support, Network Operations') == [
            'Service Desk', 'Desktop Support', 'Network Operations']

    def test_blank_list_entries_are_dropped(self):
        """A trailing newline in a textarea must not become an empty group
        name, which would match no tickets and show as an empty board."""
        assert self.spec('servicenow.assignment_groups').parse(
            'Service Desk\n\n  \n') == ['Service Desk']

    def test_an_unticked_checkbox_is_false_not_missing(self):
        assert self.spec('runtime.shadow_mode').parse(False) is False
        assert self.spec('runtime.shadow_mode').parse(True) is True

    def test_text_is_trimmed(self):
        assert self.spec('display.team_label').parse('  IT Support  ') == 'IT Support'


# ---------------------------------------------------------------------------
# the overlay
# ---------------------------------------------------------------------------

class TestTheOverlay:
    def test_an_override_wins_over_the_file(self, settings):
        assert settings.get('thresholds.aged_after_days') == 5
        settings.overlay.set('thresholds.aged_after_days', 9, 'root')
        assert settings.get('thresholds.aged_after_days') == 9

    def test_the_file_value_is_still_readable(self, settings):
        """What the page shows beside an override, so the two cannot silently
        disagree without somebody being shown both."""
        settings.overlay.set('thresholds.aged_after_days', 9, 'root')
        assert settings.file_value('thresholds.aged_after_days') == 5

    def test_clearing_restores_the_file_value(self, settings):
        settings.overlay.set('thresholds.aged_after_days', 9, 'root')
        settings.overlay.clear('thresholds.aged_after_days', 'root')
        assert settings.get('thresholds.aged_after_days') == 5

    def test_a_typed_accessor_follows_the_override(self, settings):
        """settings.aged_after_days and settings.shadow_mode are read all over
        the service and must not bypass the overlay."""
        settings.overlay.set('thresholds.aged_after_days', 11, 'root')
        settings.overlay.set('runtime.shadow_mode', False, 'root')
        assert settings.aged_after_days == 11
        assert settings.shadow_mode is False

    def test_a_list_round_trips(self, settings):
        settings.overlay.set('servicenow.assignment_groups',
                             ['Service Desk', 'Desktop Support'], 'root')
        assert settings.assignment_groups == ['Service Desk', 'Desktop Support']

    def test_every_change_is_recorded(self, db, settings):
        settings.overlay.set('runtime.shadow_mode', False, 'asha')
        entry = db.audit[-1]
        assert entry['setting_key'] == 'runtime.shadow_mode'
        assert entry['old_value'] is None          # was following the file
        assert json.loads(entry['new_value']) is False
        assert entry['changed_by'] == 'asha'

    def test_the_previous_value_is_recorded_on_a_second_change(self, db, settings):
        settings.overlay.set('thresholds.aged_after_days', 7, 'asha')
        settings.overlay.set('thresholds.aged_after_days', 9, 'raj')
        assert json.loads(db.audit[-1]['old_value']) == 7

    def test_a_reset_is_recorded_too(self, db, settings):
        settings.overlay.set('thresholds.aged_after_days', 7, 'asha')
        settings.overlay.clear('thresholds.aged_after_days', 'raj')
        assert db.audit[-1]['new_value'] is None

    def test_a_failed_audit_write_does_not_lose_the_change(self, db, settings):
        """The change is already applied. Refusing it because the history
        table is unavailable would leave nobody able to tell which happened."""
        def boom(table, data):
            raise RuntimeError('audit table missing')
        db.insert = boom

        settings.overlay.set('thresholds.aged_after_days', 9, 'root')
        assert settings.get('thresholds.aged_after_days') == 9

    def test_clearing_something_that_was_never_set_is_a_no_op(self, settings):
        assert settings.overlay.clear('thresholds.aged_after_days', 'root') is False


class TestTheOverlayFailsSafe:
    def test_a_missing_table_falls_through_to_the_file(self):
        """Migration 007 not applied yet. The service must run on what it
        started with rather than refusing to answer."""
        settings = Settings(json.loads(json.dumps(FILE_CONFIG)), Path('conf.json'))
        settings.attach_overlay(
            SettingsOverlay(FakeDb(broken=True),
                            allowed=admin_settings.editable_keys()))
        assert settings.get('thresholds.aged_after_days') == 5

    def test_an_unparseable_row_does_not_discard_the_others(self, db, settings):
        db.rows['thresholds.aged_after_days'] = 'not json'
        db.rows['thresholds.sla_risk_pct'] = json.dumps(90)
        settings.overlay.invalidate()

        assert settings.get('thresholds.aged_after_days') == 5   # file wins
        assert settings.get('thresholds.sla_risk_pct') == 90

    def test_reads_are_cached_rather_than_hitting_the_database_each_time(self, db, settings):
        """settings.get is called on every request and for every ticket
        scored. A query per call would put this table in the hot path of the
        whole service."""
        calls = []
        inner = db.query
        db.query = lambda sql, params=None: (calls.append(sql), inner(sql, params))[1]

        for _ in range(50):
            settings.get('thresholds.aged_after_days')
        assert len(calls) == 1

    def test_a_write_refreshes_the_cache_immediately(self, settings):
        settings.get('thresholds.aged_after_days')
        settings.overlay.set('thresholds.aged_after_days', 9, 'root')
        assert settings.get('thresholds.aged_after_days') == 9

    def test_the_cache_expires_so_a_cli_change_is_picked_up(self, db):
        """Two processes: the web app and a cron run. Neither can invalidate
        the other's cache, so the staleness window has to close on its own."""
        clock = {'now': datetime.datetime(2026, 1, 1, 12, 0, 0)}
        overlay = SettingsOverlay(db, allowed=admin_settings.editable_keys(),
                                  clock=lambda: clock['now'])
        assert overlay.current() == {}

        db.rows['thresholds.aged_after_days'] = json.dumps(9)
        assert overlay.current() == {}                       # still cached

        clock['now'] += datetime.timedelta(seconds=31)
        assert overlay.current() == {'thresholds.aged_after_days': 9}


# ---------------------------------------------------------------------------
# saving a form
# ---------------------------------------------------------------------------

class TestApplyForm:
    def test_a_changed_value_is_stored(self, settings):
        result = admin_settings.apply_form(
            settings, {'thresholds.aged_after_days': '9'}, 'review', 'root')
        assert result['changed'] == ['thresholds.aged_after_days']
        assert settings.get('thresholds.aged_after_days') == 9

    def test_a_value_matching_the_file_stores_nothing(self, settings):
        """Otherwise every save writes a row for every field, and
        "overridden" stops meaning "differs from conf.json"."""
        result = admin_settings.apply_form(
            settings, {'thresholds.aged_after_days': '5'}, 'review', 'root')
        assert result['changed'] == []
        assert settings.overlay.current() == {}

    def test_setting_a_value_back_to_the_file_clears_the_override(self, settings):
        settings.overlay.set('thresholds.aged_after_days', 9, 'root')
        result = admin_settings.apply_form(
            settings, {'thresholds.aged_after_days': '5'}, 'review', 'root')
        assert result['cleared'] == ['thresholds.aged_after_days']
        assert settings.overlay.current() == {}

    def test_one_bad_field_changes_nothing_at_all(self, settings):
        """A partial save leaves the administrator reading an error beside a
        page that has already half-applied."""
        with pytest.raises(SettingError):
            admin_settings.apply_form(settings, {
                'thresholds.aged_after_days': '9',
                'thresholds.sla_risk_pct': 'most of it',
            }, 'review', 'root')
        assert settings.overlay.current() == {}

    def test_an_absent_checkbox_is_stored_as_false(self, settings):
        """Checkboxes post nothing when unticked. Treating absence as "not
        submitted" would make shadow mode impossible to turn off."""
        admin_settings.apply_form(settings, {}, 'mail', 'root')
        assert settings.get('runtime.shadow_mode') is False

    def test_a_ticked_checkbox_is_stored_as_true(self, settings):
        settings.overlay.set('runtime.shadow_mode', False, 'root')
        admin_settings.apply_form(settings, {'runtime.shadow_mode': '1'},
                                  'mail', 'root')
        assert settings.get('runtime.shadow_mode') is True

    def test_a_field_from_another_section_is_ignored(self, settings):
        """The form posts one section. A key smuggled in from elsewhere must
        not be applied by a route that only validated this section."""
        admin_settings.apply_form(
            settings, {'servicenow.assignment_groups': 'Anything'},
            'review', 'root')
        assert settings.assignment_groups == ['Service Desk']

    def test_restart_keys_are_reported_separately(self, settings):
        result = admin_settings.apply_form(
            settings, {'servicenow.assignment_groups': 'Service Desk\nDesktop Support'},
            'queues', 'root')
        assert result['restart'] == ['servicenow.assignment_groups']

    def test_a_live_key_is_not_reported_as_needing_a_restart(self, settings):
        result = admin_settings.apply_form(
            settings, {'thresholds.aged_after_days': '9'}, 'review', 'root')
        assert result['restart'] == []

    def test_an_unknown_section_is_refused(self, settings):
        with pytest.raises(SettingError, match='not a settings section'):
            admin_settings.apply_form(settings, {}, 'nonsense', 'root')

    def test_no_storage_is_reported_rather_than_silently_discarding(self):
        settings = Settings(json.loads(json.dumps(FILE_CONFIG)), Path('conf.json'))
        with pytest.raises(SettingError, match='storage is unavailable'):
            admin_settings.apply_form(
                settings, {'thresholds.aged_after_days': '9'}, 'review', 'root')


# ---------------------------------------------------------------------------
# what the page shows
# ---------------------------------------------------------------------------

class TestRowsForThePage:
    def test_an_unoverridden_row_is_not_marked(self, settings):
        row = next(r for r in admin_settings.rows_for(settings, 'review')
                   if r['spec'].key == 'thresholds.aged_after_days')
        assert row['overridden'] is False
        assert row['differs_from_file'] is False

    def test_an_overridden_row_carries_both_values(self, settings):
        settings.overlay.set('thresholds.aged_after_days', 9, 'root')
        row = next(r for r in admin_settings.rows_for(settings, 'review')
                   if r['spec'].key == 'thresholds.aged_after_days')
        assert row['value'] == 9
        assert row['file_value'] == 5
        assert row['differs_from_file'] is True

    def test_restart_pending_lists_only_unapplied_restart_keys(self, settings):
        settings.overlay.set('thresholds.aged_after_days', 9, 'root')
        assert admin_settings.restart_pending(settings) == []

        settings.overlay.set('scheduler.stream_interval_minutes', 5, 'root')
        assert [s.key for s in admin_settings.restart_pending(settings)] == [
            'scheduler.stream_interval_minutes']

    def test_the_export_carries_the_overrides(self, settings):
        settings.overlay.set('thresholds.aged_after_days', 9, 'root')
        exported = json.loads(admin_settings.export_json(settings))
        assert exported['thresholds']['aged_after_days'] == 9

    def test_the_export_leaves_untouched_keys_alone(self, settings):
        settings.overlay.set('thresholds.aged_after_days', 9, 'root')
        exported = json.loads(admin_settings.export_json(settings))
        assert exported['vault']['url'] == 'https://vault.example'
        assert exported['thresholds']['sla_risk_pct'] == 75

    def test_as_dict_is_still_the_file_alone(self, settings):
        """Two callers want different things, and conflating them would make
        the file-value column on the page show the override."""
        settings.overlay.set('thresholds.aged_after_days', 9, 'root')
        assert settings.as_dict()['thresholds']['aged_after_days'] == 5


class TestTheSectionCards:
    """The grid at the top of the page. It answers "what can I change here",
    so it has to show every area at once and mark the one being edited."""

    def test_there_is_a_card_for_every_section(self, settings):
        assert ([e['key'] for e in admin_settings.section_summaries(settings)]
                == admin_settings.SECTION_KEYS)

    def test_each_card_counts_its_settings(self, settings):
        entry = next(e for e in admin_settings.section_summaries(settings)
                     if e['key'] == 'review')
        assert entry['count'] == len(admin_settings.section_specs('review'))

    def test_a_card_shows_how_many_are_changed(self, settings):
        """So the section somebody has been editing is findable without
        opening all seven."""
        settings.overlay.set('thresholds.aged_after_days', 9, 'root')
        entry = next(e for e in admin_settings.section_summaries(settings)
                     if e['key'] == 'review')
        assert entry['overridden'] == 1

    def test_an_untouched_card_counts_nothing(self, settings):
        entry = next(e for e in admin_settings.section_summaries(settings)
                     if e['key'] == 'mail')
        assert entry['overridden'] == 0

    def test_the_card_flags_a_pending_restart(self, settings):
        settings.overlay.set('scheduler.stream_interval_minutes', 5, 'root')
        entry = next(e for e in admin_settings.section_summaries(settings)
                     if e['key'] == 'schedule')
        assert entry['needs_restart'] is True

    def test_a_live_change_is_not_flagged_as_pending(self, settings):
        settings.overlay.set('thresholds.aged_after_days', 9, 'root')
        entry = next(e for e in admin_settings.section_summaries(settings)
                     if e['key'] == 'review')
        assert entry['needs_restart'] is False

    def test_exactly_one_card_is_marked_current(self, settings):
        summaries = admin_settings.section_summaries(settings, 'mail')
        assert [e['key'] for e in summaries if e['active']] == ['mail']


# ---------------------------------------------------------------------------
# the settings that must take effect without a restart
# ---------------------------------------------------------------------------

class TestLiveSettings:
    """Marked `live` in the registry, so the page promises they apply on save.

    The Mailer is built once per process and lives for its lifetime, so any
    of these captured in its constructor would have been the value the
    service started with - and shadow mode is the most consequential switch
    in the product.
    """

    def mailer(self, settings):
        from delivery.mailer import Mailer

        class NoVault:
            def secret(self, path):
                raise RuntimeError('anonymous relay')

        return Mailer(settings, NoVault(), db=None)

    def test_shadow_mode_is_marked_live(self):
        assert admin_settings.BY_KEY['runtime.shadow_mode'].applies == LIVE

    def test_the_mailer_sees_shadow_mode_change(self, settings):
        mailer = self.mailer(settings)
        assert mailer.shadow is True
        settings.overlay.set('runtime.shadow_mode', False, 'root')
        assert mailer.shadow is False

    def test_the_mailer_sees_the_from_name_change(self, settings):
        mailer = self.mailer(settings)
        settings.overlay.set('mail.from_name', 'IT Support Advisor', 'root')
        assert mailer.from_name == 'IT Support Advisor'

    def test_the_mailer_sees_mail_being_disabled(self, settings):
        mailer = self.mailer(settings)
        settings.overlay.set('mail.enabled', False, 'root')
        assert mailer.enabled is False

    def test_the_team_label_follows_the_override(self, settings):
        from delivery.digest import team_label

        settings.overlay.set('display.team_label', 'IT Support', 'root')
        assert team_label(settings) == 'IT Support'

    def test_every_scheduler_key_is_honestly_marked_restart(self):
        """build_scheduler reads these once. Marking one live would promise
        something the scheduler cannot deliver."""
        for spec in SPECS:
            if spec.key.startswith('scheduler.'):
                assert spec.applies == RESTART, spec.key

    def test_the_queue_list_is_honestly_marked_restart(self):
        """IncidentReader is constructed with it at context build time."""
        assert admin_settings.BY_KEY[
            'servicenow.assignment_groups'].applies == RESTART


# ---------------------------------------------------------------------------
# the routes
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope='module')
def app_source():
    return (ROOT / 'web' / 'app.py').read_text(encoding='utf-8')


class TestEveryAdminRouteIsAdminGated:
    """Hiding the menu entry is presentation. The POST is reachable by hand.

    These settings change how the whole service behaves - one of them starts
    live mail to the estate, another decides who can see which queue - so the
    gate has to be on the route, and it has to be on every one of them.
    """

    def routes(self, source):
        """(verb, path, function name, signature) for every /admin route."""
        import re
        found = []
        for match in re.finditer(
                r"@app\.(get|post)\(\s*'(/admin/[^']*)'", source):
            verb, path = match.groups()
            definition = re.search(r'\n\s*(?:async\s+)?def\s+(\w+)\(',
                                   source[match.end():])
            start = match.end() + definition.end()
            signature = source[start:source.index('):', start)]
            found.append((verb, path, definition.group(1), signature))
        return found

    def test_the_routes_were_found(self, app_source):
        """A regex that silently matches nothing would pass every test
        below."""
        assert len(self.routes(app_source)) >= 10

    def test_each_one_depends_on_require_admin(self, app_source):
        for verb, path, name, signature in self.routes(app_source):
            assert 'require_admin' in signature, f'{verb.upper()} {path} ({name})'

    def test_no_admin_route_settles_for_a_lead(self, app_source):
        for verb, path, name, signature in self.routes(app_source):
            assert 'require_lead' not in signature, f'{verb.upper()} {path}'


class TestTheResetRouteChecksTheRegistry:
    def test_it_looks_the_key_up_before_acting(self, app_source):
        """Without this a crafted post could delete a row for any key.
        Inert today, because only allow-listed keys are read back - but it
        would make the audit trail describe changes that never happened."""
        body = app_source[app_source.index("def admin_settings_reset"):]
        body = body[:body.index('@app.')]
        assert 'admin_settings.BY_KEY' in body
        assert '404' in body
