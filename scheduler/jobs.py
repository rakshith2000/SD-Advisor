"""Scheduled work - the three cadences described in the design.

  stream        every 10 min   delta sync + signals + risk alerts   (no LLM)
  recommend     hourly         LLM pass over changed tickets
  digest        per cron entry lead digest, then agent digests
  maintenance   nightly        full sync, index refresh, baselines, tidy-up

APScheduler with coalescing and max_instances=1 per job: if a run overruns its
interval the next one is skipped rather than stacking up, which is the failure
mode that turns a slow ServiceNow morning into a thundering herd.
"""

import datetime
from typing import Any, Dict, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.logging_setup import get_logger
from delivery.dispatch import Dispatcher
from pipeline.orchestrator import Orchestrator

log = get_logger('scheduler')


class JobRunner:
    """The job bodies, callable directly from the CLI as well as the scheduler."""

    def __init__(self, context):
        self.ctx = context
        self.orchestrator = Orchestrator(context)
        self.dispatcher = Dispatcher(context)

    # -- tier 1 ------------------------------------------------------------

    def stream_tick(self) -> Dict[str, Any]:
        """Delta sync, recompute signals, fire any new risk alerts."""
        result: Dict[str, Any] = {'started_at': datetime.datetime.now()}
        try:
            result['sync'] = self.ctx.sync.run(full=False)
            result['signals'] = self.orchestrator.refresh_signals()

            alerts = self.orchestrator.detect_new_alerts()
            result['alerts'] = self.dispatcher.send_risk_alerts(alerts)
        except Exception:
            log.exception('Stream tick failed')
            result['error'] = True
        return result

    # -- tier 2 ------------------------------------------------------------

    def recommendation_pass(self, limit: Optional[int] = None) -> Dict[str, Any]:
        try:
            return self.orchestrator.refresh_recommendations(limit=limit)
        except Exception:
            log.exception('Recommendation pass failed')
            return {'error': True}

    def daily_digest(self, assignment_groups=None, include_agents: bool = True) -> Dict[str, Any]:
        """Refresh recommendations first so the digest is never stale."""
        result: Dict[str, Any] = {}
        try:
            result['sync'] = self.ctx.sync.run(full=True)
            result['signals'] = self.orchestrator.refresh_signals()
            result['recommendations'] = self.orchestrator.refresh_recommendations()
            result['lead_digest'] = self.dispatcher.send_lead_digest(assignment_groups)

            if include_agents:
                result['agent_digests'] = self.dispatcher.send_agent_digests(assignment_groups)
        except Exception:
            log.exception('Daily digest failed')
            result['error'] = True
        return result

    # -- maintenance -------------------------------------------------------

    def nightly_maintenance(self) -> Dict[str, Any]:
        """Full sync, rebuild the evidence index, refresh baselines, prune."""
        from ops.index_builder import IndexBuilder

        result: Dict[str, Any] = {}
        try:
            result['sync'] = self.ctx.sync.run(full=True)
        except Exception:
            log.exception('Nightly full sync failed')

        try:
            builder = IndexBuilder(self.ctx)
            result['incidents_indexed'] = builder.index_resolved_incidents(days=180)
            result['kb_indexed'] = builder.index_knowledge()
            result['baselines'] = builder.refresh_baselines(days=180)
        except Exception:
            log.exception('Index refresh failed')

        try:
            result['pruned'] = self.prune()
        except Exception:
            log.exception('Prune failed')

        return result

    def prune(self, retain_days: int = 180) -> Dict[str, int]:
        """Keep the signal history useful without letting it grow unbounded.

        ticket_signal is append-only and written every stream tick, so it is by
        far the fastest-growing table here.
        """
        statements = {
            'signals': 'DELETE FROM ticket_signal WHERE computed_at < DATE_SUB(NOW(), INTERVAL %s DAY)',
            'alerts': 'DELETE FROM alert_log WHERE fired_at < DATE_SUB(NOW(), INTERVAL %s DAY)',
            'recommendations': ('DELETE FROM recommendation WHERE superseded = 1 '
                                ' AND created_at < DATE_SUB(NOW(), INTERVAL %s DAY)'),
        }

        pruned = {name: self.ctx.db.execute(sql, (retain_days,))
                  for name, sql in statements.items()}

        pruned['suppressions'] = self.ctx.db.execute(
            'DELETE FROM suppression WHERE snoozed_until < DATE_SUB(NOW(), INTERVAL 30 DAY)')

        log.info('Pruned old rows: %s', pruned)
        return pruned


def build_scheduler(context) -> BackgroundScheduler:
    runner = JobRunner(context)
    settings = context.settings

    scheduler = BackgroundScheduler(
        timezone='UTC',
        job_defaults={'coalesce': True, 'max_instances': 1, 'misfire_grace_time': 300},
    )

    stream_minutes = int(settings.get('scheduler.stream_interval_minutes', 10))
    scheduler.add_job(runner.stream_tick, IntervalTrigger(minutes=stream_minutes),
                      id='stream', name='Delta sync, signals and risk alerts',
                      next_run_time=datetime.datetime.now() + datetime.timedelta(seconds=30))

    recommend_minutes = int(settings.get('scheduler.recommendation_interval_minutes', 60))
    scheduler.add_job(runner.recommendation_pass, IntervalTrigger(minutes=recommend_minutes),
                      id='recommend', name='LLM recommendation pass')

    for entry in settings.get('scheduler.digests', []) or []:
        name = entry.get('name', 'digest')
        scheduler.add_job(
            runner.daily_digest,
            CronTrigger.from_crontab(entry['cron'], timezone=entry.get('timezone', 'UTC')),
            id=f'digest-{name}', name=f'Digest: {name}',
            kwargs={'assignment_groups': entry.get('assignment_groups')},
        )
        log.info('Digest %r scheduled (%s %s)', name, entry['cron'],
                 entry.get('timezone', 'UTC'))

    maintenance_cron = settings.get('scheduler.stats_refresh_cron', '0 2 * * *')
    scheduler.add_job(runner.nightly_maintenance,
                      CronTrigger.from_crontab(maintenance_cron, timezone='UTC'),
                      id='maintenance', name='Nightly index and baseline refresh')

    return scheduler
