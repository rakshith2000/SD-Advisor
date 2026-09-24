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

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.context import get_context
from core.logging_setup import get_logger
from delivery.digest import ACTION_LABELS, BALL_LABELS, FLAG_LABELS, DigestBuilder
from delivery.dispatch import Dispatcher
from pipeline.orchestrator import Orchestrator
from pipeline.scoring import DEFAULT_WEIGHTS
from web import auth
from web.auth import SessionManager, UserStore, require_admin, require_user, require_write

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

    sessions = SessionManager(ctx.settings, _session_secret(ctx))
    auth.configure(sessions)
    users = UserStore(ctx.db)

    builder = DigestBuilder(ctx)
    orchestrator = Orchestrator(ctx)
    dispatcher = Dispatcher(ctx)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.globals.update({
        'ACTION_LABELS': ACTION_LABELS,
        'FLAG_LABELS': FLAG_LABELS,
        'BALL_LABELS': BALL_LABELS,
        'shadow_mode': ctx.settings.shadow_mode,
        'customer': ctx.settings.customer_name,
        'aged_after_days': ctx.settings.aged_after_days,
    })

    def render(request: Request, template: str, **context) -> HTMLResponse:
        context.setdefault('user', auth.current_user(request))
        context.setdefault('now', datetime.datetime.now())
        return templates.TemplateResponse(request, template, context)

    # -----------------------------------------------------------------
    # auth
    # -----------------------------------------------------------------

    @app.get('/login', response_class=HTMLResponse)
    def login_form(request: Request, next: str = '/board', error: str = ''):
        return render(request, 'login.html', next=next, error=error, user=None)

    @app.post('/login')
    def login_submit(request: Request, username: str = Form(...),
                     password: str = Form(...), next: str = Form('/board')):
        user = users.authenticate(username, password)
        if not user:
            log.info('Failed login attempt for %r', username[:40])
            return RedirectResponse(f'/login?error=Invalid+credentials&next={next}',
                                    status_code=303)

        response = RedirectResponse(next or '/board', status_code=303)
        sessions.issue(response, user)
        return response

    @app.get('/logout')
    def logout():
        response = RedirectResponse('/login', status_code=303)
        sessions.clear(response)
        return response

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
        groups = [group] if group else visible

        rows = builder.board_rows(assignment_groups=groups or None, agent=agent,
                                  include_snoozed=show_snoozed, min_score=min_score)

        if ball:
            rows = [r for r in rows if r.get('ball_in_court') == ball]
        if flag:
            rows = [r for r in rows if flag in (r.get('risk_flags') or [])]
        if action:
            rows = [r for r in rows if r.get('recommended_action') == action]

        summary = builder.summarise(rows)
        movement = builder.movement_since(rows)

        return render(request, 'board.html',
                      rows=rows, summary=summary, movement=movement,
                      all_groups=all_groups, visible_groups=visible,
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
        row = ctx.db.query_one(
            'SELECT * FROM v_current_board WHERE incident_number = %s', (number,))
        if not row:
            raise HTTPException(status_code=404, detail=f'{number} is not on the board')

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
            'created_at': datetime.datetime.now(),
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
        _snooze(ctx, number, hours=hours, reason=reason, username=user['username'])
        return RedirectResponse(f'/ticket/{number}', status_code=303)

    @app.post('/ticket/{number}/unsnooze')
    def unsnooze_ticket(number: str, user: Dict[str, Any] = Depends(require_write)):
        ctx.db.delete('suppression', [{'col': 'incident_number', 'op': 'eq', 'val': number}])
        return RedirectResponse(f'/ticket/{number}', status_code=303)

    @app.post('/ticket/{number}/reanalyse')
    def reanalyse(number: str, background: BackgroundTasks,
                  user: Dict[str, Any] = Depends(require_write)):
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
        visible = users.visible_groups(user, ctx.known_assignment_groups())
        rows = builder.board_rows(assignment_groups=visible or None)
        rollup = builder.by_agent(rows)

        compliance = builder.compliance_context([entry['agent'] for entry in rollup][:60])
        for entry in rollup:
            entry['compliance'] = compliance.get(entry['agent'])

        return render(request, 'agents.html', rollup=rollup,
                      total=len(rows), has_compliance=bool(compliance))

    @app.get('/accuracy', response_class=HTMLResponse)
    def accuracy_view(request: Request, days: int = 30,
                      user: Dict[str, Any] = Depends(require_user)):
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
    def digest_preview(user: Dict[str, Any] = Depends(require_user)):
        return HTMLResponse(dispatcher.preview_lead_digest())

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
        rows = builder.board_rows(assignment_groups=[group] if group else None,
                                  min_score=min_score)
        return JSONResponse(json.loads(json.dumps(rows, default=str)))

    @app.get('/api/ticket/{number}')
    def api_ticket(number: str, user: Dict[str, Any] = Depends(require_user)):
        row = ctx.db.query_one(
            'SELECT * FROM v_current_board WHERE incident_number = %s', (number,))
        if not row:
            raise HTTPException(status_code=404, detail='Not tracked')
        return JSONResponse(json.loads(json.dumps(builder.decorate(row), default=str)))

    @app.get('/api/accuracy')
    def api_accuracy(days: int = 30, user: Dict[str, Any] = Depends(require_user)):
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
    until = datetime.datetime.now() + datetime.timedelta(hours=max(1, min(int(hours), 720)))
    ctx.db.upsert('suppression', {
        'incident_number': number,
        'snoozed_until': until,
        'reason': (reason or '')[:255] or None,
        'created_by': username,
        'created_at': datetime.datetime.now(),
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
        record['last_synced_at'] = datetime.datetime.now()
        record['first_seen_at'] = datetime.datetime.now()
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
    since = datetime.datetime.now() - datetime.timedelta(days=max(1, days))

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
                   AND a.alert_type IN ('SLA_JEOPARDY','SLA_BREACHED')
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
