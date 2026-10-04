"""FastAPI service: the review UI, the JSON API and the optional push webhook.

Runs on its own port (8444 by default) so it never collides with the existing
Flask front end on 8443. Everything it exposes is read-only with respect to
ServiceNow; the only writes are to the advisor's own database - feedback,
snoozes and weight tuning.
"""

import datetime
import json
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup, escape

from core.context import get_context
from core.logging_setup import get_logger
from core.timeutil import (DEFAULT_DATETIME_FORMAT, DEFAULT_DISPLAY_TZ,
                          format_dt, timezone_choices, utc_now,
                          zone_abbreviation, zone_label)
from delivery.digest import (ACTION_LABELS, FLAG_LABELS,
                             PENDING_ACTION_OWNER_LABELS, DigestBuilder)
from delivery.dispatch import Dispatcher
from pipeline.orchestrator import Orchestrator
from pipeline.scoring import DEFAULT_WEIGHTS
from web import auth
from web.access import (ALREADY_DECIDED, NOT_FOUND, REFUSED,
                        AccessRequestError, RoleRequestService)
from web.auth import (SessionManager, UserStore, require_admin, require_lead,
                      require_user, require_write, safe_next)
from web.oidc import ID_TOKEN_COOKIE, STATE_COOKIE, OIDCError, OIDCProvider

log = get_logger('web.app')

WEB_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = WEB_DIR / 'templates'
STATIC_DIR = WEB_DIR / 'static'


def _session_secret(ctx) -> str:
    """Stable signing key: Vault first, then a generated file on disk."""
    try:
        value = ctx.vault.value(ctx.settings.get('vault.paths.web', 'sd_advisor_web'),
                                'secret_key')
        if value:
            return value
    except Exception:
        pass

    path = ctx.settings.resolve_path('web.secret_file', 'config/.session_secret')
    if path.exists():
        return path.read_text(encoding='utf-8').strip()

    generated = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generated, encoding='utf-8')
    try:
        path.chmod(0o600)
    except OSError:
        pass
    log.warning('Generated a new session secret at %s', path)
    return generated


