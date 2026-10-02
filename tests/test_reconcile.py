"""Offboarding reconciliation against Keycloak.

The job exists because a leaver disabled in Entra can no longer sign in, but
their advisor_user row stays active - and leads_for() selects on
role IN ('ADMIN','LEAD'). They keep receiving the daily digest and every risk
alert, carrying incident descriptions and caller names, indefinitely, with
nothing raised anywhere.

So the job has to deactivate. Which makes its failure modes dangerous in the
opposite direction, and that is what these tests are about.

An expired service-account secret, a renamed realm and a Keycloak outage all
present the same way: lookups return nothing, for everyone, repeatedly. An
unguarded loop reads that as the entire service desk having left and silences
the digest. The proportional ceiling is the only thing standing between a
routine credential expiry and a desk that stops being told about SLA breaches.

The other two guards protect against lockout. Local accounts are never touched
because one of them is the break-glass administrator, deliberately unknown to
Keycloak - precisely so it still works when Keycloak is what has failed. And
the last active administrator is never deactivated, because nothing in the UI
can promote a replacement.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scheduler.jobs import JobRunner


class FakeSettings:
    def __init__(self, **over):
        self.values = {
            'auth.reconcile.enabled': True,
            'auth.reconcile.max_deactivate_pct': 30,
            'auth.oidc.issuer': 'https://keycloak.example.com/realms/AIOT',
        }
        self.values.update(over)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def require(self, key):
        return self.values[key]


class FakeDb:
    def __init__(self, accounts):
        self.rows = [dict(a) for a in accounts]

    def retrieve(self, table, columns=None, conditions=None, order_by=None, limit=None):
        rows = [dict(r) for r in self.rows]
        for condition in conditions or []:
            col, val = condition['col'], condition.get('val')
            rows = [r for r in rows if r.get(col) == val]
        return rows

    def update(self, table, values, conditions=None):
        target = conditions[0]['val']
        for row in self.rows:
            if row['id'] == target:
                row.update(values)
                return 1
        return 0

    def active(self):
        return {r['username'] for r in self.rows if r['active']}


class FakeKeycloak:
    """Returns accounts that still exist; everything else is treated as gone."""

    def __init__(self, present=(), disabled=(), raises=()):
        self.present = set(present)
        self.disabled = set(disabled)
        self.raises = set(raises)

    def _lookup(self, key):
        from core.keycloak import KeycloakError
        if key in self.raises:
            raise KeycloakError('Keycloak unreachable')
        if key in self.present or key in self.disabled:
            return {'username': key, 'enabled': key not in self.disabled}
        return None

    def find_by_external_id(self, external_id):
        return self._lookup(external_id)

    def find_user(self, username):
        return self._lookup(username)

    @staticmethod
    def is_active(account):
        return bool(account.get('enabled', False))


class FakeContext:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.vault = None


def account(username, role='LEAD', auth_source='OIDC', active=1, external_id=None):
    return {'id': abs(hash(username)) % 100000, 'username': username, 'role': role,
            'auth_source': auth_source, 'active': active,
            'external_id': external_id if external_id is not None else f'ext-{username}',
            'email': f'{username}@example.com'}


def run(accounts, keycloak, dry_run=False, **settings):
    db = FakeDb(accounts)
    runner = JobRunner.__new__(JobRunner)          # skip the heavyweight __init__
    runner.ctx = FakeContext(db, FakeSettings(**settings))

    import core.keycloak as module
    original = module.KeycloakAdmin
    module.KeycloakAdmin = lambda settings_, vault_: keycloak
    try:
        return runner.reconcile_users(dry_run=dry_run), db
    finally:
        module.KeycloakAdmin = original


# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_a_departed_account_is_deactivated(self):
        result, db = run(
            [account('stays'), account('left'), account('also.stays'), account('b'),
             account('c'), account('d')],
            FakeKeycloak(present=['ext-stays', 'ext-also.stays', 'ext-b', 'ext-c', 'ext-d']))

        assert result['deactivated'] == 1
        assert result['missing'] == ['left']
        assert 'left' not in db.active()

    def test_an_account_disabled_upstream_counts_as_gone(self):
        """A user federated from Entra shows enabled=false here once Keycloak
        next syncs them."""
        result, db = run(
            [account('a'), account('b'), account('c'), account('disabled.upstream')],
            FakeKeycloak(present=['ext-a', 'ext-b', 'ext-c'],
                         disabled=['ext-disabled.upstream']))

        assert result['deactivated'] == 1
        assert 'disabled.upstream' not in db.active()

    def test_present_accounts_get_a_fresh_last_seen_stamp(self):
        _, db = run([account('here')], FakeKeycloak(present=['ext-here']))
        assert db.rows[0]['last_seen_idp_at'] is not None

    def test_lookup_falls_back_to_username_without_an_external_id(self):
        result, _ = run([account('legacy', external_id=None)],
                        FakeKeycloak(present=['legacy']))
        assert result['deactivated'] == 0

    def test_nothing_happens_when_the_job_is_disabled(self):
        result, db = run([account('left')], FakeKeycloak(),
                         **{'auth.reconcile.enabled': False})
        assert result['skipped']
        assert db.active() == {'left'}

    def test_a_dry_run_reports_without_changing_anything(self):
        result, db = run(
            [account('a'), account('b'), account('c'), account('left')],
            FakeKeycloak(present=['ext-a', 'ext-b', 'ext-c']), dry_run=True)

        assert result['deactivated'] == 1
        assert result['missing'] == ['left']
        assert db.active() == {'a', 'b', 'c', 'left'}      # untouched


class TestLocalAccountsAreNeverTouched:
    def test_the_break_glass_administrator_survives(self):
        """Deliberately unknown to Keycloak - that is the point of it. A job
        that deactivated anything it could not find would remove the one
        credential that still works when Keycloak is what has failed."""
        result, db = run(
            [account('sso.lead'), account('breakglass', role='ADMIN', auth_source='LOCAL')],
            FakeKeycloak(present=['ext-sso.lead']))

        assert result['checked'] == 1                      # only the OIDC account
        assert 'breakglass' in db.active()

    def test_a_realm_wide_outage_cannot_remove_local_accounts(self):
        result, db = run(
            [account('local.one', auth_source='LOCAL'),
             account('local.two', auth_source='LOCAL')],
            FakeKeycloak())

        assert result['checked'] == 0
        assert db.active() == {'local.one', 'local.two'}


class TestProportionalCeiling:
    def test_a_mass_disappearance_changes_nothing(self):
        """The guard that stands between an expired service-account secret and
        a service desk that stops being told about SLA breaches."""
        accounts = [account(f'user{i}') for i in range(10)]
        result, db = run(accounts, FakeKeycloak())         # nobody found

        assert result['refused'] is True
        assert result['deactivated'] == 0
        assert len(db.active()) == 10

    def test_a_proportion_under_the_ceiling_proceeds(self):
        accounts = [account(f'user{i}') for i in range(10)]
        present = [f'ext-user{i}' for i in range(8)]       # 20% gone
        result, db = run(accounts, FakeKeycloak(present=present))

        assert not result.get('refused')
        assert result['deactivated'] == 2
        assert len(db.active()) == 8

    def test_the_ceiling_is_configurable_for_a_real_mass_offboarding(self):
        accounts = [account(f'user{i}') for i in range(10)]
        result, _ = run(accounts, FakeKeycloak(),
                        **{'auth.reconcile.max_deactivate_pct': 100})
        assert result['deactivated'] == 10

    def test_the_ceiling_is_a_proportion_not_a_count(self):
        """One of two accounts is 50% and must trip; one of ten is 10% and
        must not. A flat threshold would get the small deployment wrong."""
        result, _ = run([account('a'), account('gone')], FakeKeycloak(present=['ext-a']))
        assert result['refused'] is True


class TestAdminFloor:
    def test_the_only_administrator_is_never_deactivated(self):
        """Nothing in the UI can promote a replacement."""
        accounts = [account('a'), account('b'), account('c'),
                    account('sole.admin', role='ADMIN')]
        result, db = run(accounts,
                         FakeKeycloak(present=['ext-a', 'ext-b', 'ext-c']))

        assert result['deactivated'] == 0
        assert result['errors'] == 1
        assert 'sole.admin' in db.active()

    def test_one_of_several_administrators_can_go(self):
        accounts = [account('a'), account('b'), account('c'),
                    account('admin.one', role='ADMIN'),
                    account('admin.two', role='ADMIN')]
        result, db = run(accounts, FakeKeycloak(
            present=['ext-a', 'ext-b', 'ext-c', 'ext-admin.two']))

        assert result['deactivated'] == 1
        assert 'admin.one' not in db.active()
        assert 'admin.two' in db.active()


class TestLookupFailures:
    def test_one_failed_lookup_is_not_evidence_of_departure(self):
        result, db = run(
            [account('a'), account('b'), account('c'), account('flaky')],
            FakeKeycloak(present=['ext-a', 'ext-b', 'ext-c'], raises=['ext-flaky']))

        assert result['errors'] == 1
        assert result['deactivated'] == 0
        assert 'flaky' in db.active()

    def test_an_errored_account_is_not_counted_as_missing(self):
        result, _ = run(
            [account('a'), account('b'), account('c'), account('flaky')],
            FakeKeycloak(present=['ext-a', 'ext-b', 'ext-c'], raises=['ext-flaky']))
        assert result['missing'] == []

    def test_an_empty_estate_is_a_no_op(self):
        result, _ = run([], FakeKeycloak())
        assert result == {'checked': 0, 'deactivated': 0, 'missing': [],
                          'errors': 0, 'dry_run': False}
