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

log = get_logger('delivery.dispatch')

TEMPLATE_DIR = Path(__file__).resolve().parent / 'templates'


def build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(['html', 'xml']),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters['datefmt'] = lambda value, fmt='%d %b %Y %H:%M': (
        value.strftime(fmt) if hasattr(value, 'strftime') else (value or ''))
    return env


class Dispatcher:
    def __init__(self, context):
        self.ctx = context
        self.settings = context.settings
        self.db = context.db
        self.builder = DigestBuilder(context)
        self.mailer = Mailer(context.settings, context.vault, context.db)
        self.env = build_environment()

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
            f"{summary['ours']} waiting on us"
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
            generated_at=datetime.datetime.now(),
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
