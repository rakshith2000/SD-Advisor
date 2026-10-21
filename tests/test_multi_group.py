"""Covering more than one assignment group.

The service was built against a single queue, where "every configured group"
and "every group this reader may see" are the same set and the difference
cannot show up. They are not the same set once a second queue is added, and
the places that conflated them were not wrong so much as untested:

  * the lead digest built one email over every configured queue and sent it to
    everyone scoped to any of them
  * risk alerts did the same with the alert bodies

Both carry short descriptions and caller names, so the failure is a
disclosure, not a cosmetic one, and nothing about it is visible to the sender.
These tests pin the scoping. The label tests cover the other half of the ask -
that nothing in the UI or the mail tells a Desktop Support lead their tickets
are awaiting the Service Desk.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from delivery.digest import (DEFAULT_TEAM_LABEL, PENDING_ACTION_OWNER_LABELS,
                             owner_labels, team_label)
from delivery.dispatch import Dispatcher

SD = 'Service Desk'
DS = 'Desktop Support'
NW = 'Network Operations'
ALL = [SD, DS, NW]


class FakeSettings:
    def __init__(self, **values):
        self._values = values
        self.assignment_groups = list(values.get('assignment_groups', ALL))
        self.shadow_mode = False
        self.aged_after_days = 5
        self.customer_name = 'Example'

    def get(self, key, default=None):
        return self._values.get(key, default)


class FakeDb:
    """Honours the conditions it is given.

    leads_for selects on role IN ('ADMIN','LEAD') and active = 1, and a fake
    that ignored that would let a Viewer through here and pass a test the real
    query would fail.
    """

    def __init__(self, users):
        self.users = users

    def retrieve(self, table, columns=None, conditions=None, **kw):
        rows = [dict(u) for u in self.users]
        for cond in (conditions or []):
            col, op, val = cond['col'], cond['op'], cond['val']
            if op == 'eq':
                rows = [r for r in rows if r.get(col) == val]
            elif op == 'i':
                rows = [r for r in rows if r.get(col) in val]
            else:
                raise AssertionError(f'FakeDb does not implement op {op!r}')
        return rows


def user(name, groups, role='LEAD', email=None):
    return {'username': name, 'full_name': name, 'role': role,
            'email': email or f'{name}@example.com', 'active': 1,
            'assignment_groups': ','.join(groups) if groups else None}


def dispatcher(users, **settings):
    """A Dispatcher with only the collaborators these methods touch."""
    d = Dispatcher.__new__(Dispatcher)
    d.settings = FakeSettings(**settings)
    d.db = FakeDb(users)
    return d


def scopes(audiences):
    """Normalised for comparison: {frozenset(groups): sorted(recipients)}."""
    return {frozenset(a['groups']): sorted(a['recipients']) for a in audiences}


# ---------------------------------------------------------------------------
# who gets what
# ---------------------------------------------------------------------------

class TestAudiences:
    def test_a_single_queue_behaves_exactly_as_before(self):
        """The shape this was built for. One bucket, everybody in it."""
        d = dispatcher([user('asha', [SD]), user('raj', [SD])])
        assert scopes(d.audiences_for([SD])) == {
            frozenset([SD]): ['asha@example.com', 'raj@example.com']}

    def test_each_lead_is_bucketed_by_the_queues_they_cover(self):
        d = dispatcher([user('asha', [SD]), user('raj', [DS]), user('mei', [NW])])
        assert scopes(d.audiences_for(ALL)) == {
            frozenset([SD]): ['asha@example.com'],
            frozenset([DS]): ['raj@example.com'],
            frozenset([NW]): ['mei@example.com'],
        }

    def test_leads_covering_the_same_queues_share_one_email(self):
        """One bucket per distinct scope, not one per queue - otherwise a lead
        over three queues gets three mails every morning."""
        d = dispatcher([user('asha', [SD, DS]), user('raj', [DS, SD])])
        audiences = d.audiences_for(ALL)
        assert len(audiences) == 1
        assert sorted(audiences[0]['recipients']) == ['asha@example.com', 'raj@example.com']
        assert set(audiences[0]['groups']) == {SD, DS}

    def test_a_scope_is_intersected_with_the_run_never_unioned(self):
        """A lead entitled to Network Operations still does not see it in a
        run that is only covering the other two."""
        d = dispatcher([user('asha', [SD, NW])])
        audiences = d.audiences_for([SD, DS])
        assert set(audiences[0]['groups']) == {SD}

    def test_a_lead_with_no_overlap_is_not_sent_an_empty_digest(self):
        d = dispatcher([user('mei', [NW])])
        assert d.audiences_for([SD, DS]) == []

    def test_an_unrestricted_account_covers_the_whole_run(self):
        """An empty scope has always meant 'everything' for a lead, and the
        board relies on the same rule."""
        d = dispatcher([user('admin', [], role='ADMIN')])
        assert set(d.audiences_for(ALL)[0]['groups']) == set(ALL)

    def test_a_lead_without_an_address_is_skipped(self):
        d = dispatcher([dict(user('ghost', [SD]), email=None)])
        assert d.audiences_for([SD]) == []

    def test_a_viewer_is_not_a_digest_recipient(self):
        d = dispatcher([user('watcher', [SD], role='VIEWER')])
        assert d.audiences_for([SD]) == []

    def test_no_queues_means_no_audiences(self):
        d = dispatcher([user('asha', [SD])])
        assert d.audiences_for([]) == []
        assert d.audiences_for(None) == []

    def test_group_matching_ignores_case_and_padding(self):
        d = dispatcher([user('asha', ['  service desk '])])
        audiences = d.audiences_for([SD])
        # The configured spelling wins, so the digest query matches the data.
        assert audiences[0]['groups'] == [SD]

    def test_the_configured_spelling_is_what_reaches_the_query(self):
        """board_rows compares against assignment_group verbatim, so handing
        it the user's casing would quietly return nothing."""
        d = dispatcher([user('asha', ['SERVICE DESK', 'desktop support'])])
        assert set(d.audiences_for(ALL)[0]['groups']) == {SD, DS}


