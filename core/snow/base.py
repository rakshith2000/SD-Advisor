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

log = get_logger('core.snow')

TS_FORMAT = '%Y-%m-%d %H:%M:%S'


class ServiceNowError(RuntimeError):
    pass


def js_date(ts: datetime.datetime) -> str:
    """Render a timestamp as a ServiceNow gs.dateGenerate() expression.

    Using an explicit generated date rather than a relative helper such as
    gs.daysAgoStart() keeps the window deterministic and independent of the
    integration user's timezone profile.
    """
    return "javascript:gs.dateGenerate('{0}','{1}')".format(
        ts.strftime('%Y-%m-%d'), ts.strftime('%H:%M:%S')
    )


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

        self._tz_offset_seconds: Optional[float] = None
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

    def timezone_offset_seconds(self) -> float:
        """Offset between the integration user's displayed time and UTC.

        Attachment timestamps come back in the user's display timezone while
        history timestamps come back in UTC; without this correction the
        "was an email attached around the time of this event" window silently
        misses by several hours. Computed once and cached.
        """
        if self._tz_offset_seconds is not None:
            return self._tz_offset_seconds

        params = {
            'sysparm_fields': 'sys_created_on',
            'sysparm_query': f'user_name={self.username}',
            'sysparm_limit': 1,
        }
        try:
            utc_rows = self.get('sys_user', params)
            display_rows = self.get('sys_user', dict(params, sysparm_display_value='true'))
            utc = datetime.datetime.strptime(utc_rows[0]['sys_created_on'], TS_FORMAT)
            local = datetime.datetime.strptime(display_rows[0]['sys_created_on'], TS_FORMAT)
            self._tz_offset_seconds = (utc - local).total_seconds()
        except Exception:
            log.warning('Could not determine ServiceNow timezone offset; assuming UTC')
            self._tz_offset_seconds = 0.0

        return self._tz_offset_seconds


def parse_ts(value: Any) -> Optional[datetime.datetime]:
    """Lenient timestamp parse - ServiceNow returns '' for unset dates."""
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    for fmt in (TS_FORMAT, '%Y-%m-%d %H:%M', '%d-%m-%Y %H:%M:%S', '%m/%d/%Y %H:%M:%S'):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def display_value(field: Any) -> str:
    """Flatten a ServiceNow reference field to its display string."""
    if field is None:
        return ''
    if isinstance(field, dict):
        return str(field.get('display_value', '') or '').strip()
    return str(field).strip()


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
