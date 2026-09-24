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

log = get_logger('delivery.mailer')


class Mailer:
    def __init__(self, settings, vault, db=None):
        self.settings = settings
        self.db = db
        self.enabled = bool(settings.get('mail.enabled', True))
        self.shadow = bool(settings.get('runtime.shadow_mode', True))

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
             new_count: int = 0) -> bool:
        recipients = [r for r in (to or []) if r and '@' in r]
        if not recipients:
            log.warning('Skipping send - no valid recipients for %r', subject)
            return False

        full_subject = f'{self.subject_prefix} {subject}'.strip()

        if self.shadow or not self.enabled:
            reason = 'shadow mode' if self.shadow else 'mail disabled'
            log.info('[%s] would send %r to %s (%d tickets)',
                     reason, full_subject, ', '.join(recipients), ticket_count)
            self._record(run_type, audience, recipients, ticket_count, new_count,
                         'SUPPRESSED', reason)
            return False

        message = EmailMessage()
        message['Subject'] = full_subject
        message['From'] = formataddr(('Service Desk Advisor', self.from_address))
        message['To'] = ', '.join(recipients)
        message['Message-ID'] = make_msgid(domain='sd-advisor')
        if self.reply_to:
            message['Reply-To'] = self.reply_to

        message.set_content(text_body or _html_to_text(html_body))
        message.add_alternative(html_body, subtype='html')

        envelope = recipients + self.always_bcc

        try:
            self._transmit(message, envelope)
        except Exception as exc:
            log.exception('Failed to send %r', full_subject)
            self._record(run_type, audience, recipients, ticket_count, new_count,
                         'FAILED', str(exc)[:500])
            return False

        log.info('Sent %r to %s (%d tickets)', full_subject,
                 ', '.join(recipients), ticket_count)
        self._record(run_type, audience, recipients, ticket_count, new_count, 'SENT', '')
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
                ticket_count: int, new_count: int, status: str, error: str) -> None:
        if self.db is None:
            return
        try:
            self.db.insert('digest_run', {
                'run_type': run_type,
                'audience': audience,
                'recipient': ', '.join(recipients)[:200],
                'ticket_count': ticket_count,
                'new_count': new_count,
                'sent_at': datetime.datetime.now(),
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
