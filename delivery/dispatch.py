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
from typing import Any, Dict, List, Optional, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.logging_setup import get_logger
from delivery.digest import DigestBuilder, owner_labels, team_label
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
        # What to call the team that owes the next action. One name across
        # however many queues the deployment covers.
        'team_label': team_label(settings),
        'PENDING_ACTION_OWNER_LABELS': owner_labels(settings),
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

    @staticmethod
    def _scope_of(user: Dict[str, Any]) -> List[str]:
        return [g.strip() for g in (user.get('assignment_groups') or '').split(',')
                if g.strip()]

    @classmethod
    def _covers(cls, user: Dict[str, Any], group: str) -> bool:
        """Whether this account is responsible for one assignment group.

        An empty scope means every group for an administrator and no group
        for anyone else - the same rule the board enforces in
        UserStore._unrestricted, and it has to be the same rule. A lead who
        can see one queue in the UI but receives three by email is still a
        disclosure; it just happens where nobody is looking.
        """
        scope = {g.lower() for g in cls._scope_of(user)}
        if not scope:
            return user.get('role') == 'ADMIN'
        return str(group or '').strip().lower() in scope

    def leads_for(self, assignment_groups: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        users = self.db.retrieve('advisor_user', conditions=[
            {'col': 'active', 'op': 'eq', 'val': 1},
            {'col': 'role', 'op': 'i', 'val': ['ADMIN', 'LEAD']},
        ])

        if not assignment_groups:
            return [u for u in users if u.get('email')]

        return [u for u in users if u.get('email')
                and any(self._covers(u, g) for g in assignment_groups)]

    def recipients_for(self, group: str,
                       roles: Sequence[str] = ('LEAD',)) -> List[str]:
        """Deliverable addresses for one assignment group, in the given roles."""
        users = self.db.retrieve('advisor_user', conditions=[
            {'col': 'active', 'op': 'eq', 'val': 1},
            {'col': 'role', 'op': 'i', 'val': list(roles)},
        ], order_by='username')
        return [u['email'] for u in users
                if u.get('email') and self._covers(u, group)]

    def audience_for_group(self, group: str,
                           roles: Sequence[str] = ('LEAD',)) -> Dict[str, List[str]]:
        """Who to write to about one queue, and who to copy.

        To: the people who own the queue. Cc: every administrator, so there is
        a standing record of what each team was told without an administrator
        having to be scoped into every queue by hand.

        A queue with no lead falls back to addressing the administrators
        directly rather than dropping the mail. An unstaffed queue is a
        provisioning gap, and a digest that silently goes nowhere is how it
        stays one - the count in the job result would read zero with no
        indication that there was anything to send.
        """
        to = self.recipients_for(group, roles)
        cc = [a['email'] for a in self.admins()]

        if not to:
            log.warning('No %s with %s in scope - addressing the digest for that '
                        'queue to the administrators instead',
                        '/'.join(roles).lower(), group)
            return {'to': cc, 'cc': [], 'orphaned': True}

        addressed = {t.strip().lower() for t in to}
        return {'to': to,
                'cc': [c for c in cc if c.strip().lower() not in addressed],
                'orphaned': False}

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
        """One digest per assignment group.

        Deliberately one email per queue rather than one per reader. A lead
        covering three queues gets three messages, and that is the point: each
        one names its queue in the subject, is addressed to the people who own
        that queue, and can be forwarded or filed without first being split
        apart. A single combined mail cannot be any of those things.

        Administrators are copied on every one, so the whole estate is still
        visible to the people who administer it without any of them having to
        be scoped into each queue by hand.
        """
        groups = assignment_groups or self.settings.assignment_groups
        groups = [g for g in (groups or []) if str(g).strip()]
        if not groups:
            log.warning('No assignment groups configured - no digest to build')
            return {'sent': 0, 'tickets': 0, 'reason': 'no_groups'}

        max_tickets = int(self.settings.get('thresholds.digest_max_tickets_per_agent', 25))

        sent = tickets = 0
        empty = 0
        results: List[Dict[str, Any]] = []

        for group in groups:
            audience = self.audience_for_group(group, roles=('LEAD',))
            if not audience['to']:
                log.error('Nobody to send the %s digest to - no lead with that '
                          'queue in scope, and no administrator has an email '
                          'address', group)
                continue

            payload = self.builder.build_lead_digest([group], max_tickets=max_tickets)
            payload['aged_after_days'] = self.settings.aged_after_days

            if not payload['tickets']:
                log.info('Nothing aged in %s - no digest sent', group)
                empty += 1
                continue

            summary = payload['summary']
            subject = (f"{group} - aged ticket review: {summary['total']} open, "
                       f"{summary['team_owned']} awaiting your team")
            if summary['sla_breached']:
                subject += f", {summary['sla_breached']} SLA breached"

            html = self.env.get_template('lead_digest.html').render(**payload)
            delivered = self.mailer.send(
                to=audience['to'], cc=audience['cc'],
                subject=subject, html_body=html,
                run_type=run_type, audience='lead',
                ticket_count=len(payload['tickets']),
                new_count=payload['movement']['new_count'],
            )

            sent += int(bool(delivered))
            tickets += len(payload['tickets'])
            results.append({'group': group, 'to': audience['to'],
                            'cc': audience['cc'],
                            'orphaned': audience['orphaned'],
                            'tickets': len(payload['tickets'])})

        if not sent:
            return {'sent': 0, 'tickets': 0, 'groups': len(groups),
                    'reason': 'empty' if empty else 'no_recipients'}

        return {'sent': sent, 'tickets': tickets,
                'groups': len(groups), 'digests': results}

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

            payload = self.builder.build_agent_digest(agent, assignment_groups=groups)
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
        """Immediate notification for newly-detected risk events, per queue.

        Batched per run rather than one email per ticket - ten separate mails
        in a morning is how a tool gets filtered to a folder - but split by
        assignment group, because the alert body carries short descriptions
        and caller names and those belong to one team at a time.

        Wider than the digest on purpose: leads and viewers both, since an
        alert is something to act on now and the viewers are the people
        working the queue. Administrators are copied, as on the digest.

        An alert on a ticket with no assignment group is reported rather than
        sent. It belongs to no queue, so there is no correct audience for it,
        and guessing one is how a ticket nobody owns ends up in front of a
        team that does not own it either.
        """
        if not alerts:
            return {'sent': 0, 'alerts': 0}

        decorated = [self.builder.decorate(a) for a in alerts]
        for original, row in zip(alerts, decorated):
            row['alert_type'] = original.get('alert_type')

        by_group: Dict[str, List[Dict[str, Any]]] = {}
        ungrouped = 0
        for row in decorated:
            group = str(row.get('assignment_group') or '').strip()
            if not group:
                ungrouped += 1
                continue
            by_group.setdefault(group, []).append(row)

        if ungrouped:
            log.warning('%d risk alert(s) have no assignment group and were not '
                        'sent to anyone - they are visible on the board only',
                        ungrouped)

        if not by_group:
            return {'sent': 0, 'alerts': len(decorated), 'ungrouped': ungrouped,
                    'reason': 'no_groups'}

        sent = 0
        delivered_alerts = 0
        results: List[Dict[str, Any]] = []

        for group in sorted(by_group):
            mine = by_group[group]
            audience = self.audience_for_group(group, roles=('LEAD', 'VIEWER'))
            if not audience['to']:
                log.error('Nobody to send %d %s risk alert(s) to', len(mine), group)
                continue

            html = self.env.get_template('risk_alert.html').render(
                alerts=mine,
                group=group,
                generated_at=utc_now(),
                base_url=self.builder.base_url,
                shadow_mode=self.settings.shadow_mode,
            )

            breached = sum(1 for a in mine if a.get('alert_type') == 'SLA_BREACHED')
            subject = (f"{group} - {len(mine)} new risk "
                       f"alert{'s' if len(mine) != 1 else ''}")
            if breached:
                subject += f" ({breached} SLA breach{'es' if breached != 1 else ''})"

            if self.mailer.send(to=audience['to'], cc=audience['cc'],
                                subject=subject, html_body=html,
                                run_type='risk_alert',
                                audience='lead', ticket_count=len(mine)):
                sent += 1
                delivered_alerts += len(mine)
                results.append({'group': group, 'to': audience['to'],
                                'cc': audience['cc'], 'alerts': len(mine)})

        return {'sent': sent, 'alerts': len(decorated),
                'delivered': delivered_alerts, 'groups': len(by_group),
                'ungrouped': ungrouped, 'notifications': results}

    # -- preview (used by the UI) -----------------------------------------

    def preview_lead_digest(self, assignment_groups: Optional[List[str]] = None) -> str:
        """Render one digest without sending it.

        Takes a list for the sake of the callers that already hold one, but a
        real digest covers a single queue, so a preview over several would
        show a message that is never actually sent. Narrowed to the first,
        and the header names which one.
        """
        groups = assignment_groups or self.settings.assignment_groups
        groups = [g for g in (groups or []) if str(g).strip()][:1]
        payload = self.builder.build_lead_digest(groups)
        payload['aged_after_days'] = self.settings.aged_after_days
        return self.env.get_template('lead_digest.html').render(**payload)
