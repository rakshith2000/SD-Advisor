"""Covering more than one assignment group.

The service was built against a single queue, where "every configured group"
and "every group this reader may see" are the same set and the difference
cannot show up. They are not the same set once a second queue is added, and
the places that conflated them were not wrong so much as untested: the lead
digest and the risk alerts each built one email over every configured queue
and sent it to everyone scoped to any of them. Both bodies carry short
descriptions and caller names, so the failure is a disclosure, and nothing
about it is visible to the sender.

The delivery model is now one email per queue:

    to    the leads of that queue (plus viewers, for risk alerts)
    cc    every administrator
    subj  names the queue

A lead over three queues therefore gets three messages. That is the point -
each one can be forwarded, filed or escalated on its own, which a combined
mail cannot be. The administrators' Cc is what keeps the estate visible to
the people who administer it without scoping each of them into every queue.

The label tests cover the other half: nothing in the UI or the mail tells a
Desktop Support lead their tickets are awaiting the Service Desk.
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

    recipients_for selects on role and active = 1, and a fake that ignored
    that would let a viewer onto the digest and pass a test the real query
    would fail.
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


# ---------------------------------------------------------------------------
# who gets what
# ---------------------------------------------------------------------------

class TestCovers:
    """The one rule everything else is built on."""

    def test_a_scoped_account_covers_its_own_queues(self):
        assert Dispatcher._covers(user('asha', [SD, DS]), SD)
        assert not Dispatcher._covers(user('asha', [SD, DS]), NW)

    def test_matching_ignores_case_and_padding(self):
        assert Dispatcher._covers(user('asha', ['  service desk ']), SD)

    def test_an_empty_scope_covers_everything_for_an_administrator(self):
        assert Dispatcher._covers(user('root', [], role='ADMIN'), NW)

    def test_an_empty_scope_covers_nothing_for_anyone_else(self):
        """The disclosure this prevents: a lead whose groups nobody set
        receiving every other team's incidents by email."""
        assert not Dispatcher._covers(user('asha', [], role='LEAD'), SD)
        assert not Dispatcher._covers(user('watcher', [], role='VIEWER'), SD)

    def test_it_agrees_with_the_board(self):
        """Mail scope and UI scope have to be the same rule. A lead who sees
        one queue in the browser and receives three by email is still a
        disclosure - it just happens where nobody is looking."""
        from web.auth import UserStore

        store = UserStore(db=None)
        for role in ('ADMIN', 'LEAD', 'VIEWER'):
            for scope in ([], [SD], [SD, DS]):
                account = user('x', scope, role=role)
                browser = {'username': 'x', 'role': role, 'groups': scope}
                for group in ALL:
                    assert (Dispatcher._covers(account, group)
                            == store.can_see_group(browser, group)), (
                        f'{role} scope={scope} group={group}')


