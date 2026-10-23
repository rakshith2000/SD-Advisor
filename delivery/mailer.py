"""SMTP delivery.

Credentials come from Vault; an anonymous relay is supported by simply not
storing a username. Every send is logged to digest_run so "did the lead
actually get it" is answerable without reading mail server logs.

shadow_mode short-circuits sending: everything is computed, rendered and
recorded, but nothing leaves the box. That is how the first two weeks run.
"""

import datetime
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from core.timeutil import utc_now

log = get_logger('delivery.mailer')


class Mailer:
    def __init__(self, settings, vault, db=None):
        self.settings = settings
        self.db = db
        self.enabled = bool(settings.get('mail.enabled', True))
        # The display name on every outbound message. Configurable because the
        # service is not tied to one queue: a deployment covering several
        # groups should not have its mail appear to come from one of them.
        self.from_name = (str(settings.get('mail.from_name', '')).strip()
                          or 'Aged Ticket Advisor')
        self.shadow = bool(settings.get('runtime.shadow_mode', True))

        # Shadow mode exists to stop bulk mail reaching the wider team
        # while output is still being validated. An access request notification
        # is a different thing: one message to the administrators, about the
        # service itself, triggered by a person who is waiting for an answer.
        # Suppressing it leaves the request sitting unseen - and the trial is
        # exactly when that workflow is being exercised.
        self.shadow_exempt = {
            str(t).strip() for t in (settings.get('runtime.shadow_mode_exempt', []) or [])
            if str(t).strip()
        }

        self.host = settings.get('mail.host')
        self.port = int(settings.get('mail.port', 587))
        self.use_starttls = bool(settings.get('mail.use_starttls', True))
        self.from_address = settings.get('mail.from_address')
        self.reply_to = settings.get('mail.reply_to')
        self.subject_prefix = settings.get('mail.subject_prefix', '[SD Advisor]')
        self.always_bcc = list(settings.get('mail.always_bcc', []) or [])

        self.username: Optional[str] = None
        self.password: Optional[str] = None

        try:
            secret = vault.secret(settings.get('vault.paths.smtp', 'sd_advisor_smtp'))
            self.username = secret.get('username')
            self.password = secret.get('password')
        except Exception:
            log.info('No SMTP credentials in Vault - assuming an open relay')

    # -- public ------------------------------------------------------------

    def send(self, to: List[str], subject: str, html_body: str,
             text_body: str = '', run_type: str = 'digest',
             audience: str = 'lead', ticket_count: int = 0,
             new_count: int = 0, cc: Optional[List[str]] = None) -> bool:
        recipients = [r for r in (to or []) if r and '@' in r]
        if not recipients:
            log.warning('Skipping send - no valid recipients for %r', subject)
            return False

        # Anyone already on To is dropped from Cc. Addressing the same person
        # twice is how a mail client decides the message is a duplicate, and
        # some relays expand it into two deliveries.
        addressed = {r.strip().lower() for r in recipients}
        copies = []
        for address in (cc or []):
            if not address or '@' not in address:
                continue
            key = address.strip().lower()
            if key in addressed:
                continue
            addressed.add(key)
            copies.append(address)

        full_subject = f'{self.subject_prefix} {subject}'.strip()

        suppressed_by_shadow = self.shadow and run_type not in self.shadow_exempt

        # mail.enabled=false is absolute - it means this host must not talk to
        # the relay at all - so no run_type escapes it.
        if suppressed_by_shadow or not self.enabled:
            reason = 'shadow mode' if suppressed_by_shadow else 'mail disabled'
            log.info('[%s] would send %r to %s%s (%d tickets)',
                     reason, full_subject, ', '.join(recipients),
                     f" cc {', '.join(copies)}" if copies else '', ticket_count)
            self._record(run_type, audience, recipients, ticket_count, new_count,
                         'SUPPRESSED', reason, copies)
            return False

        message = EmailMessage()
        message['Subject'] = full_subject
        message['From'] = formataddr((self.from_name, self.from_address))
        message['To'] = ', '.join(recipients)
        if copies:
            message['Cc'] = ', '.join(copies)
        message['Message-ID'] = make_msgid(domain='sd-advisor')
        if self.reply_to:
            message['Reply-To'] = self.reply_to

        message.set_content(text_body or _html_to_text(html_body))
        message.add_alternative(html_body, subtype='html')

        # The Cc header tells the reader who else got it; the envelope is what
        # actually delivers. Both are needed - send_message would otherwise
        # derive the envelope from the headers, and always_bcc has to stay out
        # of the headers by definition.
        envelope = recipients + copies + self.always_bcc

        try:
            self._transmit(message, envelope)
        except Exception as exc:
            log.exception('Failed to send %r', full_subject)
            self._record(run_type, audience, recipients, ticket_count, new_count,
                         'FAILED', str(exc)[:500], copies)
            return False

        log.info('Sent %r to %s%s (%d tickets)', full_subject,
                 ', '.join(recipients),
                 f" cc {', '.join(copies)}" if copies else '', ticket_count)
        self._record(run_type, audience, recipients, ticket_count, new_count,
                     'SENT', '', copies)
        return True

    # -- internals ---------------------------------------------------------

    def _transmit(self, message: EmailMessage, envelope: List[str]) -> None:
        if self.use_starttls:
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                if self.username and self.password:
                    server.login(self.username, self.password)
                server.send_message(message, to_addrs=envelope)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                if self.username and self.password:
                    server.login(self.username, self.password)
                server.send_message(message, to_addrs=envelope)

    def _record(self, run_type: str, audience: str, recipients: List[str],
                ticket_count: int, new_count: int, status: str, error: str,
                copies: Optional[List[str]] = None) -> None:
        if self.db is None:
            return
        # To first, then Cc. The column is short and gets truncated, and "who
        # was this addressed to" is the question the audit trail is for.
        addressed = ', '.join(recipients)
        if copies:
            addressed += ' (cc: ' + ', '.join(copies) + ')'
        try:
            self.db.insert('digest_run', {
                'run_type': run_type,
                'audience': audience,
                'recipient': addressed[:200],
                'ticket_count': ticket_count,
                'new_count': new_count,
                'sent_at': utc_now(),
                'status': status,
                'error': error or None,
            })
        except Exception:
            log.debug('Could not write digest_run row')


def _html_to_text(html: str) -> str:
    """Crude plaintext alternative so the message is not HTML-only."""
    import re
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.S | re.I)
    text = re.sub(r'<br\s*/?>|</(p|tr|div|h[1-6])>', '\n', text, flags=re.I)
    text = re.sub(r'</td>', '\t', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = (text.replace('&nbsp;', ' ').replace('&amp;', '&')
                .replace('&lt;', '<').replace('&gt;', '>').replace('&#39;', "'"))
    return re.sub(r'\n{3,}', '\n\n', text).strip()
