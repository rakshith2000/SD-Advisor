"""Addressing: To, Cc, Bcc and the envelope.

Mail has two independent notions of who gets a message. The headers say who
it was addressed to, and the envelope is what the relay actually delivers to.
They are allowed to differ - that is what Bcc is - and every bug in this area
is one of them being right while the other is not.

Three things here:

  * Cc appears in the headers and in the envelope. A Cc header with nobody in
    the envelope prints an address the message never reached.
  * always_bcc appears in the envelope and never in the headers. That is the
    definition of a blind copy, and leaking it is a disclosure.
  * Nobody is addressed twice. Some relays expand a duplicate into two
    deliveries, and mail clients treat it as evidence of a loop.

    python -m pytest tests/test_mail_envelope.py -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from delivery.mailer import Mailer


class FakeSettings:
    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


class NoVault:
    def secret(self, path):
        raise RuntimeError('no credentials - anonymous relay')


def mailer(**settings):
    """A Mailer that records instead of transmitting."""
    defaults = {'runtime.shadow_mode': False, 'mail.enabled': True,
                'mail.host': 'relay.example', 'mail.port': 25,
                'mail.use_starttls': False,
                'mail.from_address': 'ata@example.com',
                'mail.subject_prefix': '[ATA]'}
    defaults.update(settings)

    m = Mailer(FakeSettings(**defaults), NoVault(), db=None)
    m.transmitted = []
    m._transmit = lambda message, envelope: m.transmitted.append(
        {'message': message, 'envelope': list(envelope)})
    return m


def send(m, **kw):
    kw.setdefault('to', ['lead@example.com'])
    kw.setdefault('subject', 'Aged ticket review')
    kw.setdefault('html_body', '<p>body</p>')
    return m.send(**kw)


def headers(m):
    return m.transmitted[0]['message']


def envelope(m):
    return m.transmitted[0]['envelope']


# ---------------------------------------------------------------------------

class TestCc:
    def test_it_reaches_the_headers(self):
        m = mailer()
        send(m, cc=['admin@example.com'])
        assert headers(m)['Cc'] == 'admin@example.com'

    def test_it_reaches_the_envelope(self):
        """A Cc header with nobody in the envelope prints an address the
        message never actually went to."""
        m = mailer()
        send(m, cc=['admin@example.com'])
        assert 'admin@example.com' in envelope(m)

    def test_several_are_comma_separated(self):
        m = mailer()
        send(m, cc=['a@example.com', 'b@example.com'])
        assert headers(m)['Cc'] == 'a@example.com, b@example.com'

    def test_no_cc_header_when_there_is_none(self):
        """An empty Cc: header is not the same as no header, and some
        scanners treat it as malformed."""
        m = mailer()
        send(m)
        assert headers(m).get('Cc') is None

    def test_an_address_on_to_is_not_also_copied(self):
        """Two copies is how a mail client decides the message is a
        duplicate, and some relays deliver it twice."""
        m = mailer()
        send(m, to=['lead@example.com'], cc=['lead@example.com', 'admin@example.com'])
        assert headers(m)['Cc'] == 'admin@example.com'
        assert envelope(m).count('lead@example.com') == 1

    def test_the_comparison_ignores_case(self):
        m = mailer()
        send(m, to=['Lead@Example.com'], cc=['lead@example.com'])
        assert headers(m).get('Cc') is None

    def test_a_duplicate_within_cc_is_dropped(self):
        m = mailer()
        send(m, cc=['admin@example.com', 'admin@example.com'])
        assert headers(m)['Cc'] == 'admin@example.com'

    @pytest.mark.parametrize('bad', ['', '   ', None, 'not-an-address'])
    def test_rubbish_is_discarded_rather_than_sent(self, bad):
        m = mailer()
        send(m, cc=[bad, 'admin@example.com'])
        assert headers(m)['Cc'] == 'admin@example.com'

    def test_cc_alone_is_not_a_deliverable_message(self):
        """To is what makes a message addressed. A Cc-only send would look
        like it worked and arrive addressed to nobody."""
        m = mailer()
        assert send(m, to=[], cc=['admin@example.com']) is False
        assert m.transmitted == []


class TestBccStaysBlind:
    def test_it_is_in_the_envelope(self):
        m = mailer(**{'mail.always_bcc': ['archive@example.com']})
        send(m)
        assert 'archive@example.com' in envelope(m)

    def test_it_is_not_in_any_header(self):
        """The whole point of the field. A recipient who can read it has been
        told who else is watching."""
        m = mailer(**{'mail.always_bcc': ['archive@example.com']})
        send(m, cc=['admin@example.com'])
        rendered = headers(m).as_string()
        assert 'archive@example.com' not in rendered.split('\n\n')[0]

    def test_it_coexists_with_cc(self):
        m = mailer(**{'mail.always_bcc': ['archive@example.com']})
        send(m, cc=['admin@example.com'])
        assert sorted(envelope(m)) == ['admin@example.com', 'archive@example.com',
                                       'lead@example.com']


class TestSuppression:
    def test_shadow_mode_sends_nothing(self):
        m = mailer(**{'runtime.shadow_mode': True})
        assert send(m, cc=['admin@example.com']) is False
        assert m.transmitted == []

    def test_mail_disabled_sends_nothing(self):
        m = mailer(**{'mail.enabled': False})
        assert send(m, cc=['admin@example.com']) is False
        assert m.transmitted == []

    def test_an_exempt_run_type_is_still_sent_in_shadow_mode(self):
        m = mailer(**{'runtime.shadow_mode': True,
                      'runtime.shadow_mode_exempt': ['role_request']})
        assert send(m, run_type='role_request', cc=['admin@example.com']) is True
        assert headers(m)['Cc'] == 'admin@example.com'


class TestRecordedForAudit:
    """digest_run answers "did the lead actually get it" without reading mail
    server logs, so it has to record who was addressed - not just how many."""

    class Recorder:
        def __init__(self):
            self.rows = []

        def insert(self, table, row):
            self.rows.append(row)

    def recorded(self, **kw):
        m = mailer()
        m.db = self.Recorder()
        send(m, **kw)
        return m.db.rows[0]

    def test_the_addressees_are_recorded(self):
        assert 'lead@example.com' in self.recorded()['recipient']

    def test_the_copies_are_recorded_and_marked_as_such(self):
        row = self.recorded(cc=['admin@example.com'])
        assert 'cc: admin@example.com' in row['recipient']

    def test_to_comes_first(self):
        """The column is short and gets truncated. 'Who was this addressed
        to' is the question the record exists to answer."""
        row = self.recorded(cc=['admin@example.com'])
        assert row['recipient'].index('lead@') < row['recipient'].index('cc:')

    def test_the_column_width_is_respected(self):
        row = self.recorded(cc=[f'admin{i}@example.com' for i in range(40)])
        assert len(row['recipient']) <= 200