class TestLeadsFor:
    def test_unscoped_request_returns_every_lead(self):
        d = dispatcher([user('asha', [SD]), user('mei', [NW])])
        assert len(d.leads_for(None)) == 2

    def test_scoped_request_filters(self):
        d = dispatcher([user('asha', [SD]), user('mei', [NW])])
        assert [u['username'] for u in d.leads_for([SD])] == ['asha']


# ---------------------------------------------------------------------------
# what the team is called
# ---------------------------------------------------------------------------

class TestTeamLabel:
    def test_the_default_names_no_queue(self):
        """A deployment covering three queues must not call itself one of
        them, and the shipped default is what most will run with."""
        assert team_label(FakeSettings()) == DEFAULT_TEAM_LABEL
        assert 'Service Desk' not in DEFAULT_TEAM_LABEL

    def test_it_can_be_configured(self):
        assert team_label(FakeSettings(**{'display.team_label': 'IT Support'})) == 'IT Support'

    @pytest.mark.parametrize('value', ['', '   ', None])
    def test_a_blank_setting_falls_back(self, value):
        assert team_label(FakeSettings(**{'display.team_label': value})) == DEFAULT_TEAM_LABEL

    def test_no_settings_at_all_still_yields_a_label(self):
        """build_environment is called with settings=None in a few places."""
        assert team_label(None) == DEFAULT_TEAM_LABEL

    def test_the_owner_labels_follow_it(self):
        labels = owner_labels(FakeSettings(**{'display.team_label': 'IT Support'}))
        assert labels['SERVICE_DESK'] == 'IT Support'

    def test_the_other_owners_are_untouched(self):
        labels = owner_labels(FakeSettings(**{'display.team_label': 'IT Support'}))
        for key in ('CALLER', 'VENDOR', 'CHANGE', 'PROBLEM', 'APPROVAL'):
            assert labels[key] == PENDING_ACTION_OWNER_LABELS[key]

    def test_the_module_constant_is_not_mutated(self):
        """It is imported directly in a couple of places; a label configured
        for one deployment must not leak into it."""
        owner_labels(FakeSettings(**{'display.team_label': 'Something Else'}))
        assert PENDING_ACTION_OWNER_LABELS['SERVICE_DESK'] == DEFAULT_TEAM_LABEL

    def test_the_stored_token_is_unchanged(self):
        """SERVICE_DESK is written into ticket_signal and into the model's
        schema. Renaming the token would rewrite history to fix a caption."""
        assert 'SERVICE_DESK' in PENDING_ACTION_OWNER_LABELS


# ---------------------------------------------------------------------------
# the disclosure this was all for
# ---------------------------------------------------------------------------

class Sent:
    """Captures what would have gone out."""

    def __init__(self):
        self.messages = []

    def send(self, to, subject, html_body, run_type=None, audience=None,
             ticket_count=0, new_count=0, **kw):
        self.messages.append({'to': list(to), 'subject': subject,
                              'body': html_body, 'run_type': run_type,
                              'tickets': ticket_count})
        return True


