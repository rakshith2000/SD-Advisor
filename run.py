#!/usr/bin/env python3
"""Single entry point for every mode of the service.

    python run.py serve            # web UI + scheduler (the normal daemon)
    python run.py web              # web UI only
    python run.py sync [--full]    # one sync pass
    python run.py signals          # recompute signals for the aged backlog
    python run.py recommend [-n N] # LLM pass
    python run.py digest [--no-agents]
    python run.py backfill [--days 180]
    python run.py preview -o out.html
    python run.py adduser --username x --role LEAD
    python run.py passwd  --username x
    python run.py grant   --username x --role LEAD --groups "Service Desk"
    python run.py sync-users [--dry-run]   # reconcile accounts against Keycloak
    python run.py sla-map          # verify how each SLA definition classifies
    python run.py doctor           # check every dependency and exit

Kept as one file so the systemd unit, the operator and the cron fallback all
use the same code path.
"""

import argparse
import getpass
import json
import signal
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.context import get_context                     # noqa: E402
from core.logging_setup import get_logger                # noqa: E402
from core.timeutil import utc_now                        # noqa: E402

log = get_logger('run')


def _pretty(result) -> None:
    print(json.dumps(result, indent=2, default=str))


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_serve(args) -> int:
    """Web UI and scheduler in one process."""
    import uvicorn
    from scheduler.jobs import build_scheduler
    from web.app import create_app

    ctx = get_context(args.config)
    scheduler = build_scheduler(ctx)
    scheduler.start()
    log.info('Scheduler started with jobs: %s',
             ', '.join(job.id for job in scheduler.get_jobs()))

    stopping = threading.Event()

    def shutdown(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        log.info('Signal %s received - shutting the scheduler down', signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    app = create_app(args.config)
    try:
        uvicorn.run(app,
                    host=ctx.settings.get('web.host', '0.0.0.0'),
                    port=int(ctx.settings.get('web.port', 8444)),
                    log_config=None)
    finally:
        if not stopping.is_set():
            scheduler.shutdown(wait=False)
    return 0


def cmd_web(args) -> int:
    import uvicorn
    from web.app import create_app

    ctx = get_context(args.config)
    uvicorn.run(create_app(args.config),
                host=ctx.settings.get('web.host', '0.0.0.0'),
                port=int(ctx.settings.get('web.port', 8444)),
                log_config=None)
    return 0


def cmd_sync(args) -> int:
    ctx = get_context(args.config)
    _pretty(ctx.sync.run(full=args.full))
    return 0


def cmd_signals(args) -> int:
    from pipeline.orchestrator import Orchestrator

    ctx = get_context(args.config)
    _pretty(Orchestrator(ctx).refresh_signals())
    return 0


def cmd_recommend(args) -> int:
    from pipeline.orchestrator import Orchestrator

    ctx = get_context(args.config)
    _pretty(Orchestrator(ctx).refresh_recommendations(limit=args.limit, force=args.force))
    return 0


def cmd_digest(args) -> int:
    from scheduler.jobs import JobRunner

    ctx = get_context(args.config)
    _pretty(JobRunner(ctx).daily_digest(include_agents=not args.no_agents))
    return 0


def cmd_backfill(args) -> int:
    from ops.index_builder import IndexBuilder

    ctx = get_context(args.config)
    _pretty(IndexBuilder(ctx).full_backfill(days=args.days))
    return 0


def cmd_preview(args) -> int:
    from delivery.dispatch import Dispatcher

    ctx = get_context(args.config)
    html = Dispatcher(ctx).preview_lead_digest()

    if args.output:
        Path(args.output).write_text(html, encoding='utf-8')
        print(f'Digest written to {args.output}')
    else:
        print(html)
    return 0


def cmd_adduser(args) -> int:
    from web.auth import PasswordError, UserStore, validate_password

    ctx = get_context(args.config)
    store = UserStore(ctx.db)

    existing = ctx.db.query_one(
        'SELECT username FROM advisor_user WHERE username = %s', (args.username,))
    if existing:
        print(f"Account {args.username!r} already exists. Use 'run.py passwd' to change "
              f"its password.", file=sys.stderr)
        return 1

    if args.password:
        password = args.password
    else:
        password = getpass.getpass('Password: ')
        # getpass does not confirm, and there is no self-service reset, so a
        # typo here would otherwise create an account nobody can log into.
        if password != getpass.getpass('Retype password: '):
            print('Passwords did not match', file=sys.stderr)
            return 1

    try:
        validate_password(password)
    except PasswordError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    store.create(
        username=args.username, password=password,
        full_name=args.full_name or args.username,
        email=args.email or '', role=args.role,
        assignment_groups=args.groups or '')

    print(f'Created {args.role} account {args.username!r}')
    return 0


def cmd_passwd(args) -> int:
    """Change an existing account's password."""
    from web.auth import PasswordError, hash_password, validate_password

    ctx = get_context(args.config)

    user = ctx.db.query_one(
        'SELECT id, username, role FROM advisor_user WHERE username = %s', (args.username,))
    if not user:
        print(f'No account named {args.username!r}', file=sys.stderr)
        return 1

    password = args.password or getpass.getpass(f'New password for {args.username}: ')
    if not args.password and password != getpass.getpass('Retype password: '):
        print('Passwords did not match', file=sys.stderr)
        return 1

    try:
        validate_password(password)
    except PasswordError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    ctx.db.update('advisor_user', {'password_hash': hash_password(password)},
                  conditions=[{'col': 'id', 'op': 'eq', 'val': user['id']}])

    print(f"Password updated for {user['username']} ({user['role']})")
    return 0


def cmd_grant(args) -> int:
    """Set an account's role and assignment group scope directly.

    The administrator's path that does not go through the request workflow -
    needed for the first administrator, for granting ADMIN (which is
    deliberately not requestable), and whenever Keycloak is unavailable.
    """
    from web.auth import UserStore

    ctx = get_context(args.config)
    store = UserStore(ctx.db)

    account = store.live(args.username)
    if not account:
        print(f'No account named {args.username!r}', file=sys.stderr)
        return 1

    groups = [g.strip() for g in (args.groups or '').split(',') if g.strip()]
    if groups:
        known = {g.lower(): g for g in ctx.known_assignment_groups()}
        unknown = [g for g in groups if g.lower() not in known]
        if unknown:
            # A near-miss on a group name errors nowhere downstream; it simply
            # matches no tickets, and the board silently shows an empty board.
            print(f'Not a known assignment group: {", ".join(unknown)}', file=sys.stderr)
            print(f'Known: {", ".join(ctx.known_assignment_groups())}', file=sys.stderr)
            return 1
        groups = [known[g.lower()] for g in groups]

    try:
        store.set_role(args.username, args.role, groups or None, source='MANUAL')
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"{args.username}: {account['role']} -> {args.role.upper()}"
          f"{' scoped to ' + ', '.join(groups) if groups else ' (all groups)'}")
    if args.role.upper() in ('ADMIN', 'LEAD'):
        print('This account will now receive the daily digest and risk alerts.')
    return 0


def cmd_sync_users(args) -> int:
    """Reconcile accounts against Keycloak. Read-only against Keycloak."""
    from scheduler.jobs import JobRunner

    ctx = get_context(args.config)
    result = JobRunner(ctx).reconcile_users(dry_run=args.dry_run)

    if result.get('skipped'):
        print(f"Skipped: {result['skipped']}")
        return 0

    _pretty(result)

    if result.get('refused'):
        print('\nRefused: too many accounts appeared to have gone at once. Nothing '
              'was changed. See the log for the reasoning.', file=sys.stderr)
        return 2
    if result.get('errors'):
        print(f"\n{result['errors']} account(s) could not be checked or were "
              f"protected. See the log.", file=sys.stderr)
        return 1
    return 0


def cmd_sla_map(args) -> int:
    """Show how every SLA definition classifies, so it can be verified.

    contract_sla holds the definitions; task_sla attaches one to a ticket. What
    matters for scoring is which definitions count as the customer-facing
    resolution target - getting that wrong skews the largest component of the
    attention score, silently. This makes it visible.

    Exit codes, so this can be a cron canary:
        0  every definition explicitly mapped, no duration drift
        1  no definitions found for the table
        2  one or more definitions rely on the name-token fallback
        3  a definition's duration differs from the recorded value
    """
    ctx = get_context(args.config)
    definitions = ctx.sla.get_definitions(collection=args.table)

    if not definitions:
        print(f'No active SLA definitions found for table {args.table!r}')
        return 1

    name_width = max(len(d['name']) for d in definitions)
    print(f'{"KIND":<11} {"NAME":<{name_width}}  {"TYPE":<6} {"DURATION":<12} SOURCE')
    print('-' * (11 + name_width + 34))

    counts: dict = {}
    fallback = []
    drifted = []

    for entry in definitions:
        counts[entry['kind']] = counts.get(entry['kind'], 0) + 1
        if entry['source'] in ('name-token', 'unmatched'):
            fallback.append(entry)

        duration = entry['duration']
        if entry['duration_drift']:
            drifted.append(entry)
            duration += ' *'
        print(f"{entry['kind']:<11} {entry['name']:<{name_width}}  "
              f"{entry['type']:<6} {duration:<12} {entry['source']}")

    print()
    print('Totals: ' + ', '.join(f'{k}={v}' for k, v in sorted(counts.items())))

    if drifted:
        print()
        print(f'WARNING: {len(drifted)} definition(s) marked * have a different duration than')
        print('when this map was written. Every ticket under them is now scored against a')
        print('different target, which moves sla_pct_consumed and reorders the board:')
        for entry in drifted:
            print(f"    {entry['name']}: recorded {entry['recorded_duration']!r}, "
                  f"now {entry['duration']!r}")
        print('Confirm the change was intended, then update duration in sla.definitions.')

    include = ctx.sla.include_types
    print(f'Types counted: {", ".join(include) if include else "all (no filter configured)"}')

    excluded = [d for d in definitions if include and d['type'] not in include]
    if excluded:
        print(f'Excluded by type filter: {len(excluded)} '
              f'({", ".join(sorted({d["type"] for d in excluded}))})')

    if fallback:
        print()
        print(f'WARNING: {len(fallback)} definition(s) are not explicitly configured and rely')
        print('on matching words in the name. Add them to sla.definitions in conf.json:')
        for entry in fallback:
            marker = '  <-- UNMATCHED, defaults to OTHER' if entry['source'] == 'unmatched' else ''
            print(f'    "{entry["sys_id"]}": {{ "kind": "{entry["kind"]}", '
                  f'"name": "{entry["name"]}", "duration": "{entry["duration"]}" }},{marker}')
        return 2

    if drifted:
        return 3

    unverified = [d for d in definitions if not d['recorded_duration']]
    if unverified:
        print(f'\nAll definitions are explicitly configured. {len(unverified)} have no '
              f'recorded duration, so a retune in ServiceNow would go unnoticed.')
    else:
        print('\nAll definitions are explicitly configured, and every duration matches '
              'what was recorded.')
    return 0


def cmd_doctor(args) -> int:
    """Check every external dependency and report. Safe to run any time."""
    checks = []
    ok = True

    try:
        ctx = get_context(args.config)
    except Exception as exc:
        print(f'FAIL  configuration/context: {exc}')
        return 1

    def check(name, fn):
        nonlocal ok
        try:
            detail = fn()
            checks.append(('PASS', name, detail))
        except Exception as exc:
            ok = False
            checks.append(('FAIL', name, str(exc)[:200]))

    def llm_check():
        if ctx.llm is None:
            raise RuntimeError('Azure OpenAI unreachable - recommendations would fall back to rules')
        return f'reachable ({ctx.llm.chat_deployment})'

    def vault_check():
        status = ctx.vault.auth_status()
        if status.get('error'):
            raise RuntimeError(status['error'])

        detail = f"auth={status['method']}"
        ttl = status.get('ttl_seconds')
        if ttl:
            detail += f", token expires in {ttl / 3600.0:.1f}h"
            if status['method'] == 'token' and ttl < 7 * 86400:
                detail += ' (STATIC TOKEN - switch to approle)'
        if status['method'] == 'approle':
            if not status.get('renewer_running'):
                raise RuntimeError('AppRole renewer thread is not running')
            detail += ', auto-renewal active'
        return detail

    def web_check():
        base = str(ctx.settings.get('web.base_url', '')).rstrip('/')
        host = ctx.settings.get('web.host', '0.0.0.0')
        port = ctx.settings.get('web.port', 8444)

        if not base:
            raise RuntimeError(
                'web.base_url is empty - digest emails would be sent with no links back '
                'to the board')

        detail = f'{base} (serving {host}:{port})'
        if not base.lower().startswith('https'):
            # Not fatal - plain HTTP is a reasonable trial setup on an internal
            # network - but leads log in with a password, so say so plainly.
            detail += '  [HTTP: login credentials and session cookie are sent in cleartext]'
        return detail

    def groups_check():
        configured = ctx.settings.assignment_groups
        if not configured:
            return 'no filter configured - EVERY assignment group is in scope'

        unknown = ctx.incidents.unresolvable_groups()
        if unknown:
            raise RuntimeError(
                f'{unknown} do not match any active sys_user_group name exactly. '
                f'Every incident query is silently returning zero rows. '
                f'Find the real names with: python ops/find_groups.py "<term>"')
        return f'{len(configured)} group(s) resolved: {", ".join(configured)}'

    check('config', lambda: f'loaded {ctx.settings.path}')
    check('vault', vault_check)
    check('web url', web_check)
    check('snow groups', groups_check)
    check('database', lambda: (
        f"{ctx.db.query_one('SELECT COUNT(*) AS n FROM watched_ticket')['n']} tracked tickets"))
    def snow_check():
        aged = len(ctx.incidents.get_aged_open_incidents(ctx.settings.aged_after_days))
        detail = f'{aged} aged open'
        if not ctx.incidents.sample_resolved_within(30):
            detail += ('; NO incidents resolved in the last 30 days in scope - '
                       'backfill would index nothing')
        return detail

    check('servicenow', snow_check)
    check('llm', llm_check)
    check('vector index', lambda: (
        f"{ctx.index.load('incident')} incidents, {ctx.index.load('kb')} KB articles"))
    check('users', lambda: (
        f"{ctx.db.query_one('SELECT COUNT(*) AS n FROM advisor_user WHERE active = 1')['n']} active"))

    def admin_floor_check():
        """The one lockout this service cannot talk itself out of.

        Roles are written by the approval workflow and by the reconcile job, so
        a mistaken demotion or a misconfigured realm can leave no administrator
        at all - and nothing in the UI can promote one.
        """
        admins = ctx.db.query(
            "SELECT username FROM advisor_user WHERE active = 1 AND role = 'ADMIN'")
        if not admins:
            raise RuntimeError(
                'No active ADMIN account. Nobody can tune weights, decide access '
                'requests or administer users. Fix with: '
                'python run.py grant --username <name> --role ADMIN')

        names = ', '.join(a['username'] for a in admins)
        if len(admins) == 1:
            return (f'{names} - the ONLY administrator. A second one means a lockout '
                    f'is recoverable without the CLI.')
        return f'{len(admins)} administrators: {names}'

    check('admin floor', admin_floor_check)

    def access_requests_check():
        pending = ctx.db.query(
            "SELECT username, created_at FROM role_request WHERE status = 'PENDING' "
            " ORDER BY created_at")
        if not pending:
            return 'none pending'

        oldest = pending[0]
        age_days = (utc_now() - oldest['created_at']).days
        if age_days >= 7:
            raise RuntimeError(
                f'{len(pending)} access request(s) pending, oldest from '
                f'{oldest["username"]} is {age_days} days old. Either the '
                f'notification is not reaching the administrators, or nobody is '
                f'reading it - check runtime.shadow_mode_exempt includes '
                f'role_request, and that an active ADMIN has an email address.')
        return f'{len(pending)} pending, oldest {age_days}d'

    check('access requests', access_requests_check)

    mode = str(ctx.settings.get('auth.mode', 'local')).strip().lower()
    if mode in ('oidc', 'both'):
        def oidc_check():
            from web.oidc import OIDCProvider
            provider = OIDCProvider(ctx.settings, ctx.vault, 'doctor-probe')
            metadata = provider.metadata()

            missing = [e for e in ('authorization_endpoint', 'token_endpoint', 'jwks_uri')
                       if not metadata.get(e)]
            if missing:
                raise RuntimeError(f'The issuer does not advertise: {", ".join(missing)}')

            keys = provider._load_jwks()
            if not keys.keys:
                raise RuntimeError('The issuer publishes no signing keys')

            # Fetched rather than assumed: a missing client secret surfaces
            # here instead of on the first person who tries to sign in.
            provider.client_secret

            if not metadata.get('end_session_endpoint'):
                return (f'{provider.issuer} ({len(keys.keys)} signing keys) - no '
                        f'end_session_endpoint, so sign-out will not end the '
                        f'Keycloak session')
            return f'{provider.issuer} ({len(keys.keys)} signing keys)'

        check('oidc issuer', oidc_check)

        def redirect_check():
            from urllib.parse import urlparse
            configured = str(ctx.settings.require('auth.oidc.redirect_uri'))
            base = str(ctx.settings.get('web.base_url', ''))

            redirect_host = urlparse(configured).netloc.lower()
            base_host = urlparse(base).netloc.lower()
            if base_host and redirect_host != base_host:
                raise RuntimeError(
                    f'auth.oidc.redirect_uri points at {redirect_host!r} but '
                    f'web.base_url is {base_host!r}. Keycloak matches the redirect '
                    f'URI exactly, and the browser has to reach it - these must be '
                    f'the same host.')
            if not configured.lower().startswith('https'):
                raise RuntimeError(
                    f'{configured} is not HTTPS. The authorization code and the '
                    f'session cookie would cross the network in cleartext.')
            return configured

        check('oidc redirect', redirect_check)

        if ctx.settings.get('auth.reconcile.enabled', False):
            def reconcile_check():
                from core.keycloak import KeycloakAdmin
                status = KeycloakAdmin(ctx.settings, ctx.vault).ping()
                return f"realm {status['realm']} readable (view-users granted)"

            check('keycloak admin', reconcile_check)
        else:
            checks.append(('INFO', 'keycloak admin',
                           'reconcile disabled - a leaver stays on the digest until '
                           'deactivated by hand'))

    if (mode in ('oidc', 'both')
            and ctx.settings.get('auth.oidc.scope_from_servicenow', True)):
        def scope_source_check():
            """sys_user_grmember must be readable, or every new account lands
            with the fallback scope and nobody is told why."""
            status = ctx.directory.probe()
            if not status.get('readable'):
                raise RuntimeError(
                    f"sys_user_grmember is not readable by the integration user "
                    f"({status.get('error', 'unknown error')}). New single sign-on "
                    f"accounts would silently fall back to "
                    f"auth.oidc.default_viewer_groups. Either grant read access or "
                    f"set auth.oidc.scope_from_servicenow false to make that "
                    f"deliberate.")
            tracked = ctx.known_assignment_groups()
            return (f'sys_user_grmember readable; '
                    f'{len(tracked)} tracked group(s) to match against')

        check('snow group membership', scope_source_check)

    local_enabled = bool(ctx.settings.get('auth.local_login.enabled', True))
    allow = ', '.join(ctx.settings.get('auth.local_login.allow_roles', ['ADMIN', 'LEAD', 'VIEWER']))
    checks.append(('INFO', 'auth mode',
                   f'{mode}' + (f'; password sign-in for {allow}' if local_enabled
                                else '; password sign-in disabled')))

    audit = 'configured' if ctx.audit_db else 'disabled (coaching rollup will be omitted)'
    checks.append(('INFO', 'audit database link', audit))
    checks.append(('INFO', 'shadow mode',
                   'ON - no mail will be sent' if ctx.settings.shadow_mode else 'OFF - mail is live'))

    width = max(len(name) for _, name, _ in checks)
    for status, name, detail in checks:
        print(f'{status:<5} {name:<{width}}  {detail}')

    return 0 if ok else 1


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Aged Ticket Advisor')
    parser.add_argument('--config', help='path to conf.json (default config/conf.json)')

    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('serve', help='web UI and scheduler').set_defaults(func=cmd_serve)
    sub.add_parser('web', help='web UI only').set_defaults(func=cmd_web)

    p = sub.add_parser('sync', help='sync tickets from ServiceNow')
    p.add_argument('--full', action='store_true', help='full backlog rather than delta')
    p.set_defaults(func=cmd_sync)

    sub.add_parser('signals', help='recompute deterministic signals').set_defaults(func=cmd_signals)

    p = sub.add_parser('recommend', help='run the LLM recommendation pass')
    p.add_argument('-n', '--limit', type=int, help='cap the number of tickets')
    p.add_argument('--force', action='store_true', help='ignore the cache')
    p.set_defaults(func=cmd_recommend)

    p = sub.add_parser('digest', help='build and send the digests')
    p.add_argument('--no-agents', action='store_true', help='leads only')
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser('backfill', help='build the evidence index and baselines')
    p.add_argument('--days', type=int, default=180)
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser('preview', help='render the lead digest without sending')
    p.add_argument('-o', '--output', help='write to a file instead of stdout')
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser('adduser', help='create a UI account')
    p.add_argument('--username', required=True)
    p.add_argument('--password', help='prompted for if omitted')
    p.add_argument('--full-name')
    p.add_argument('--email')
    p.add_argument('--role', default='LEAD', choices=['ADMIN', 'LEAD', 'VIEWER'])
    p.add_argument('--groups', help='comma-separated assignment groups; blank means all')
    p.set_defaults(func=cmd_adduser)

    p = sub.add_parser('grant', help="set an account's role and group scope")
    p.add_argument('--username', required=True)
    p.add_argument('--role', required=True, choices=['ADMIN', 'LEAD', 'VIEWER'])
    p.add_argument('--groups', help='comma-separated assignment groups; blank means all')
    p.set_defaults(func=cmd_grant)

    p = sub.add_parser('sync-users',
                       help='reconcile accounts against Keycloak (read-only there)')
    p.add_argument('--dry-run', action='store_true',
                   help='print what would change without changing it')
    p.set_defaults(func=cmd_sync_users)

    p = sub.add_parser('passwd', help='change an account password')
    p.add_argument('--username', required=True)
    p.add_argument('--password', help='prompted for (twice) if omitted')
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser('sla-map', help='show how each SLA definition classifies')
    p.add_argument('--table', default='incident', help='contract_sla collection (default: incident)')
    p.set_defaults(func=cmd_sla_map)

    sub.add_parser('doctor', help='check every dependency').set_defaults(func=cmd_doctor)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.exception('%s failed', args.command)
        return 1


if __name__ == '__main__':
    sys.exit(main())
