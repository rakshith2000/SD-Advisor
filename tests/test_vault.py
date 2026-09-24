"""Tests for Vault credential handling and AppRole token renewal.

No live Vault is required: the parts worth testing are the file handling and
the renewal arithmetic, both of which are pure local logic. Login and renewal
round-trips are covered by `run.py doctor` against a real server.
"""

import logging
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vault_client import (DEFAULT_RENEW_RATIO, MIN_RENEW_SECONDS,
                               RENEW_RETRY_SECONDS, VaultClient, VaultError,
                               _read_secret_file)

ON_WINDOWS = os.name == 'nt'


def bare_client(**attrs) -> VaultClient:
    """A VaultClient with only the attributes under test set.

    __init__ talks to Vault, so the file and timing logic is exercised
    directly rather than through a mock server.
    """
    client = object.__new__(VaultClient)
    client.auth_method = 'approle'
    client.write_token_file = False
    client.renew_ratio = DEFAULT_RENEW_RATIO
    client.min_renew_seconds = MIN_RENEW_SECONDS
    client._lease_duration = 3600
    client._renew_count = 0
    client._relogin_count = 0
    client._renewer = None
    client._stop = threading.Event()
    for key, value in attrs.items():
        setattr(client, key, value)
    return client


# ---------------------------------------------------------------------------
# credential files
# ---------------------------------------------------------------------------

class TestReadSecretFile:
    def test_reads_and_strips(self, tmp_path):
        path = tmp_path / '.vault_role_id'
        path.write_text('  abc-123-role \n', encoding='utf-8')
        assert _read_secret_file(path, 'Vault role_id') == 'abc-123-role'

    def test_missing_file_names_the_credential(self, tmp_path):
        with pytest.raises(VaultError, match='role_id file not found'):
            _read_secret_file(tmp_path / 'nope', 'Vault role_id')

    def test_empty_file_is_rejected(self, tmp_path):
        path = tmp_path / '.vault_secret_id'
        path.write_text('   \n', encoding='utf-8')
        with pytest.raises(VaultError, match='is empty'):
            _read_secret_file(path, 'Vault secret_id')

    def test_loose_permissions_warn_but_do_not_fail(self, tmp_path, caplog):
        """An over-permissive credential file is worth flagging, but is not a
        reason to refuse to start a service that is otherwise healthy."""
        path = tmp_path / '.vault_secret_id'
        path.write_text('sid', encoding='utf-8')
        path.chmod(0o644)

        with caplog.at_level(logging.WARNING, logger='core.vault'):
            assert _read_secret_file(path, 'Vault secret_id') == 'sid'

        # Asserted on the advice rather than the exact bits: Windows reports
        # 666 for an ordinary file, so the mode differs but the warning does
        # fire on both platforms - which is what this test is really about.
        assert 'should be 600' in caplog.text
        assert '.vault_secret_id' in caplog.text

    def test_correct_permissions_produce_no_warning(self, tmp_path, caplog):
        path = tmp_path / '.vault_role_id'
        path.write_text('rid', encoding='utf-8')
        path.chmod(0o600)

        with caplog.at_level(logging.WARNING, logger='core.vault'):
            assert _read_secret_file(path, 'Vault role_id') == 'rid'

        if not ON_WINDOWS:          # Windows cannot represent mode 600
            assert 'should be 600' not in caplog.text


# ---------------------------------------------------------------------------
# token file mirroring
# ---------------------------------------------------------------------------

class TestPublishTokenFile:
    def test_disabled_by_default_writes_nothing(self, tmp_path):
        target = tmp_path / '.vault_token'
        bare_client(write_token_file=False, token_file_path=target)._publish_token_file('hvs.x')
        assert not target.exists()

    def test_writes_the_token_when_enabled(self, tmp_path):
        target = tmp_path / '.vault_token'
        bare_client(write_token_file=True, token_file_path=target)._publish_token_file('hvs.abc')
        assert target.read_text(encoding='utf-8') == 'hvs.abc'

    def test_overwrites_a_previous_token(self, tmp_path):
        target = tmp_path / '.vault_token'
        target.write_text('hvs.old', encoding='utf-8')
        bare_client(write_token_file=True, token_file_path=target)._publish_token_file('hvs.new')
        assert target.read_text(encoding='utf-8') == 'hvs.new'

    def test_leaves_no_temp_file_behind(self, tmp_path):
        target = tmp_path / '.vault_token'
        bare_client(write_token_file=True, token_file_path=target)._publish_token_file('hvs.abc')
        assert list(tmp_path.iterdir()) == [target]

    def test_creates_the_parent_directory(self, tmp_path):
        target = tmp_path / 'config' / '.vault_token'
        bare_client(write_token_file=True, token_file_path=target)._publish_token_file('hvs.abc')
        assert target.read_text(encoding='utf-8') == 'hvs.abc'

    @pytest.mark.skipif(ON_WINDOWS, reason='POSIX permission bits')
    def test_written_at_mode_600(self, tmp_path):
        target = tmp_path / '.vault_token'
        bare_client(write_token_file=True, token_file_path=target)._publish_token_file('hvs.abc')
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_an_unwritable_path_does_not_raise(self, tmp_path):
        """A token file that cannot be written is a logging problem, not a
        reason to take the service down - the app does not read it back."""
        target = tmp_path / 'file-not-dir' / '.vault_token'
        target.parent.write_text('I am a file', encoding='utf-8')
        bare_client(write_token_file=True, token_file_path=target)._publish_token_file('hvs.abc')


# ---------------------------------------------------------------------------
# renewal timing
# ---------------------------------------------------------------------------

class TestRenewalTiming:
    def test_renews_at_half_the_lease(self):
        assert bare_client(_lease_duration=3600)._sleep_seconds() == 1800

    def test_respects_a_custom_ratio(self):
        assert bare_client(_lease_duration=3600, renew_ratio=0.25)._sleep_seconds() == 900

    def test_never_busy_loops_on_a_short_lease(self):
        # A 30s lease at ratio 0.5 would be 15s; the floor prevents thrashing.
        assert bare_client(_lease_duration=30)._sleep_seconds() == MIN_RENEW_SECONDS

    def test_unknown_lease_falls_back_to_the_retry_interval(self):
        assert bare_client(_lease_duration=0)._sleep_seconds() == RENEW_RETRY_SECONDS

    def test_long_lease_scales_up(self):
        assert bare_client(_lease_duration=86400)._sleep_seconds() == 43200

    def test_renewal_leaves_margin_before_expiry(self):
        """Whatever the lease, the refresh must happen with time to spare so a
        single failed attempt can still be retried before the token dies."""
        for lease in (120, 600, 3600, 14400, 86400):
            assert bare_client(_lease_duration=lease)._sleep_seconds() < lease


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------

class TestClose:
    def test_close_is_safe_without_a_renewer(self):
        client = bare_client()
        client.close()
        assert client._stop.is_set()

    def test_close_stops_the_renewer_thread(self):
        client = bare_client(_lease_duration=3600)

        def loop():
            client._stop.wait(30)

        client._renewer = threading.Thread(target=loop, daemon=True)
        client._renewer.start()
        client.close()
        assert not client._renewer.is_alive()