class TestAudienceForGroup:
    def test_leads_are_addressed_and_admins_copied(self):
        d = dispatcher([user('asha', [SD]), user('root', [], role='ADMIN')])
        audience = d.audience_for_group(SD)
        assert audience['to'] == ['asha@example.com']
        assert audience['cc'] == ['root@example.com']

    def test_only_this_queues_leads_are_addressed(self):
        d = dispatcher([user('asha', [SD]), user('raj', [DS])])
        assert d.audience_for_group(SD)['to'] == ['asha@example.com']

    def test_a_lead_over_several_queues_is_on_each_of_them(self):
        d = dispatcher([user('asha', [SD, DS])])
        assert d.audience_for_group(SD)['to'] == ['asha@example.com']
        assert d.audience_for_group(DS)['to'] == ['asha@example.com']
        assert d.audience_for_group(NW)['to'] == []

    def test_an_admin_scoped_to_the_queue_is_not_addressed_twice(self):
        """They would be in To as a lead and in Cc as an administrator. Two
        copies is how a mail client decides the message is a duplicate."""
        d = dispatcher([user('root', [SD], role='ADMIN')])
        audience = d.audience_for_group(SD, roles=('LEAD', 'ADMIN'))
        assert audience['to'] == ['root@example.com']
        assert audience['cc'] == []

    def test_viewers_are_not_on_the_digest(self):
        d = dispatcher([user('watcher', [SD], role='VIEWER')])
        assert d.audience_for_group(SD, roles=('LEAD',))['to'] == []

    def test_viewers_are_on_the_risk_alerts(self):
        """An alert is something to act on now, and the viewers are the people
        working the queue."""
        d = dispatcher([user('watcher', [SD], role='VIEWER')])
        assert (d.audience_for_group(SD, roles=('LEAD', 'VIEWER'))['to']
                == ['watcher@example.com'])

    def test_an_account_without_an_address_is_skipped(self):
        d = dispatcher([dict(user('ghost', [SD]), email=None)])
        assert d.audience_for_group(SD)['to'] == []

    def test_an_unled_queue_falls_back_to_the_administrators(self):
        """Not dropped. An unstaffed queue is a provisioning gap, and a digest
        that silently goes nowhere is how it stays one."""
        d = dispatcher([user('root', [], role='ADMIN')])
        audience = d.audience_for_group(NW)
        assert audience['to'] == ['root@example.com']
        assert audience['cc'] == []
        assert audience['orphaned'] is True

    def test_a_staffed_queue_is_not_flagged_as_orphaned(self):
        d = dispatcher([user('asha', [SD])])
        assert d.audience_for_group(SD)['orphaned'] is False

    def test_an_inactive_account_is_not_a_recipient(self):
        d = dispatcher([dict(user('gone', [SD]), active=0)])
        assert d.audience_for_group(SD)['to'] == []


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

    def send(self, to, subject, html_body, cc=None, run_type=None,
             audience=None, ticket_count=0, new_count=0, **kw):
        self.messages.append({'to': list(to), 'cc': list(cc or []),
                              'subject': subject, 'body': html_body,
                              'run_type': run_type, 'tickets': ticket_count})
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


def bodies(messages):
    """Everything each address was actually shown, To or Cc."""
    seen = {}
    for message in messages:
        for address in message['to'] + message['cc']:
            seen.setdefault(address, []).append(message['body'])
    return seen


class TestNoCrossQueueDisclosure:
    def test_the_lead_digest_carries_only_the_recipients_own_queues(self):
        """The bug: one digest over every configured queue, mailed to everyone
        scoped to any of them. The body carries descriptions and caller
        names."""
        d = wired([user('asha', [SD]), user('raj', [DS])], ROWS)
        d.send_lead_digest(ALL)

        seen = bodies(d.mailer.messages)
        assert seen['asha@example.com'] == ['INC_SD']
        assert seen['raj@example.com'] == ['INC_DS']
        assert 'INC_NW' not in ' '.join(sum(seen.values(), []))

    def test_one_email_per_queue_even_for_a_lead_who_covers_several(self):
        """Three separate messages, each nameable and forwardable on its own.
        A combined mail cannot be split by whoever receives it."""
        d = wired([user('asha', [SD, DS])], ROWS)
        d.send_lead_digest(ALL)

        assert len(d.mailer.messages) == 2
        assert sorted(m['body'] for m in d.mailer.messages) == ['INC_DS', 'INC_SD']

    def test_the_subject_names_the_queue(self):
        d = wired([user('asha', [SD, DS])], ROWS)
        d.send_lead_digest(ALL)

        for message in d.mailer.messages:
            group = SD if 'INC_SD' in message['body'] else DS
            assert message['subject'].startswith(group), message['subject']

    def test_administrators_are_copied_on_every_queue(self):
        d = wired([user('asha', [SD]), user('raj', [DS]),
                   user('root', [], role='ADMIN')], ROWS)
        d.send_lead_digest([SD, DS])

        assert len(d.mailer.messages) == 2
        for message in d.mailer.messages:
            assert message['cc'] == ['root@example.com']
        assert sorted(bodies(d.mailer.messages)['root@example.com']) == [
            'INC_DS', 'INC_SD']

    def test_an_unled_queue_reaches_the_administrators_rather_than_nobody(self):
        d = wired([user('root', [], role='ADMIN')], ROWS)
        result = d.send_lead_digest([NW])

        assert d.mailer.messages[0]['to'] == ['root@example.com']
        assert d.mailer.messages[0]['body'] == 'INC_NW'
        assert result['digests'][0]['orphaned'] is True

    def test_a_queue_with_nothing_aged_sends_no_email(self):
        d = wired([user('mei', [NW])], [ticket('INC_SD', SD)])
        result = d.send_lead_digest([NW])
        assert d.mailer.messages == []
        assert result['sent'] == 0 and result['reason'] == 'empty'

    def test_an_empty_queue_does_not_stop_the_others(self):
        """One quiet queue must not short-circuit the loop."""
        d = wired([user('asha', [SD]), user('mei', [NW])],
                  [ticket('INC_SD', SD)])
        result = d.send_lead_digest([NW, SD])
        assert [m['body'] for m in d.mailer.messages] == ['INC_SD']
        assert result['sent'] == 1

    def test_a_lead_with_no_scope_receives_nothing(self):
        """Previously an empty scope meant every queue, so this lead would
        have been sent the whole estate."""
        d = wired([user('asha', [], role='LEAD')], ROWS)
        result = d.send_lead_digest(ALL)
        assert d.mailer.messages == []
        assert result['sent'] == 0

    def test_no_recipients_at_all_is_reported(self):
        d = wired([], ROWS)
        assert d.send_lead_digest(ALL)['reason'] == 'no_recipients'

    def test_an_empty_argument_falls_back_to_the_configured_queues(self):
        """How the scheduler asks for 'everything': a digest entry with no
        assignment_groups covers whatever is configured - as one email each,
        not one shared email."""
        d = wired([user('asha', [SD]), user('raj', [DS]), user('mei', [NW])], ROWS)
        d.send_lead_digest([])
        assert sorted(m['body'] for m in d.mailer.messages) == [
            'INC_DS', 'INC_NW', 'INC_SD']

    def test_no_configured_queues_is_distinguished_from_no_recipients(self):
        """Different remedies, and a zero count alone cannot tell them apart."""
        d = wired([user('asha', [SD])], ROWS, assignment_groups=[])
        assert d.send_lead_digest([])['reason'] == 'no_groups'