class FakeBuilder:
    """Returns only the rows belonging to the groups it is asked for."""

    def __init__(self, rows):
        self.rows = rows
        self.base_url = 'https://ata.example'

    def _in(self, groups):
        wanted = {g.lower() for g in (groups or [])}
        return [r for r in self.rows
                if not wanted or r['assignment_group'].lower() in wanted]

    def build_lead_digest(self, groups=None, max_tickets=25):
        rows = self._in(groups)
        return {'tickets': rows, 'summary': {'total': len(rows), 'team_owned': len(rows),
                                             'sla_breached': 0},
                'movement': {'new_count': 0}, 'groups': groups}

    def decorate(self, row):
        return dict(row)


class FakeEnv:
    def get_template(self, name):
        class T:
            @staticmethod
            def render(**ctx):
                tickets = ctx.get('tickets') or ctx.get('alerts') or []
                return ' '.join(t['incident_number'] for t in tickets)
        return T()


def wired(users, rows, **settings):
    d = dispatcher(users, **settings)
    d.builder = FakeBuilder(rows)
    d.env = FakeEnv()
    d.mailer = Sent()
    return d


def ticket(number, group, alert='SLA_BREACHED'):
    return {'incident_number': number, 'assignment_group': group,
            'short_description': 'confidential detail', 'alert_type': alert}


ROWS = [ticket('INC_SD', SD), ticket('INC_DS', DS), ticket('INC_NW', NW)]


class TestNoCrossQueueDisclosure:
    def test_the_lead_digest_carries_only_the_recipients_own_queues(self):
        """The bug: one digest over every configured queue, mailed to everyone
        scoped to any of them. The body carries descriptions and caller names."""
        d = wired([user('asha', [SD]), user('raj', [DS])], ROWS)
        d.send_lead_digest(ALL)

        by_recipient = {m['to'][0]: m['body'] for m in d.mailer.messages}
        assert by_recipient['asha@example.com'] == 'INC_SD'
        assert by_recipient['raj@example.com'] == 'INC_DS'
        for body in by_recipient.values():
            assert 'INC_NW' not in body

    def test_one_email_per_distinct_scope_not_per_queue(self):
        d = wired([user('asha', [SD, DS])], ROWS)
        d.send_lead_digest(ALL)
        assert len(d.mailer.messages) == 1
        assert d.mailer.messages[0]['body'] == 'INC_SD INC_DS'

    def test_an_unrestricted_lead_still_sees_everything(self):
        d = wired([user('admin', [], role='ADMIN')], ROWS)
        d.send_lead_digest(ALL)
        assert d.mailer.messages[0]['body'] == 'INC_SD INC_DS INC_NW'

    def test_a_queue_with_nothing_aged_sends_no_email(self):
        d = wired([user('mei', [NW])], [ticket('INC_SD', SD)])
        result = d.send_lead_digest(ALL)
        assert d.mailer.messages == []
        assert result['sent'] == 0 and result['reason'] == 'empty'

    def test_the_subject_names_no_queue(self):
        d = wired([user('asha', [SD])], ROWS)
        d.send_lead_digest(ALL)
        assert 'Service Desk' not in d.mailer.messages[0]['subject']

    def test_risk_alerts_are_filtered_the_same_way(self):
        """Same disclosure, different surface - the alert body carries the
        short description of every incident in the run."""
        d = wired([user('asha', [SD]), user('raj', [DS])], ROWS)
        d.send_risk_alerts(ROWS)

        by_recipient = {m['to'][0]: m['body'] for m in d.mailer.messages}
        assert by_recipient['asha@example.com'] == 'INC_SD'
        assert by_recipient['raj@example.com'] == 'INC_DS'

    def test_a_lead_with_no_alerts_in_their_queues_is_not_emailed(self):
        d = wired([user('mei', [NW])], ROWS)
        d.send_risk_alerts([ticket('INC_SD', SD)])
        assert d.mailer.messages == []

    def test_no_recipients_is_reported_rather_than_silently_nothing(self):
        d = wired([], ROWS)
        assert d.send_lead_digest(ALL)['reason'] == 'no_recipients'
        assert d.send_risk_alerts(ROWS)['reason'] == 'no_recipients'

    def test_an_empty_argument_falls_back_to_the_configured_queues(self):
        """How the scheduler asks for 'everything': a digest entry with no
        assignment_groups covers whatever is configured."""
        d = wired([user('admin', [], role='ADMIN')], ROWS)
        d.send_lead_digest([])
        assert d.mailer.messages[0]['body'] == 'INC_SD INC_DS INC_NW'

    def test_no_configured_queues_is_distinguished_from_no_recipients(self):
        """Different remedies, and a zero count alone cannot tell them apart."""
        d = wired([user('asha', [SD])], ROWS, assignment_groups=[])
        assert d.send_lead_digest([])['reason'] == 'no_groups'
