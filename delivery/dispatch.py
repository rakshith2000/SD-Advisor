"""Render and send.

Sits between DigestBuilder (what to say) and Mailer (how to send it), so
templates are the only place that knows about presentation and the scheduler
only has to call one method.

Recipient resolution is deliberately conservative: leads come from
advisor_user, agents only get their own tickets, and if an address cannot be
resolved the digest still goes to the leads rather than silently vanishing.
"""

import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.logging_setup import get_logger
from delivery.digest import DigestBuilder
from delivery.mailer import Mailer
from core.timeutil import (DEFAULT_DATETIME_FORMAT, DEFAULT_DISPLAY_TZ,
                          format_dt, utc_now, zone_abbreviation, zone_label)

log = get_logger('delivery.dispatch')

TEMPLATE_DIR = Path(__file__).resolve().parent / 'templates'


def build_environment(settings=None) -> Environment:
    """Jinja environment for the outbound mail templates.

    An email is rendered once and delivered to everyone on the list, so it
    cannot follow a per-person timezone the way the board does. It uses the
    service-wide display.timezone and says so in a footer - a timestamp with
    no zone beside it is worse than one in the wrong zone, because the reader
    has no way to tell which they are looking at.
    """
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(['html', 'xml']),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    zone = DEFAULT_DISPLAY_TZ
    fmt = DEFAULT_DATETIME_FORMAT
    if settings is not None:
        zone = str(settings.get('display.timezone', zone))
        fmt = str(settings.get('display.datetime_format', fmt))

    # Same name as before so existing templates keep working, but it now
    # converts out of UTC instead of printing the stored value as-is.
    env.filters['datefmt'] = lambda value, f=None: format_dt(value, zone, f or fmt)
    env.filters['localdt'] = env.filters['datefmt']

    env.globals.update({
        'display_tz': zone,
        'display_tz_label': zone_label(zone),
        'display_tz_abbrev': zone_abbreviation(zone),
    })
    return env