def create_app(config_path: Optional[str] = None) -> FastAPI:
    ctx = get_context(config_path)

    app = FastAPI(title='Aged Ticket Advisor', docs_url='/api/docs',
                  openapi_url='/api/openapi.json')

    secret = _session_secret(ctx)
    sessions = SessionManager(ctx.settings, secret)
    users = UserStore(ctx.db)

    mode = str(ctx.settings.get('auth.mode', 'local')).strip().lower()
    oidc_enabled = mode in ('oidc', 'both')
    local_enabled = (mode in ('local', 'both')
                     and bool(ctx.settings.get('auth.local_login.enabled', True)))
    local_roles = {r.upper() for r in
                   ctx.settings.get('auth.local_login.allow_roles',
                                    ['ADMIN', 'LEAD', 'VIEWER'])}

    provider: Optional[OIDCProvider] = None
    if oidc_enabled:
        # Constructed eagerly so a misconfigured issuer is a startup failure
        # rather than a 500 on the first person who tries to sign in.
        provider = OIDCProvider(ctx.settings, ctx.vault, secret)
        log.info('Single sign-on enabled against %s (client %s)',
                 provider.issuer, provider.client_id)
    if not local_enabled and not oidc_enabled:
        raise RuntimeError(
            'auth.mode leaves no way to sign in. Set it to local, oidc or both.')
    # Passing the store in is what lets require_user reconcile each request
    # against the stored account, so a granted or revoked role applies on the
    # next click rather than at the next sign-in.
    auth.configure(sessions, users)

    builder = DigestBuilder(ctx)
    orchestrator = Orchestrator(ctx)
    dispatcher = Dispatcher(ctx)
    access = RoleRequestService(ctx.db, users, ctx.settings)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')

    # -----------------------------------------------------------------
    # display timezone
    #
    # Storage and comparison stay naive UTC everywhere. This is the one place
    # that is relaxed, at the edge, for a person reading a screen. Conversion
    # happens in the template filter and nowhere else, so a converted value
    # can never find its way back into a subtraction or a database write.
    # -----------------------------------------------------------------

    service_tz = str(ctx.settings.get('display.timezone', DEFAULT_DISPLAY_TZ))
    datetime_format = str(ctx.settings.get('display.datetime_format',
                                           DEFAULT_DATETIME_FORMAT))
    allow_user_tz = bool(ctx.settings.get('display.allow_user_timezone', True))
    tz_options = timezone_choices(ctx.settings.get('display.timezone_choices', []))

    def tz_for(user: Optional[Dict[str, Any]]) -> str:
        if allow_user_tz and user and user.get('timezone'):
            return str(user['timezone'])
        return service_tz

    @pass_context
    def localdt(context, value, fmt: Optional[str] = None,
                with_zone: bool = False) -> str:
        """Render a stored UTC timestamp in the reader's timezone.

        Takes the zone from the render context rather than an argument, so no
        template has to thread it through - and so a template that forgets
        still gets the configured default rather than raw UTC.
        """
        return format_dt(value, context.get('display_tz') or service_tz,
                         fmt or datetime_format, with_zone=with_zone)

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    def activity_html(value: str) -> Markup:
        """Render activity text with clickable links.

        Keeps plain text as the source of truth and only adds anchors for
        detected URLs. ServiceNow history sometimes carries forum-style [code]
        blocks; these are stripped so only the link remains.
        """
        if not value:
            return Markup('')

        text = str(value)

        # Drop [code]...[/code] markers that ServiceNow wraps around pasted
        # HTML. The content is still useful as text even when the tags are gone.
        text = text.replace('[code]', '').replace('[/code]', '')

        # Escape first so any HTML in the comment is shown literally rather than
        # executed. Anchors we add are Markup-wrapped below.
        escaped = escape(text)

        # Detect http(s) URLs and bare kb_view.do?... links.
        import re
        pattern = re.compile(r"(https?://\S+|\bkb_view\.do\S*)")

        snow_base = str(ctx.settings.get('servicenow.url', '')).rstrip('/')

        def repl(match: 're.Match') -> Markup:
            url = match.group(0)
            href = url
            if href.startswith('kb_view.do') and snow_base:
                href = snow_base + '/' + href
            return Markup(f'<a href="{href}" target="_blank" rel="noopener">{escape(url)}</a>')

        parts = []
        last = 0
        for m in pattern.finditer(escaped):
            parts.append(escaped[last:m.start()])
            parts.append(repl(m))
            last = m.end()
        parts.append(escaped[last:])

        return Markup('').join(parts)

    templates.env.filters['activity_html'] = activity_html
    templates.env.filters['localdt'] = localdt
    templates.env.globals.update({
        'ACTION_LABELS': ACTION_LABELS,
        'FLAG_LABELS': FLAG_LABELS,
        'PENDING_ACTION_OWNER_LABELS': PENDING_ACTION_OWNER_LABELS,
        'shadow_mode': ctx.settings.shadow_mode,
        'customer': ctx.settings.customer_name,
        'aged_after_days': ctx.settings.aged_after_days,
    })

    def render(request: Request, template: str, **context) -> HTMLResponse:
        context.setdefault('user', auth.current_user(request))
        context.setdefault('now', utc_now())

        zone = tz_for(context.get('user'))
        context.setdefault('display_tz', zone)
        context.setdefault('display_tz_label', zone_label(zone))
        context.setdefault('display_tz_abbrev', zone_abbreviation(zone))
        context.setdefault('timezone_options', tz_options)
        context.setdefault('allow_user_timezone', allow_user_tz)

        # The badge is the reason an access request cannot be lost. Shadow mode
        # suppresses most outbound mail, and an administrator may simply not
        # read the notification, so the UI carries the count on every page
        # rather than relying on the email having arrived.
        user = context.get('user') or {}
        if user.get('role') == 'ADMIN' and 'pending_access_requests' not in context:
            try:
                context['pending_access_requests'] = access.pending_count()
            except Exception:
                # A counter is not worth failing a page render over.
                log.debug('Could not count pending access requests')
                context['pending_access_requests'] = 0

        return templates.TemplateResponse(request, template, context)

    # -----------------------------------------------------------------
    # scope
    # -----------------------------------------------------------------

    def scope_of(user: Dict[str, Any]) -> List[str]:
        return users.visible_groups(user, ctx.known_assignment_groups())

    def narrow_scope(user: Dict[str, Any], group: Optional[str]) -> List[str]:
        """The groups to query, optionally narrowed to one the caller asked for.

        An out-of-scope `group` yields an empty list rather than being ignored.
        Ignoring it would silently widen the result to the user's whole scope,
        which reads as success and is the wrong direction to fail in.
        """
        visible = scope_of(user)
        if not group:
            return visible
        return [group] if users.can_see_group(user, group) else []

    def fetch_in_scope(user: Dict[str, Any], number: str) -> Dict[str, Any]:
        """A board row the caller is entitled to, or 404.

        404 rather than 403 throughout: 403 on an out-of-scope ticket confirms
        the incident exists and is being tracked, which is precisely the fact a
        scoped account should not be able to probe for.
        """
        row = ctx.db.query_one(
            'SELECT * FROM v_current_board WHERE incident_number = %s', (number,))
        if not row or not users.can_see_group(user, row.get('assignment_group')):
            raise HTTPException(status_code=404,
                                detail=f'{number} is not on the board')
        return row

    # -----------------------------------------------------------------
    # auth
    # -----------------------------------------------------------------

    @app.get('/login', response_class=HTMLResponse)
    def login_form(request: Request, next: str = '/board', error: str = ''):
        # Sanitised on the way in as well as on the way out, so the hidden
        # field in the form cannot carry an off-site value forward.
        return render(request, 'login.html', next=safe_next(next), error=error,
                      user=None, sso_enabled=oidc_enabled,
                      local_enabled=local_enabled,
                      local_note=('Administrator access only'
                                  if local_enabled and local_roles == {'ADMIN'} else ''))

    @app.post('/login')
    def login_submit(request: Request, username: str = Form(...),
                     password: str = Form(...), next: str = Form('/board')):
        destination = safe_next(next)

        if not local_enabled:
            raise HTTPException(status_code=404, detail='Password sign-in is not available')

        user = users.authenticate(username, password)
        if not user:
            log.info('Failed login attempt for %r', username[:40])
            return RedirectResponse(
                f'/login?error=Invalid+credentials&next={quote(destination, safe="/")}',
                status_code=303)

        # Enforced here rather than only in the template. Restricting the form
        # to administrators is the break-glass arrangement; a lead whose
        # password still works would otherwise bypass single sign-on entirely
        # by posting to this endpoint directly.
        if user['role'] not in local_roles:
            log.warning('Password sign-in refused for %r: role %s is not in '
                        'auth.local_login.allow_roles', user['username'], user['role'])
            return RedirectResponse(
                '/login?error=Please+sign+in+with+single+sign-on', status_code=303)

        response = RedirectResponse(destination, status_code=303)
        sessions.issue(response, user)
        return response

    # -- single sign-on ----------------------------------------------------

    @app.get('/oidc/login')
    def oidc_login(next: str = '/board'):
        if provider is None:
            raise HTTPException(status_code=404, detail='Single sign-on is not configured')

        try:
            url, state = provider.authorization_url(safe_next(next))
        except OIDCError as exc:
            log.error('Could not start single sign-on: %s', exc)
            return RedirectResponse(
                '/login?error=Single+sign-on+is+unavailable', status_code=303)

        response = RedirectResponse(url, status_code=303)
        # Short-lived and signed. There is no server-side session store, so
        # this cookie is the only thing tying the callback to a login this
        # service actually started.
        response.set_cookie(STATE_COOKIE, state, max_age=300, httponly=True,
                            samesite='lax', secure=sessions.secure, path='/oidc')
        return response

    def _initial_scope(identity: Dict[str, Any]) -> tuple:
        """Assignment groups to give a newly provisioned account.

        Derived from ServiceNow group membership, because someone already in
        an assignment group can already see those tickets in ServiceNow -
        mirroring it here grants no access they did not have, it only saves an
        administrator from typing it in. Role is a different matter and stays
        a human decision.

        Never raises. A slow or unreachable ServiceNow degrades to the
        configured default scope; the person has authenticated correctly and
        this is an enrichment, not a condition of signing in.
        """
        fallback = (list(ctx.settings.get('auth.oidc.default_viewer_groups', [])),
                    'MANUAL')

        if not bool(ctx.settings.get('auth.oidc.scope_from_servicenow', True)):
            return fallback

        try:
            derived = ctx.directory.assignment_groups_for(
                email=identity.get('email'),
                username=identity.get('username'),
                known_groups=ctx.known_assignment_groups())
        except Exception:
            log.exception('Could not derive a scope from ServiceNow for %s - '
                          'falling back to the configured default',
                          identity.get('username'))
            return fallback

        if not derived:
            # Not an error: an unmatched person, or one whose groups the
            # advisor does not track. Either way nothing was learned, so the
            # configured default applies and the board shows its no-scope
            # banner rather than an empty page with no explanation.
            return fallback

        return derived, 'SERVICENOW'

    @app.get('/oidc/callback')
    def oidc_callback(request: Request, code: str = '', state: str = '',
                      error: str = '', error_description: str = ''):
        if provider is None:
            raise HTTPException(status_code=404, detail='Single sign-on is not configured')

        if error:
            # Keycloak declined before we ever saw a code - access_denied when
            # the user cancels, and so on.
            log.info('Single sign-on returned %s: %s', error, error_description[:200])
            return RedirectResponse(
                f'/login?error={quote(error_description or error)}', status_code=303)

        try:
            result = provider.exchange(code, state, request.cookies.get(STATE_COOKIE))
            identity = provider.identity(result['claims'])
        except OIDCError as exc:
            log.warning('Single sign-on callback rejected: %s', exc)
            return RedirectResponse(
                '/login?error=Sign-in+could+not+be+completed', status_code=303)

        if not bool(ctx.settings.get('auth.oidc.jit_provisioning', True)):
            account = users.live(identity['username'])
            if not account:
                log.info('Refused an unprovisioned account: %s', identity['username'])
                return render(request, 'no_access.html', identity=identity, user=None)
        else:
            groups, source = _initial_scope(identity)
            account = users.jit_upsert(
                identity,
                default_role=str(ctx.settings.get('auth.oidc.default_role', 'VIEWER')),
                default_groups=groups,
                groups_source=source,
                refresh_groups=bool(ctx.settings.get(
                    'auth.oidc.refresh_scope_on_login', False)))

        if not account or not account.get('active'):
            log.info('Single sign-on succeeded for a disabled account: %s',
                     identity['username'])
            return render(request, 'no_access.html', identity=identity, user=None,
                          disabled=True)

        response = RedirectResponse(safe_next(result.get('next')), status_code=303)
        sessions.issue(response, account, expires_at=identity.get('expires_at'))

        # Path-scoped to /logout so a 1-2 KB token is not attached to every
        # request including static assets. Kept only so logout can be silent -
        # without id_token_hint Keycloak shows a confirmation screen.
        response.set_cookie(ID_TOKEN_COOKIE, result['id_token'],
                            max_age=sessions.max_age, httponly=True,
                            samesite='lax', secure=sessions.secure, path='/logout')
        response.delete_cookie(STATE_COOKIE, path='/oidc')

        log.info('%s signed in via single sign-on as %s',
                 account['username'], account['role'])
        return response

    @app.get('/logout')
    def logout(request: Request):
        """Ends the local session, and the Keycloak one.

        Clearing the cookie alone is not a logout: the Keycloak session
        survives, so the sign-in button goes straight back in with no prompt
        and on a shared machine the next person inherits the session.
        """
        destination = '/login'
        if provider is not None:
            base = str(ctx.settings.get('web.base_url', '')).rstrip('/')
            destination = provider.logout_url(
                request.cookies.get(ID_TOKEN_COOKIE), f'{base}/login' if base else '/login')

        response = RedirectResponse(destination, status_code=303)
        sessions.clear(response)
        response.delete_cookie(ID_TOKEN_COOKIE, path='/logout')
        return response

    # -----------------------------------------------------------------
    # profile and access requests
    # -----------------------------------------------------------------

    @app.get('/profile', response_class=HTMLResponse)
    def profile(request: Request, error: str = '', sent: int = 0,
                user: Dict[str, Any] = Depends(require_user)):
        account = users.live(user['username']) or {}
        return render(request, 'profile.html',
                      account=account,
                      scope=scope_of(user),
                      all_groups=ctx.known_assignment_groups(),
                      open_request=access.open_request_for(user['username']),
                      history=access.history_for(user['username']),
                      requestable=access.allowed_targets if access.enabled else [],
                      min_justification=access.min_justification,
                      error=error, sent=bool(sent))

    @app.post('/profile/request-role')
    async def request_role(request: Request,
                           user: Dict[str, Any] = Depends(require_user)):
        form = await request.form()
        try:
            opened = access.open_request(
                user={**user, 'email': (users.live(user['username']) or {}).get('email')},
                to_role=form.get('to_role', 'LEAD'),
                justification=form.get('justification', ''),
                requested_groups=form.getlist('groups'),
                valid_groups=ctx.known_assignment_groups())
        except AccessRequestError as exc:
            return RedirectResponse(f'/profile?error={quote(str(exc))}', status_code=303)

        # After the row exists, never before: an administrator following the
        # link must find the request there. A send failure is logged and leaves
        # the request standing, visible on the badge.
        try:
            dispatcher.send_role_request(opened)
            access.mark_notified(opened['id'])
        except Exception:
            log.exception('Could not notify administrators of access request %s',
                          opened.get('id'))

        return RedirectResponse('/profile?sent=1', status_code=303)

    @app.post('/preferences/timezone')
    def set_timezone(timezone: str = Form(''),
                     user: Dict[str, Any] = Depends(require_user)):
        """Save the reader's display timezone. Posted by the navigation bar.

        A POST rather than a GET because it changes stored state, and
        SameSite=Lax is what protects it - the same protection every other
        form here relies on.

        An empty value clears the preference, which is not the same as
        choosing UTC: it means "follow display.timezone", so the account keeps
        tracking the service default if that is ever changed.
        """
        if not allow_user_tz:
            raise HTTPException(status_code=404,
                                detail='Per-user timezones are disabled')

        if not users.set_timezone(user['username'], timezone):
            raise HTTPException(status_code=400,
                                detail=f'{timezone[:64]!r} is not a known timezone')

        zone = timezone or service_tz
        return JSONResponse({'timezone': timezone or None,
                             'label': zone_label(zone),
                             'abbreviation': zone_abbreviation(zone)})

    @app.post('/profile/request-role/{request_id}/cancel')
    def cancel_role_request(request_id: int,
                            user: Dict[str, Any] = Depends(require_user)):
        access.cancel(request_id, user)
        return RedirectResponse('/profile', status_code=303)

    @app.get('/admin/role-requests', response_class=HTMLResponse)
    def role_requests(request: Request, user: Dict[str, Any] = Depends(require_admin)):
        return render(request, 'access_requests.html',
                      pending=access.pending(),
                      recent=ctx.db.retrieve(
                          'role_request',
                          conditions=[{'col': 'status', 'op': 'ni',
                                       'val': ['PENDING']}],
                          order_by='decided_at DESC', limit=25))

    @app.get('/admin/role-requests/{request_id}', response_class=HTMLResponse)
    def role_request_detail(request: Request, request_id: int, error: str = '',
                            user: Dict[str, Any] = Depends(require_admin)):
        """Renders only. Following this link decides nothing.

        Mail scanners fetch URLs found in email to inspect them, so the link in
        the notification has to be safe to prefetch. The decision is a POST
        from this page.
        """
        record = access.get(request_id)
        if not record:
            raise HTTPException(status_code=404, detail='No such access request')

        return render(request, 'access_request_detail.html',
                      req=record,
                      all_groups=ctx.known_assignment_groups(),
                      prior=[r for r in access.history_for(record['username'], limit=20)
                             if r['id'] != record['id']],
                      is_own=record['username'] == user['username'],
                      error=error)

    @app.post('/admin/role-requests/{request_id}/approve')
    async def approve_role_request(request: Request, request_id: int,
                                   user: Dict[str, Any] = Depends(require_admin)):
        form = await request.form()
        result = access.approve(
            request_id, user,
            granted_groups=form.getlist('groups'),
            note=form.get('note', ''),
            valid_groups=ctx.known_assignment_groups())
        return _after_decision(result, request_id)

    @app.post('/admin/role-requests/{request_id}/reject')
    async def reject_role_request(request: Request, request_id: int,
                                  user: Dict[str, Any] = Depends(require_admin)):
        form = await request.form()
        result = access.reject(request_id, user, note=form.get('note', ''))
        return _after_decision(result, request_id)

    def _after_decision(result: Dict[str, Any], request_id: int) -> RedirectResponse:
        outcome = result.get('outcome')

        if outcome == NOT_FOUND:
            raise HTTPException(status_code=404, detail='No such access request')

        if outcome == REFUSED:
            return RedirectResponse(
                f'/admin/role-requests/{request_id}?error={quote(result["reason"])}',
                status_code=303)

        if outcome == ALREADY_DECIDED:
            # Every administrator is emailed at once, so two of them deciding
            # within seconds is ordinary. Say who got there first rather than
            # reporting a failure.
            decided = result.get('request') or {}
            message = (f'Already {decided.get("status", "decided").lower()} by '
                       f'{decided.get("decided_by", "another administrator")}.')
            return RedirectResponse(
                f'/admin/role-requests/{request_id}?error={quote(message)}',
                status_code=303)

        try:
            dispatcher.send_role_decision(result['request'])
        except Exception:
            log.exception('Could not email the decision for request %s', request_id)

        return RedirectResponse('/admin/role-requests', status_code=303)

    # -----------------------------------------------------------------
    # board
    # -----------------------------------------------------------------

    @app.get('/', response_class=HTMLResponse)
    def index():
        return RedirectResponse('/board', status_code=303)

    @app.get('/board', response_class=HTMLResponse)
    def board(request: Request,
              user: Dict[str, Any] = Depends(require_user),
              group: Optional[str] = None,
              agent: Optional[str] = None,
              ball: Optional[str] = None,
              flag: Optional[str] = None,
              action: Optional[str] = None,
              min_score: int = 0,
              show_snoozed: bool = False):

        all_groups = ctx.known_assignment_groups()
        visible = users.visible_groups(user, all_groups)
        groups = narrow_scope(user, group)

        # board_rows treats None as "every group", so an empty scope has to
        # short-circuit here. Passing `groups or None` - the previous shape -
        # would turn "entitled to nothing" into "entitled to everything".
        rows = ([] if not groups else
                builder.board_rows(assignment_groups=groups, agent=agent,
                                   include_snoozed=show_snoozed, min_score=min_score))

        if ball:
            rows = [r for r in rows if r.get('pending_action_owner') == ball]
        if flag:
            rows = [r for r in rows if flag in (r.get('risk_flags') or [])]
        if action:
            rows = [r for r in rows if r.get('recommended_action') == action]

        summary = builder.summarise(rows)
        movement = builder.movement_since(rows)

        return render(request, 'board.html',
                      rows=rows, summary=summary, movement=movement,
                      all_groups=all_groups, visible_groups=visible,
                      no_scope=not visible,
                      agents=sorted({r['assigned_to'] for r in rows if r.get('assigned_to')}),
                      filters={'group': group, 'agent': agent, 'ball': ball,
                               'flag': flag, 'action': action,
                               'min_score': min_score, 'show_snoozed': show_snoozed})

    # -----------------------------------------------------------------
    # ticket detail
    # -----------------------------------------------------------------

    @app.get('/ticket/{number}', response_class=HTMLResponse)
    def ticket_detail(request: Request, number: str,
                      user: Dict[str, Any] = Depends(require_user)):
        row = fetch_in_scope(user, number)
        decorated = builder.decorate(row)

        signal = ctx.db.query_one(
            'SELECT * FROM ticket_signal WHERE incident_number = %s '
            ' ORDER BY id DESC LIMIT 1', (number,)) or {}
        breakdown = _load_json(signal.get('score_breakdown'), {})

        recommendation = ctx.db.query_one(
            'SELECT * FROM recommendation WHERE incident_number = %s AND superseded = 0 '
            ' ORDER BY id DESC LIMIT 1', (number,)) or {}
        evidence = _load_json(recommendation.get('evidence'), [])

        feedback = ctx.db.retrieve(
            'recommendation_feedback',
            conditions=[{'col': 'incident_number', 'op': 'eq', 'val': number}],
            order_by='created_at DESC', limit=10)

        ticket = ctx.db.query_one(
            'SELECT * FROM watched_ticket WHERE incident_number = %s', (number,)) or {}

        timeline: List[Dict[str, Any]] = []
        try:
            from pipeline.signals import build_timeline
            history = ctx.incidents.get_history(ticket.get('sys_id', ''))
            timeline = build_timeline(history, ticket.get('caller_name') or '',
                                      ctx.settings.system_accounts)
        except Exception:
            log.debug('Could not load live history for %s', number)

        snooze = ctx.db.query_one(
            'SELECT * FROM suppression WHERE incident_number = %s', (number,))

        return render(request, 'ticket.html',
                      row=decorated, ticket=ticket, signal=signal,
                      breakdown=breakdown, recommendation=recommendation,
                      evidence=evidence, feedback=feedback,
                      timeline=list(reversed(timeline))[:60], snooze=snooze,
                      snow_url=str(ctx.settings.get('servicenow.url', '')).rstrip('/'))

    # -----------------------------------------------------------------
    # actions
    # -----------------------------------------------------------------

    @app.post('/ticket/{number}/feedback')
    def submit_feedback(number: str, decision: str = Form(...),
                        actual_action: str = Form(''), comment: str = Form(''),
                        user: Dict[str, Any] = Depends(require_write)):
        fetch_in_scope(user, number)

        valid = {'ACCEPTED', 'REJECTED', 'MODIFIED', 'NOT_APPLICABLE', 'DONE'}
        if decision not in valid:
            raise HTTPException(status_code=400, detail=f'decision must be one of {sorted(valid)}')

        recommendation = ctx.db.query_one(
            'SELECT id FROM recommendation WHERE incident_number = %s AND superseded = 0 '
            ' ORDER BY id DESC LIMIT 1', (number,))

        ctx.db.insert('recommendation_feedback', {
            'recommendation_id': recommendation['id'] if recommendation else None,
            'incident_number': number,
            'user_name': user['username'],
            'decision': decision,
            'actual_action': actual_action or None,
            'comment': comment or None,
            'created_at': utc_now(),
        })

        # Recording a decision implies the lead has dealt with it, so stop it
        # reappearing in tomorrow's digest.
        if decision in ('DONE', 'NOT_APPLICABLE'):
            _snooze(ctx, number, hours=48, reason=f'Marked {decision}',
                    username=user['username'])

        log.info('%s recorded %s on %s', user['username'], decision, number)
        return RedirectResponse(f'/ticket/{number}', status_code=303)

    @app.post('/ticket/{number}/snooze')
    def snooze_ticket(number: str, hours: int = Form(24), reason: str = Form(''),
                      user: Dict[str, Any] = Depends(require_write)):
        fetch_in_scope(user, number)
        _snooze(ctx, number, hours=hours, reason=reason, username=user['username'])
        return RedirectResponse(f'/ticket/{number}', status_code=303)

    @app.post('/ticket/{number}/unsnooze')
    def unsnooze_ticket(number: str, user: Dict[str, Any] = Depends(require_write)):
        fetch_in_scope(user, number)
        ctx.db.delete('suppression', [{'col': 'incident_number', 'op': 'eq', 'val': number}])
        return RedirectResponse(f'/ticket/{number}', status_code=303)

    @app.post('/ticket/{number}/reanalyse')
    def reanalyse(number: str, background: BackgroundTasks,
                  user: Dict[str, Any] = Depends(require_write)):
        fetch_in_scope(user, number)

        ticket = ctx.db.query_one(
            'SELECT * FROM watched_ticket WHERE incident_number = %s', (number,))
        if not ticket:
            raise HTTPException(status_code=404, detail=f'{number} is not tracked')

        background.add_task(_reanalyse_one, orchestrator, ticket)
        log.info('%s requested re-analysis of %s', user['username'], number)
        return RedirectResponse(f'/ticket/{number}?queued=1', status_code=303)

    # -----------------------------------------------------------------
    # agents & accuracy
    # -----------------------------------------------------------------

    @app.get('/agents', response_class=HTMLResponse)
    def agents_view(request: Request, user: Dict[str, Any] = Depends(require_user)):
        visible = scope_of(user)
        # `visible or None` would have meant board_rows saw None and returned
        # every group - the same inversion as on /board.
        rows = [] if not visible else builder.board_rows(assignment_groups=visible)
        rollup = builder.by_agent(rows)

        compliance = builder.compliance_context([entry['agent'] for entry in rollup][:60])
        for entry in rollup:
            entry['compliance'] = compliance.get(entry['agent'])

        return render(request, 'agents.html', rollup=rollup,
                      total=len(rows), has_compliance=bool(compliance))

    @app.get('/accuracy', response_class=HTMLResponse)
    def accuracy_view(request: Request, days: int = 30,
                      user: Dict[str, Any] = Depends(require_lead)):
        # Aggregated across every group, with no per-group breakdown to scope,
        # so entitlement is the role rather than the queue list.
        return render(request, 'accuracy.html', days=days, **_accuracy_metrics(ctx, days))

    # -----------------------------------------------------------------
    # settings
    # -----------------------------------------------------------------

    @app.get('/settings/weights', response_class=HTMLResponse)
    def weights_form(request: Request, saved: int = 0,
                     user: Dict[str, Any] = Depends(require_admin)):
        rows = ctx.db.retrieve('attention_weightage', order_by='weightage DESC')
        total = sum(int(r['weightage']) for r in rows if r['enabled'])
        return render(request, 'weights.html', rows=rows, total=total,
                      defaults=DEFAULT_WEIGHTS, saved=bool(saved))

    @app.post('/settings/weights')
    async def weights_save(request: Request, user: Dict[str, Any] = Depends(require_admin)):
        form = await request.form()
        for component in DEFAULT_WEIGHTS:
            if component not in form:
                continue
            try:
                value = max(0, min(100, int(form[component])))
            except (TypeError, ValueError):
                continue
            ctx.db.update('attention_weightage',
                          {'weightage': value,
                           'enabled': 1 if form.get(f'{component}_enabled') else 0},
                          conditions=[{'col': 'component', 'op': 'eq', 'val': component}])

        log.info('%s updated attention weights', user['username'])
        return RedirectResponse('/settings/weights?saved=1', status_code=303)

    # -----------------------------------------------------------------
    # digest preview & manual run
    # -----------------------------------------------------------------

    @app.get('/digest/preview', response_class=HTMLResponse)
    def digest_preview(user: Dict[str, Any] = Depends(require_lead)):
        # Scoped as well as role-gated: a lead restricted to one queue should
        # preview the digest they would receive, not the one the whole desk
        # receives.
        visible = scope_of(user)
        if not visible:
            raise HTTPException(status_code=403,
                                detail='Your account has no assignment group scope')
        return HTMLResponse(dispatcher.preview_lead_digest(assignment_groups=visible))

    @app.post('/digest/send')
    def digest_send(background: BackgroundTasks,
                    user: Dict[str, Any] = Depends(require_admin)):
        background.add_task(dispatcher.send_lead_digest, None, 'manual_digest')
        log.info('%s triggered a manual digest', user['username'])
        return RedirectResponse('/board?digest=queued', status_code=303)

    # -----------------------------------------------------------------
    # JSON API (Grafana, automation)
    # -----------------------------------------------------------------

    @app.get('/api/board')
    def api_board(user: Dict[str, Any] = Depends(require_user),
                  group: Optional[str] = None,
                  min_score: int = Query(0, ge=0, le=100)):
        # This route previously ignored the caller's scope entirely, so a lead
        # restricted to one queue could read every group through the API that
        # the board would not show them.
        groups = narrow_scope(user, group)
        rows = ([] if not groups else
                builder.board_rows(assignment_groups=groups, min_score=min_score))
        return JSONResponse(json.loads(json.dumps(rows, default=str)))

    @app.get('/api/ticket/{number}')
    def api_ticket(number: str, user: Dict[str, Any] = Depends(require_user)):
        row = fetch_in_scope(user, number)
        return JSONResponse(json.loads(json.dumps(builder.decorate(row), default=str)))

    @app.get('/api/accuracy')
    def api_accuracy(days: int = 30, user: Dict[str, Any] = Depends(require_lead)):
        return JSONResponse(json.loads(json.dumps(_accuracy_metrics(ctx, days), default=str)))

    @app.get('/healthz')
    def healthz():
        checks: Dict[str, Any] = {'status': 'ok'}
        try:
            ctx.db.query_one('SELECT 1 AS ok')
            checks['database'] = 'ok'
        except Exception as exc:
            checks['status'] = 'degraded'
            checks['database'] = str(exc)[:200]

        watermark = None
        try:
            watermark = ctx.db.get_sync_watermark('incident_delta')
        except Exception:
            pass
        checks['last_sync'] = watermark.isoformat() if watermark else None
        checks['llm'] = 'ok' if ctx.llm is not None else 'unavailable'
        checks['shadow_mode'] = ctx.settings.shadow_mode

        code = 200 if checks['status'] == 'ok' else 503
        return JSONResponse(checks, status_code=code)

    # -----------------------------------------------------------------
    # optional ServiceNow push webhook
    # -----------------------------------------------------------------

    @app.post('/webhook/snow')
    async def snow_webhook(request: Request, background: BackgroundTasks):
        """Drop-in upgrade from polling to true push.

        A ServiceNow Business Rule posts {"number": "INC..."} here on update.
        Shared-secret header rather than a session, since ServiceNow cannot
        hold a cookie.
        """
        expected = None
        try:
            expected = ctx.vault.value(
                ctx.settings.get('vault.paths.web', 'sd_advisor_web'), 'webhook_token')
        except Exception:
            pass

        if not expected:
            raise HTTPException(status_code=503, detail='Webhook not configured')
        if request.headers.get('x-advisor-token') != expected:
            raise HTTPException(status_code=401, detail='Bad token')

        payload = await request.json()
        number = str(payload.get('number') or '').strip()
        if not number:
            raise HTTPException(status_code=400, detail='number is required')

        background.add_task(_refresh_single, orchestrator, ctx, number)
        return {'accepted': number}

    log.info('Web application constructed')
    return app


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return fallback