class TestRiskAlerts:
    def test_alerts_are_split_by_queue(self):
        d = wired([user('asha', [SD]), user('raj', [DS])], ROWS)
        d.send_risk_alerts(ROWS)

        seen = bodies(d.mailer.messages)
        assert seen['asha@example.com'] == ['INC_SD']
        assert seen['raj@example.com'] == ['INC_DS']

    def test_the_subject_names_the_queue(self):
        d = wired([user('asha', [SD])], ROWS)
        d.send_risk_alerts([ticket('INC_SD', SD)])
        assert d.mailer.messages[0]['subject'].startswith(SD)

    def test_viewers_are_addressed_as_well_as_leads(self):
        """Wider than the digest: an alert is something to act on now, and the
        viewers are the people working the queue."""
        d = wired([user('asha', [SD]), user('watcher', [SD], role='VIEWER')], ROWS)
        d.send_risk_alerts([ticket('INC_SD', SD)])
        assert sorted(d.mailer.messages[0]['to']) == [
            'asha@example.com', 'watcher@example.com']

    def test_administrators_are_copied(self):
        d = wired([user('asha', [SD]), user('root', [], role='ADMIN')], ROWS)
        d.send_risk_alerts([ticket('INC_SD', SD)])
        assert d.mailer.messages[0]['cc'] == ['root@example.com']

    def test_a_queue_with_no_alerts_is_not_emailed(self):
        d = wired([user('mei', [NW]), user('asha', [SD])], ROWS)
        d.send_risk_alerts([ticket('INC_SD', SD)])
        assert [m['to'] for m in d.mailer.messages] == [['asha@example.com']]

    def test_an_alert_with_no_assignment_group_is_reported_not_guessed(self):
        """It belongs to no queue, so there is no correct audience. Sending it
        to one anyway puts an unowned ticket in front of a team that does not
        own it either."""
        d = wired([user('asha', [SD]), user('root', [], role='ADMIN')], ROWS)
        result = d.send_risk_alerts([ticket('INC_ORPHAN', '')])

        assert d.mailer.messages == []
        assert result['ungrouped'] == 1
        assert result['reason'] == 'no_groups'

    def test_no_alerts_is_not_an_error(self):
        d = wired([user('asha', [SD])], ROWS)
        assert d.send_risk_alerts([]) == {'sent': 0, 'alerts': 0}

    def test_every_alert_is_delivered_somewhere(self):
        """The count that would catch a filter dropping rows on the floor."""
        d = wired([user('asha', [SD]), user('raj', [DS]), user('mei', [NW])], ROWS)
        result = d.send_risk_alerts(ROWS)
        assert result['delivered'] == len(ROWS)
        assert result['groups'] == 3