class Dispatcher:
    def __init__(self, context):
        self.ctx = context
        self.settings = context.settings
        self.db = context.db
        self.builder = DigestBuilder(context)
        self.mailer = Mailer(context.settings, context.vault, context.db)
        self.env = build_environment(context.settings)

    # -- recipients --------------------------------------------------------

    def leads_for(self, assignment_groups: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        users = self.db.retrieve('advisor_user', conditions=[
            {'col': 'active', 'op': 'eq', 'val': 1},
            {'col': 'role', 'op': 'i', 'val': ['ADMIN', 'LEAD']},
        ])

        if not assignment_groups:
            return [u for u in users if u.get('email')]

        wanted = {g.strip().lower() for g in assignment_groups}
        matched = []
        for user in users:
            if not user.get('email'):
                continue
            scoped = [g.strip().lower() for g in (user.get('assignment_groups') or '').split(',') if g.strip()]
            # No group restriction on the user means "all groups".
            if not scoped or wanted & set(scoped):
                matched.append(user)
        return matched

    def admins(self) -> List[Dict[str, Any]]:
        """Administrators with a deliverable address.

        Unscoped by assignment group on purpose: an access request is about the
        service, not about a queue, and every administrator should be able to
        decide it.
        """
        return [u for u in self.db.retrieve('advisor_user', conditions=[
            {'col': 'active', 'op': 'eq', 'val': 1},
            {'col': 'role', 'op': 'eq', 'val': 'ADMIN'},
        ], order_by='username') if u.get('email')]

    # -- access requests ---------------------------------------------------

    def send_role_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Tell every administrator that someone is waiting on a decision.

        The buttons in this message are deep links to the decision page, not
        actions. They must stay that way: mail security scanners - Defender for
        Office 365 Safe Links among them - fetch URLs found in email to inspect
        them, so a link that approved on GET would approve every request within
        seconds of sending, attributed to nobody. The link renders a page; the
        grant is a POST from an authenticated administrator's session.
        """
        recipients = [a['email'] for a in self.admins()]
        if not recipients:
            # Not recoverable by waiting - the request sits until someone looks
            # at the badge in the UI, which is why that badge exists.
            log.error('Access request %s from %s has no administrator to notify - '
                      'no active ADMIN account has an email address',
                      request.get('id'), request.get('username'))
            return {'sent': 0, 'reason': 'no_admins'}

        html = self.env.get_template('role_request.html').render(
            request=request,
            base_url=self.builder.base_url,
            generated_at=utc_now(),
            shadow_mode=self.settings.shadow_mode,
        )

        who = request.get('full_name') or request.get('username')
        subject = f"Access request: {who} - {request.get('to_role', '').title()}"

        sent = self.mailer.send(
            to=recipients, subject=subject, html_body=html,
            run_type='role_request', audience='admin')
        return {'sent': int(sent), 'recipients': recipients}

    def send_role_decision(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Close the loop with the person who asked.

        Without this the requester has no way to tell the difference between
        approved, declined, and nobody having looked yet.
        """
        if not request.get('email'):
            log.info('No address for %s - decision not emailed', request.get('username'))
            return {'sent': 0, 'reason': 'no_address'}

        html = self.env.get_template('role_decision.html').render(
            request=request,
            base_url=self.builder.base_url,
            generated_at=utc_now(),
            shadow_mode=self.settings.shadow_mode,
        )

        approved = request.get('status') == 'APPROVED'
        subject = (f"Access request {'approved' if approved else 'declined'}: "
                   f"{request.get('to_role', '').title()}")

        sent = self.mailer.send(
            to=[request['email']], subject=subject, html_body=html,
            run_type='role_decision', audience='user')
        return {'sent': int(sent)}

    # -- lead digest -------------------------------------------------------

    def send_lead_digest(self, assignment_groups: Optional[List[str]] = None,
                         run_type: str = 'daily_digest') -> Dict[str, Any]:
        groups = assignment_groups or self.settings.assignment_groups
        max_tickets = int(self.settings.get('thresholds.digest_max_tickets_per_agent', 25))

        payload = self.builder.build_lead_digest(groups, max_tickets=max_tickets)
        payload['aged_after_days'] = self.settings.aged_after_days

        if not payload['tickets']:
            log.info('Nothing aged in %s - no digest sent', groups or 'all groups')
            return {'sent': 0, 'tickets': 0, 'reason': 'empty'}

        html = self.env.get_template('lead_digest.html').render(**payload)

        summary = payload['summary']
        subject = (
            f"Aged ticket review - {summary['total']} open, "
            f"{summary['service_desk_owned']} awaiting Service Desk action"
        )
        if summary['sla_breached']:
            subject += f", {summary['sla_breached']} SLA breached"

        recipients = [u['email'] for u in self.leads_for(groups)]
        if not recipients:
            log.warning('No lead recipients configured - digest rendered but undeliverable')
            return {'sent': 0, 'tickets': len(payload['tickets']), 'reason': 'no_recipients'}

        sent = self.mailer.send(
            to=recipients, subject=subject, html_body=html,
            run_type=run_type, audience='lead',
            ticket_count=len(payload['tickets']),
            new_count=payload['movement']['new_count'],
        )
        return {'sent': int(sent), 'tickets': len(payload['tickets']),
                'recipients': recipients}

    # -- agent digest ------------------------------------------------------

    def send_agent_digests(self, assignment_groups: Optional[List[str]] = None,
                           min_tickets: int = 1) -> Dict[str, Any]:
        """One focused email per agent who has aged tickets.

        Agents see only their own queue - the ranking and the drafted text,
        without the cross-team comparison that belongs to the lead view.
        """
        groups = assignment_groups or self.settings.assignment_groups
        rows = self.builder.board_rows(assignment_groups=groups)
        rollup = self.builder.by_agent(rows)

        directory = self._agent_directory()
        sent = skipped = 0

        for entry in rollup:
            agent = entry['agent']
            if agent == '(unassigned)' or entry['ticket_count'] < min_tickets:
                continue

            email = directory.get(agent.strip().lower())
            if not email:
                skipped += 1
                continue

            payload = self.builder.build_agent_digest(agent)
            payload['aged_after_days'] = self.settings.aged_after_days
            html = self.env.get_template('agent_digest.html').render(**payload)

            subject = f"Your aged tickets - {entry['ticket_count']} open"
            if entry['sla_breached']:
                subject += f", {entry['sla_breached']} SLA breached"

            if self.mailer.send(to=[email], subject=subject, html_body=html,
                                run_type='agent_digest', audience='agent',
                                ticket_count=entry['ticket_count']):
                sent += 1

        log.info('Agent digests: %d sent, %d skipped for want of an address', sent, skipped)
        return {'sent': sent, 'skipped': skipped, 'agents': len(rollup)}

    def _agent_directory(self) -> Dict[str, str]:
        """Map agent display name -> email.

        Sourced from advisor_user first; anything unmatched is looked up once
        against sys_user and cached in advisor_user by the bootstrap script.
        """
        rows = self.db.retrieve('advisor_user', columns=['full_name', 'username', 'email'],
                                conditions=[{'col': 'active', 'op': 'eq', 'val': 1}])
        directory: Dict[str, str] = {}
        for row in rows:
            if not row.get('email'):
                continue
            for key in (row.get('full_name'), row.get('username')):
                if key:
                    directory[str(key).strip().lower()] = row['email']
        return directory

    # -- stream-tier risk alerts ------------------------------------------

    def send_risk_alerts(self, alerts: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Immediate notification for newly-detected risk events.

        Batched per run rather than one email per ticket - ten separate mails
        in a morning is how a tool gets filtered to a folder.
        """
        if not alerts:
            return {'sent': 0, 'alerts': 0}

        decorated = [self.builder.decorate(a) for a in alerts]
        for original, row in zip(alerts, decorated):
            row['alert_type'] = original.get('alert_type')

        groups = sorted({r.get('assignment_group') for r in decorated if r.get('assignment_group')})
        recipients = [u['email'] for u in self.leads_for(groups)]
        if not recipients:
            return {'sent': 0, 'alerts': len(alerts), 'reason': 'no_recipients'}

        html = self.env.get_template('risk_alert.html').render(
            alerts=decorated,
            generated_at=utc_now(),
            base_url=self.builder.base_url,
            shadow_mode=self.settings.shadow_mode,
        )

        breached = sum(1 for a in decorated if a.get('alert_type') == 'SLA_BREACHED')
        subject = f"{len(decorated)} new risk alert{'s' if len(decorated) != 1 else ''}"
        if breached:
            subject += f" ({breached} SLA breach{'es' if breached != 1 else ''})"

        sent = self.mailer.send(to=recipients, subject=subject, html_body=html,
                                run_type='risk_alert', audience='lead',
                                ticket_count=len(decorated))
        return {'sent': int(sent), 'alerts': len(decorated)}

    # -- preview (used by the UI) -----------------------------------------

    def preview_lead_digest(self, assignment_groups: Optional[List[str]] = None) -> str:
        groups = assignment_groups or self.settings.assignment_groups
        payload = self.builder.build_lead_digest(groups)
        payload['aged_after_days'] = self.settings.aged_after_days
        return self.env.get_template('lead_digest.html').render(**payload)