def _snooze(ctx, number: str, hours: int, reason: str, username: str) -> None:
    until = utc_now() + datetime.timedelta(hours=max(1, min(int(hours), 720)))
    ctx.db.upsert('suppression', {
        'incident_number': number,
        'snoozed_until': until,
        'reason': (reason or '')[:255] or None,
        'created_by': username,
        'created_at': utc_now(),
    }, update_columns=['snoozed_until', 'reason', 'created_by', 'created_at'])
    log.info('%s snoozed %s until %s', username, number, until)


def _reanalyse_one(orchestrator: Orchestrator, ticket: Dict[str, Any]) -> None:
    try:
        orchestrator.refresh_signals([ticket])
        orchestrator.refresh_recommendations([ticket], force=True)
    except Exception:
        log.exception('Manual re-analysis failed for %s', ticket.get('incident_number'))


def _refresh_single(orchestrator: Orchestrator, ctx, number: str) -> None:
    try:
        raw = ctx.incidents.get_by_number(number)
        if not raw:
            return
        from pipeline.sync import flatten_incident
        record = flatten_incident(raw)
        record['last_synced_at'] = utc_now()
        record['first_seen_at'] = utc_now()
        ctx.db.upsert('watched_ticket', record,
                      update_columns=[c for c in record if c not in
                                      ('incident_number', 'first_seen_at')])

        ticket = ctx.db.query_one(
            'SELECT * FROM watched_ticket WHERE incident_number = %s', (number,))
        if ticket:
            orchestrator.refresh_signals([ticket])
    except Exception:
        log.exception('Webhook refresh failed for %s', number)


