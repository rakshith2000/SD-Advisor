"""Scheduled work - the three cadences described in the design.

  stream        every 10 min   delta sync + signals + risk alerts   (no LLM)
  recommend     hourly         LLM pass over changed tickets
  digest        per cron entry lead digest, then agent digests
  maintenance   nightly        full sync, index refresh, baselines, retention

APScheduler with coalescing and max_instances=1 per job: if a run overruns its
interval the next one is skipped rather than stacking up, which is the failure
failure mode that would turn a slow ServiceNow period into a backlog of
concurrent runs.
"""

import datetime
from typing import Any, Dict, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.logging_setup import get_logger
from delivery.dispatch import Dispatcher
from pipeline.orchestrator import Orchestrator
from core.timeutil import utc_now

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
        result: Dict[str, Any] = {'started_at': utc_now()}
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

        try:
            result['expired_access_requests'] = self.expire_access_requests()
        except Exception:
            log.exception('Access request expiry failed')

        return result

    def reconcile_users(self, dry_run: bool = False) -> Dict[str, Any]:
        """Deactivate advisor accounts whose identity has gone from Keycloak.

        A leaver disabled in Entra can no longer sign in, but their row here
        stays active and leads_for() keeps mailing them the digest and every
        risk alert - incident descriptions and caller names - indefinitely.

        Three guards, all of which exist because the failure mode is silent.

        Local accounts are never touched. One of them is the break-glass
        administrator, and it is deliberately unknown to Keycloak - a job that
        deactivated anything it could not find would remove the single
        credential that still works when Keycloak is the thing that is down.

        A run that would deactivate more than max_deactivate_pct of accounts
        changes nothing. An expired service-account secret, a renamed realm and
        a Keycloak outage all present as "no user found", repeatedly; an
        unguarded loop reads that as everyone having left and silences the
        digest for the whole desk.

        The last active administrator is never deactivated. Nothing in the UI
        can restore one, so that would leave the service with no way back in
        short of the CLI.
        """
        from core.keycloak import KeycloakAdmin, KeycloakError

        result: Dict[str, Any] = {'checked': 0, 'deactivated': 0, 'missing': [],
                                  'errors': 0, 'dry_run': dry_run}

        if not self.ctx.settings.get('auth.reconcile.enabled', False):
            result['skipped'] = 'auth.reconcile.enabled is false'
            return result

        admin = KeycloakAdmin(self.ctx.settings, self.ctx.vault)

        accounts = self.ctx.db.retrieve('advisor_user', conditions=[
            {'col': 'active', 'op': 'eq', 'val': 1},
            {'col': 'auth_source', 'op': 'eq', 'val': 'OIDC'},
        ], order_by='username')
        result['checked'] = len(accounts)

        if not accounts:
            return result

        gone: List[Dict[str, Any]] = []
        for account in accounts:
            try:
                found = (admin.find_by_external_id(account['external_id'])
                         if account.get('external_id')
                         else admin.find_user(account['username']))
            except KeycloakError:
                # One lookup failing is not evidence that the person has left.
                # Counted, logged, and excluded from the deactivation set.
                log.exception('Could not check %s against Keycloak', account['username'])
                result['errors'] += 1
                continue

            if found and admin.is_active(found):
                self.ctx.db.update(
                    'advisor_user', {'last_seen_idp_at': utc_now()},
                    conditions=[{'col': 'id', 'op': 'eq', 'val': account['id']}])
            else:
                gone.append(account)

        result['missing'] = [a['username'] for a in gone]

        if not gone:
            return result

        ceiling = float(self.ctx.settings.get('auth.reconcile.max_deactivate_pct', 30))
        proportion = 100.0 * len(gone) / len(accounts)
        if proportion > ceiling:
            log.error(
                'Refusing to reconcile: %d of %d accounts (%.0f%%) appear to have gone '
                'from Keycloak, above the %.0f%% ceiling. That pattern is far more often '
                'an expired service-account secret, a renamed realm or an outage than '
                'a genuine departure. Nothing has been changed. Investigate, then '
                'rerun - or raise auth.reconcile.max_deactivate_pct if this really is '
                'a mass offboarding.',
                len(gone), len(accounts), proportion, ceiling)
            result['refused'] = True
            return result

        admins = {a['username'] for a in self.ctx.db.retrieve('advisor_user', conditions=[
            {'col': 'active', 'op': 'eq', 'val': 1},
            {'col': 'role', 'op': 'eq', 'val': 'ADMIN'},
        ])}

        for account in gone:
            if account['role'] == 'ADMIN' and len(admins) <= 1:
                log.error('%s is gone from Keycloak but is the only active administrator. '
                          'Leaving it enabled - promote another account first.',
                          account['username'])
                result['errors'] += 1
                continue

            if dry_run:
                log.info('[dry run] would deactivate %s (%s)',
                         account['username'], account['role'])
            else:
                self.ctx.db.update(
                    'advisor_user', {'active': 0},
                    conditions=[{'col': 'id', 'op': 'eq', 'val': account['id']}])
                log.warning('Deactivated %s - no longer present or enabled in Keycloak. '
                            'They will stop receiving the digest and risk alerts.',
                            account['username'])
            admins.discard(account['username'])
            result['deactivated'] += 1

        return result

    def expire_access_requests(self) -> int:
        """Close out role requests nobody decided.

        Imported locally: web.auth pulls in FastAPI, and the CLI entry points
        that run this job have no reason to load a web framework.

        Without the sweep the administrators' badge accumulates requests from
        people who have since moved on, and the one signal that the workflow is
        being ignored disappears into a standing count that nobody reads.
        """
        from web.access import RoleRequestService
        from web.auth import UserStore

        service = RoleRequestService(self.ctx.db, UserStore(self.ctx.db),
                                     self.ctx.settings)
        return service.expire_stale()

    def prune(self, retain_days: int = 180) -> Dict[str, int]:
        """Keep the signal history useful without letting it grow unbounded.

        ticket_signal is append-only and written every stream tick, so it is by
        far the fastest-growing table here.
        """
        statements = {
            'signals': 'DELETE FROM ticket_signal WHERE computed_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s DAY)',
            'alerts': 'DELETE FROM alert_log WHERE fired_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s DAY)',
            'recommendations': ('DELETE FROM recommendation WHERE superseded = 1 '
                                ' AND created_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s DAY)'),
        }

        pruned = {name: self.ctx.db.execute(sql, (retain_days,))
                  for name, sql in statements.items()}

        pruned['suppressions'] = self.ctx.db.execute(
            'DELETE FROM suppression WHERE snoozed_until < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 30 DAY)')

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
                      next_run_time=utc_now() + datetime.timedelta(seconds=30))

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

    if settings.get('auth.reconcile.enabled', False):
        # Deliberately ahead of the digest. An account deactivated here must
        # stop receiving mail on the same morning, not the next one - the whole
        # point is that a leaver's last digest was yesterday's.
        reconcile_cron = settings.get('auth.reconcile.cron', '0 6 * * *')
        scheduler.add_job(
            runner.reconcile_users,
            CronTrigger.from_crontab(reconcile_cron, timezone='Europe/London'),
            id='reconcile', name='Reconcile accounts against Keycloak')
        log.info('Account reconciliation scheduled (%s Europe/London)', reconcile_cron)

    return scheduler
