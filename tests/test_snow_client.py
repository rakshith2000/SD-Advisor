"""Tests for ServiceNow REST pagination.

The regression these exist for: get_all() stopped as soon as a page came back
with fewer rows than requested. ServiceNow applies sysparm_limit/sysparm_offset
at the database level and filters by read ACL afterwards, so a short page means
nothing about whether more records follow. On kb_knowledge - where knowledge
base access is governed by user criteria - the very first page came back short
and the index was silently truncated to 191 of 4,409 published articles.

Nothing errored. The fetch simply returned less than it should have, and every
downstream count looked plausible.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.snow.base import ServiceNowClient, ServiceNowError

PAGE = 200


class FakeResponse:
    def __init__(self, rows, total=None, status=200):
        self.status_code = status
        self._rows = rows
        self.headers = {} if total is None else {'X-Total-Count': str(total)}
        self.text = ''

    def json(self):
        return {'result': self._rows}


class FakeSession:
    """Serves a table of N records through limit/offset, applying an ACL mask
    AFTER the window is taken - which is what ServiceNow does."""

    def __init__(self, total, readable=None, send_total_header=True):
        self.total = total
        # readable(index) -> bool; default everything readable.
        self.readable = readable or (lambda i: True)
        self.send_total_header = send_total_header
        self.requests = []
        self.auth = None
        self.headers = {}

    def mount(self, *_a, **_k):
        pass

    def get(self, url, params=None, timeout=None, verify=None):
        params = params or {}
        limit = int(params.get('sysparm_limit', PAGE))
        offset = int(params.get('sysparm_offset', 0))
        self.requests.append((offset, limit))

        window = range(offset, min(offset + limit, self.total))
        rows = [{'number': f'REC{i:05d}'} for i in window if self.readable(i)]
        return FakeResponse(rows, self.total if self.send_total_header else None)


class FakeSettings:
    def __init__(self, **over):
        self._v = {'servicenow.url': 'https://example.service-now.com',
                   'servicenow.page_size': PAGE,
                   'vault.paths.servicenow': 'snow_advisor'}
        self._v.update(over)

    def get(self, key, default=None):
        return self._v.get(key, default)

    def require(self, key):
        return self._v[key]


class FakeVault:
    def credential_pair(self, _path):
        return ('svc.advisor', 'secret')


def client(session, **settings):
    c = ServiceNowClient(FakeSettings(**settings), FakeVault())
    c.session = session
    return c


class TestShortPageDoesNotEndPagination:
    """The exact production failure."""

    def test_acl_filtered_first_page_does_not_truncate(self):
        # 4409 records; every 23rd is unreadable, so page 1 returns < 200.
        session = FakeSession(4409, readable=lambda i: i % 23 != 0)
        rows = client(session).get_all('kb_knowledge', {})

        assert len(session.requests) > 1, 'stopped after one short page'
        expected = sum(1 for i in range(4409) if i % 23 != 0)
        assert len(rows) == expected

    def test_a_wholly_unreadable_page_is_not_the_end(self):
        """Nothing readable in the first window, plenty behind it."""
        session = FakeSession(1000, readable=lambda i: i >= 400)
        rows = client(session).get_all('kb_knowledge', {})
        assert len(rows) == 600

    def test_offset_advances_by_page_size_not_row_count(self):
        """Advancing by len(page) would re-read filtered records forever."""
        session = FakeSession(1000, readable=lambda i: i % 2 == 0)
        client(session).get_all('incident', {})
        offsets = [offset for offset, _ in session.requests]
        assert offsets == [0, 200, 400, 600, 800]

    def test_no_duplicates_across_pages(self):
        session = FakeSession(950, readable=lambda i: i % 3 != 0)
        rows = client(session).get_all('incident', {})
        numbers = [r['number'] for r in rows]
        assert len(numbers) == len(set(numbers))


class TestStoppingConditions:
    def test_stops_at_the_total_count(self):
        session = FakeSession(450)
        rows = client(session).get_all('incident', {})
        assert len(rows) == 450
        assert len(session.requests) == 3           # 0, 200, 400

    def test_exact_multiple_of_page_size_does_not_over_fetch(self):
        session = FakeSession(400)
        client(session).get_all('incident', {})
        assert [o for o, _ in session.requests] == [0, 200]

    def test_empty_table(self):
        session = FakeSession(0)
        assert client(session).get_all('incident', {}) == []
        assert len(session.requests) == 1

    def test_without_the_count_header_an_empty_page_ends_it(self):
        """Older instances may not send X-Total-Count.

        Costs one extra round trip: the page at 400 returns 50 rows, which
        proves nothing, so we have to see the empty page at 600 to stop. That
        extra call is the price of not truncating, and it is only paid when
        the header is missing.
        """
        session = FakeSession(450, send_total_header=False)
        rows = client(session).get_all('incident', {})
        assert len(rows) == 450
        assert [o for o, _ in session.requests] == [0, 200, 400, 600]

    def test_the_count_header_avoids_that_extra_call(self):
        session = FakeSession(450)
        client(session).get_all('incident', {})
        assert [o for o, _ in session.requests] == [0, 200, 400]

    def test_without_the_header_a_short_page_still_does_not_stop(self):
        session = FakeSession(500, readable=lambda i: i < 150 or i >= 300,
                              send_total_header=False)
        rows = client(session).get_all('incident', {})
        assert len(rows) == 350

    def test_max_offset_caps_a_runaway(self, caplog):
        session = FakeSession(10_000)
        c = client(session, **{'servicenow.max_offset': 600})
        with caplog.at_level('WARNING'):
            rows = c.get_all('incident', {})
        assert len(rows) == 600
        assert 'safety cap' in caplog.text


class TestMaxRecords:
    def test_truncates_to_max_records(self):
        session = FakeSession(4409)
        rows = client(session).get_all('incident', {}, max_records=250)
        assert len(rows) == 250

    def test_does_not_keep_fetching_past_max_records(self):
        session = FakeSession(4409)
        client(session).get_all('incident', {}, max_records=250)
        assert len(session.requests) == 2


class TestIterAll:
    def test_yields_everything_despite_filtering(self):
        session = FakeSession(1000, readable=lambda i: i % 7 != 0)
        rows = list(client(session).iter_all('incident', {}))
        assert len(rows) == sum(1 for i in range(1000) if i % 7 != 0)

    def test_is_lazy(self):
        """One page fetched per consumption step, not all up front."""
        session = FakeSession(1000)
        iterator = client(session).iter_all('incident', {})
        next(iterator)
        assert len(session.requests) == 1


class TestSinglePageGet:
    def test_get_returns_rows_only(self):
        session = FakeSession(50)
        assert len(client(session).get('incident', {'sysparm_limit': 200})) == 50

    def test_http_error_raises(self):
        class Failing(FakeSession):
            def get(self, *a, **k):
                return FakeResponse([], status=403)

        with pytest.raises(ServiceNowError, match='403'):
            client(Failing(1)).get('incident', {})


class TestAclGapIsReported:
    def test_logs_the_shortfall(self, caplog):
        """Silent under-fetch is the thing that cost us; say it out loud."""
        session = FakeSession(1000, readable=lambda i: i < 191)
        with caplog.at_level('INFO'):
            client(session).get_all('kb_knowledge', {})
        assert 'matched 1000 records, 191 readable' in caplog.text

    def test_no_message_when_everything_is_readable(self, caplog):
        session = FakeSession(300)
        with caplog.at_level('INFO'):
            client(session).get_all('incident', {})
        assert 'readable by' not in caplog.text