def _accuracy_metrics(ctx, days: int) -> Dict[str, Any]:
    """The numbers that answer "is this working well enough to trust"."""
    since = utc_now() - datetime.timedelta(days=max(1, days))

    overall = ctx.db.query_one("""
        SELECT COUNT(*) AS total,
               SUM(decision = 'ACCEPTED')       AS accepted,
               SUM(decision = 'MODIFIED')       AS modified,
               SUM(decision = 'REJECTED')       AS rejected,
               SUM(decision = 'NOT_APPLICABLE') AS not_applicable,
               SUM(decision = 'DONE')           AS done
          FROM recommendation_feedback
         WHERE created_at >= %s
    """, (since,)) or {}

    total = int(overall.get('total') or 0)
    accepted = int(overall.get('accepted') or 0)
    modified = int(overall.get('modified') or 0)
    rejected = int(overall.get('rejected') or 0)
    not_applicable = int(overall.get('not_applicable') or 0)

    # Agreement = the lead did what we suggested, exactly or with edits.
    agreement = round(100.0 * (accepted + modified) / total, 1) if total else None
    # Actionable precision = of the tickets we flagged, how many mattered.
    judged = accepted + modified + rejected + not_applicable
    precision = round(100.0 * (accepted + modified) / judged, 1) if judged else None

    by_action = ctx.db.query("""
        SELECT r.recommended_action                    AS action,
               COUNT(*)                                AS judged,
               SUM(f.decision IN ('ACCEPTED','MODIFIED')) AS agreed,
               ROUND(AVG(r.confidence), 3)             AS avg_confidence
          FROM recommendation_feedback f
          JOIN recommendation r ON r.id = f.recommendation_id
         WHERE f.created_at >= %s
         GROUP BY r.recommended_action
         ORDER BY judged DESC
    """, (since,))

    for row in by_action:
        judged_count = int(row['judged'] or 0)
        row['agreement'] = (round(100.0 * int(row['agreed'] or 0) / judged_count, 1)
                            if judged_count else None)

    coverage = ctx.db.query_one("""
        SELECT COUNT(DISTINCT r.incident_number) AS recommended,
               COUNT(DISTINCT f.incident_number) AS reviewed
          FROM recommendation r
          LEFT JOIN recommendation_feedback f
                 ON f.incident_number = r.incident_number
                AND f.created_at >= %s
         WHERE r.created_at >= %s AND r.superseded = 0
    """, (since, since)) or {}

    # A breach nobody was warned about is the metric that gets skipped.
    false_quiet = ctx.db.query_one("""
        SELECT COUNT(*) AS missed
          FROM ticket_signal s
         WHERE s.computed_at >= %s
           AND s.sla_breached = 1
           AND NOT EXISTS (
                SELECT 1 FROM alert_log a
                 WHERE a.incident_number = s.incident_number
                   AND a.alert_type IN ('SLA_AT_RISK','SLA_BREACHED')
                   AND a.fired_at < s.computed_at)
    """, (since,)) or {}

    volume = ctx.db.query("""
        SELECT DATE(created_at) AS day, COUNT(*) AS n
          FROM recommendation
         WHERE created_at >= %s
         GROUP BY DATE(created_at)
         ORDER BY day
    """, (since,))

    return {
        'total_feedback': total,
        'accepted': accepted,
        'modified': modified,
        'rejected': rejected,
        'not_applicable': not_applicable,
        'agreement_pct': agreement,
        'precision_pct': precision,
        'by_action': by_action,
        'recommended': int(coverage.get('recommended') or 0),
        'reviewed': int(coverage.get('reviewed') or 0),
        'review_coverage_pct': (
            round(100.0 * int(coverage.get('reviewed') or 0) / int(coverage['recommended']), 1)
            if coverage.get('recommended') else None),
        'false_quiet': int(false_quiet.get('missed') or 0),
        'volume': volume,
        'target_agreement': 80,
        'target_precision': 85,
    }


app = None  # populated by run.py / uvicorn factory
