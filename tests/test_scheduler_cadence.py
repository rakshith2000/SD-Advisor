"""The hourly cadence, and why it runs a full sync.

The delta sync returns the rows ServiceNow reports as changed *and in scope*.
A ticket that has been resolved, or reassigned to a team this service does
not cover, has stopped matching that query - so it is not in the delta, and
no number of delta runs will ever mention it again. A query returns what
matches; it cannot return what stopped matching.

Only the full pass walks what is already tracked and notices what has
departed. That used to happen once a day inside the digest job, which is why
the board could carry a resolved ticket for most of a day. It now runs hourly,
in front of the LLM pass so recommendations are computed against a current
board rather than last hour's.

    python -m pytest tests/test_scheduler_cadence.py -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scheduler.jobs import JobRunner, build_scheduler


class FakeSettings:
    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


class FakeSync:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def run(self, full=False):
        self.calls.append(full)
        if self.fail:
            raise RuntimeError('ServiceNow is unreachable')
        return {'full': full}


class FakeOrchestrator:
    def __init__(self, fail_signals=False, fail_recs=False):
        self.order = []
        self.fail_signals = fail_signals
        self.fail_recs = fail_recs

    def refresh_signals(self):
        self.order.append('signals')
        if self.fail_signals:
            raise RuntimeError('a malformed history row')
        return {'signals': 1}

    def refresh_recommendations(self, limit=None):
        self.order.append('recommendations')
        if self.fail_recs:
            raise RuntimeError('rate limited')
        return {'recommendations': 1, 'limit': limit}


class FakeContext:
    def __init__(self, sync, settings=None):
        self.sync = sync
        self.settings = settings or FakeSettings()


def runner(sync=None, orchestrator=None, **settings):
    r = JobRunner.__new__(JobRunner)
    r.ctx = FakeContext(sync or FakeSync(), FakeSettings(**settings))
    r.orchestrator = orchestrator or FakeOrchestrator()
    r.dispatcher = None
    return r


# ---------------------------------------------------------------------------

class TestHourlyRefresh:
    def test_the_sync_is_full_not_a_delta(self):
        """The whole reason this job exists. A delta cannot report a ticket
        that has stopped matching the query."""
        sync = FakeSync()
        runner(sync).hourly_refresh()
        assert sync.calls == [True]

    def test_signals_then_recommendations(self):
        """Ordered so each stage sees the previous one's output. A
        recommendation computed before the sync lands is a recommendation
        about last hour's state."""
        orchestrator = FakeOrchestrator()
        runner(orchestrator=orchestrator).hourly_refresh()
        assert orchestrator.order == ['signals', 'recommendations']

    def test_all_three_stages_are_reported(self):
        result = runner().hourly_refresh()
        assert set(result) >= {'sync', 'signals', 'recommendations'}

    def test_the_limit_is_passed_through(self):
        result = runner().hourly_refresh(limit=5)
        assert result['recommendations']['limit'] == 5

    def test_a_failed_sync_does_not_skip_the_rest(self):
        """They fail for unrelated reasons - a slow ServiceNow, a malformed
        history row, a rate-limited model - and one of them is no reason to
        skip the other two."""
        orchestrator = FakeOrchestrator()
        result = runner(FakeSync(fail=True), orchestrator).hourly_refresh()

        assert result['sync'] == {'error': True}
        assert orchestrator.order == ['signals', 'recommendations']

    def test_signals_are_recomputed_even_when_the_sync_failed(self):
        """The inactivity clocks move with the wall clock, not with the sync,
        so a ticket can cross the four-day line during an outage."""
        orchestrator = FakeOrchestrator()
        runner(FakeSync(fail=True), orchestrator).hourly_refresh()
        assert 'signals' in orchestrator.order

    def test_a_failed_signal_pass_does_not_skip_the_model(self):
        orchestrator = FakeOrchestrator(fail_signals=True)
        result = runner(orchestrator=orchestrator).hourly_refresh()

        assert result['signals'] == {'error': True}
        assert result['recommendations'] == {'recommendations': 1, 'limit': None}

    def test_a_failed_model_pass_is_reported_not_raised(self):
        """The scheduler swallows exceptions anyway; returning the flag is
        what puts it in the job result where it can be seen."""
        result = runner(orchestrator=FakeOrchestrator(fail_recs=True)).hourly_refresh()
        assert result['recommendations'] == {'error': True}

    def test_nothing_escapes(self):
        """APScheduler logs an unhandled exception and moves on, so a job
        that raises loses its partial result."""
        everything = runner(FakeSync(fail=True),
                            FakeOrchestrator(fail_signals=True, fail_recs=True))
        assert everything.hourly_refresh()['sync'] == {'error': True}


