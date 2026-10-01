"""Shared ServiceNow REST plumbing.

Read-only by design: this module exposes GET helpers only. Nothing in the
advisor is capable of mutating a ticket, which keeps the risk profile of the
whole service at "it might send a wrong email".

Improvements over the clients in the existing project:
  * one requests.Session with connection pooling and retry/backoff, rather
    than a bare requests.get per call
  * TLS verification on
  * query parameters passed through requests so they are properly encoded
  * transparent pagination via sysparm_offset
"""

import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.logging_setup import get_logger
from core.timeutil import TS_FORMAT, parse_ts, utc_now   # noqa: F401  (re-exported)

log = get_logger('core.snow')

# Every read that carries a timestamp asks for this. Reference and choice
# fields then arrive as {'display_value': ..., 'value': ...}: take
# display_value for anything a human reads, and value for every datetime,
# because value is UTC while display_value is the integration user's timezone
# rendered in the integration user's date format.
DISPLAY_AND_VALUE = 'all'


class ServiceNowError(RuntimeError):
    pass


def query_ts(ts: datetime.datetime) -> str:
    """Render a UTC timestamp for use in an encoded query.

    A plain literal, not javascript:gs.dateGenerate(). gs.dateGenerate
    interprets the wall clock it is given in the *session user's* timezone and
    converts to UTC, so feeding it a UTC value shifts the window by that
    user's offset. An encoded query compares against the stored UTC value
    directly, which is what the caller already holds.

    Note that an unparseable condition is dropped by ServiceNow rather than
    rejected, so a malformed boundary widens the result set instead of
    narrowing it. Verified against this instance with ops/probe_resolved.py.
    """
    return ts.strftime(TS_FORMAT)


class ServiceNowClient:
    """Base client; one instance is shared by all table-specific clients."""

    def __init__(self, settings, vault):
        url = str(settings.require('servicenow.url')).rstrip('/')
        self.base_url = url
        self.timeout = int(settings.get('servicenow.timeout_seconds', 60))
        self.page_size = int(settings.get('servicenow.page_size', 200))
        self.verify_tls = bool(settings.get('servicenow.verify_tls', True))
        # Backstop against a runaway pagination loop, not a tuning knob.
        self.max_offset = int(settings.get('servicenow.max_offset', 200000))

        # Required: a missing key here would otherwise surface as an obscure
        # Vault error rather than a configuration one.
        snow_path = settings.require('vault.paths.servicenow')
        username, password = vault.credential_pair(snow_path)
        self.username = username

        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Accept-Language': 'en',
        })

        retry = Retry(
            total=int(settings.get('servicenow.max_retries', 3)),
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(['GET']),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)

        log.info('ServiceNow client ready (%s)', self.base_url)

    # -- core request ------------------------------------------------------

    def _fetch_page(self, table: str,
                    params: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """One GET. Returns (rows, total_matching) - total is None if absent.

        X-Total-Count is how many records the QUERY matches, counted before
        read ACLs are applied, so it is the right thing to page against even
        though fewer rows come back.
        """
        url = f'{self.base_url}/api/now/table/{table}'
        try:
            response = self.session.get(
                url, params=params, timeout=self.timeout, verify=self.verify_tls
            )
        except requests.RequestException as exc:
            raise ServiceNowError(f'{table}: request failed - {exc}') from exc

        if response.status_code >= 400:
            raise ServiceNowError(
                f'{table}: HTTP {response.status_code} - {response.text[:300]}'
            )

        try:
            rows = response.json().get('result', [])
        except ValueError as exc:
            raise ServiceNowError(f'{table}: non-JSON response - {exc}') from exc

        total: Optional[int] = None
        raw_total = response.headers.get('X-Total-Count')
        if raw_total is not None:
            try:
                total = int(raw_total)
            except (TypeError, ValueError):
                total = None

        return rows, total

    def get(self, table: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Single page GET against /api/now/table/<table>."""
        return self._fetch_page(table, params)[0]

    def _pages(self, table: str, params: Dict[str, Any]) -> Iterator[List[Dict[str, Any]]]:
        """Walk the full result window, one page at a time.

        Deliberately does NOT stop on a short page. ServiceNow applies the
        limit/offset window at the database level and filters by read ACL
        afterwards, so a page can come back partly - or entirely - empty while
        thousands of records remain behind it. kb_knowledge is the worst case,
        because knowledge base access is governed by user criteria.

        Stopping on a short page silently truncated the KB index to a single
        page. The offset therefore advances by the requested page size, not by
        the number of rows received, and the loop ends on the record count
        rather than on the row count.
        """
        offset = 0
        total: Optional[int] = None
        seen = 0

        while True:
            page_params = dict(params)
            page_params['sysparm_limit'] = self.page_size
            page_params['sysparm_offset'] = offset

            page, page_total = self._fetch_page(table, page_params)
            if page_total is not None:
                total = page_total
            seen += len(page)

            if page:
                yield page

            offset += self.page_size

            if total is not None:
                if offset >= total:
                    break
            elif not page:
                # No count header to go on; an empty page is the only safe
                # stopping condition left.
                break

            if offset >= self.max_offset:
                log.warning('%s: stopped at the %d-record safety cap with %d collected; '
                            'raise servicenow.max_offset if the query is meant to be '
                            'this large', table, self.max_offset, seen)
                break

        if total is not None and seen < total:
            # Not an error - this is what read ACLs look like - but the gap is
            # worth stating, because it is invisible in the returned data.
            log.info('%s: query matched %d records, %d readable by %s',
                     table, total, seen, self.username)

    def get_all(self, table: str, params: Dict[str, Any],
                max_records: Optional[int] = None) -> List[Dict[str, Any]]:
        """Paginated GET. Stops at max_records if supplied."""
        collected: List[Dict[str, Any]] = []

        for page in self._pages(table, params):
            collected.extend(page)
            if max_records is not None and len(collected) >= max_records:
                return collected[:max_records]

        return collected

    def iter_all(self, table: str, params: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        for page in self._pages(table, params):
            for row in page:
                yield row

    # -- helpers -----------------------------------------------------------

def display_value(field: Any) -> str:
    """The human-readable form of a field. Use for references and choices."""
    if field is None:
        return ''
    if isinstance(field, dict):
        return str(field.get('display_value', '') or '').strip()
    return str(field).strip()


def utc_value(field: Any) -> str:
    """The raw stored form of a field. Use for every datetime.

    Under sysparm_display_value=all a datetime arrives as both an unambiguous
    UTC string in `value` and a timezone-and-format-dependent string in
    `display_value`. Only the former can be compared against utc_now()
    without drifting.

    Falls back to display_value if `value` is absent, so a response fetched
    with display_value=true still yields something rather than nothing.
    """
    if field is None:
        return ''
    if isinstance(field, dict):
        raw = field.get('value')
        if raw in (None, ''):
            raw = field.get('display_value')
        return str(raw or '').strip()
    return str(field).strip()


def utc_ts(field: Any):
    """Parse a datetime field straight to a naive UTC datetime."""
    return parse_ts(utc_value(field))


def reference_sys_id(field: Any) -> str:
    """Pull the sys_id out of a reference field's link."""
    if isinstance(field, dict):
        link = field.get('link') or ''
        if link:
            return str(link).rstrip('/').split('/')[-1].strip()
        value = field.get('value')
        if value:
            return str(value).strip()
    return ''