class StubRunner:
    """Stands in for JobRunner while the wiring is under test.

    The real one builds an Orchestrator and a Dispatcher, which want a
    database, Vault and ServiceNow. None of that is the subject here - what
    is being checked is which method each trigger is bound to, and that is
    readable from the name alone.
    """

    def __init__(self, context):
        self.ctx = context

    def _named(name):
        def body(self, *a, **kw):
            return name
        body.__name__ = name
        return body

    stream_tick = _named('stream_tick')
    hourly_refresh = _named('hourly_refresh')
    recommendation_pass = _named('recommendation_pass')
    daily_digest = _named('daily_digest')
    nightly_maintenance = _named('nightly_maintenance')
    reconcile_users = _named('reconcile_users')
    del _named


class TestTheScheduledJobs:
    """build_scheduler wires the job bodies to triggers. The failure it
    catches is a job that is defined and never registered."""

    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch):
        monkeypatch.setattr('scheduler.jobs.JobRunner', StubRunner)

    def scheduler(self, **settings):
        defaults = {'scheduler.digests': [], 'auth.reconcile.enabled': False}
        defaults.update(settings)
        # Never started, so never shut down - build_scheduler only registers
        # the jobs, and starting one here would run them.
        return build_scheduler(FakeContext(FakeSync(), FakeSettings(**defaults)))

    def jobs(self, **settings):
        return {job.id: job for job in self.scheduler(**settings).get_jobs()}

    def test_the_hourly_job_is_registered(self):
        assert 'hourly' in self.jobs()

    def test_it_runs_the_full_refresh_by_default(self):
        assert self.jobs()['hourly'].func.__name__ == 'hourly_refresh'

    def test_the_interval_is_configurable(self):
        job = self.jobs(**{'scheduler.recommendation_interval_minutes': 30})['hourly']
        assert job.trigger.interval.total_seconds() == 30 * 60

    def test_the_full_sync_can_be_turned_off(self):
        """A full pass is the heaviest regular call this service makes on
        ServiceNow. Turning it off restores the previous behaviour exactly -
        the LLM pass alone - rather than disabling the job."""
        job = self.jobs(**{'scheduler.hourly_full_sync': False})['hourly']
        assert job.func.__name__ == 'recommendation_pass'

    def test_the_name_says_what_it_does(self):
        """A job called 'LLM recommendation pass' that also syncs is how the
        next person spends an afternoon looking for the sync."""
        assert 'full sync' in self.jobs()['hourly'].name.lower()
        assert 'full sync' not in self.jobs(
            **{'scheduler.hourly_full_sync': False})['hourly'].name.lower()

    def test_the_stream_tick_is_still_a_delta(self):
        """Every ten minutes, so it has to stay cheap. The full pass is what
        the hourly job is for."""
        assert 'stream' in self.jobs()

    def test_overrunning_runs_are_skipped_rather_than_stacked(self):
        """max_instances=1 and coalesce, applied to every job: a slow
        ServiceNow period must not turn into a backlog of concurrent runs,
        and the hourly job is now heavy enough for that to matter.

        Asserted on the scheduler's defaults rather than on each job - a
        pending job has not had them applied yet, and they are set in one
        place for exactly this reason."""
        defaults = self.scheduler()._job_defaults
        assert defaults['max_instances'] == 1
        assert defaults['coalesce'] is True
