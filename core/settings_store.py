"""Settings an administrator can change at runtime.

Layered over config/conf.json rather than replacing it:

    Settings.get('thresholds.aged_after_days')
        -> runtime_setting override, if one exists and the key is allowed
        -> the value in conf.json
        -> the caller's default

The file remains the bootstrap. It carries the Vault address, the database
host and the OIDC registration - everything that has to be correct before the
service can tell anybody that something is not. Those keys are not overridable
and never will be: an override lives in the database, and reaching the
database is the thing they configure.

Three properties this has to hold.

**Reads must be free.** Settings.get() is called on every request and for
every ticket scored. A database round trip per call would put the config
table in the hot path of the whole service. Values are cached in process and
refreshed on a timer; a write through this class refreshes immediately, so
the only staleness is between processes - the web app seeing a change made by
a CLI run - and that resolves within CACHE_TTL_SECONDS.

**A broken override must not break the service.** If the table is missing -
migration 007 not yet applied - or the database is briefly unreachable, the
overlay yields nothing and every caller falls through to the file. The
service runs on the configuration it started with rather than refusing to
start. The failure is logged once, not per read.

**Only an allow-list is overridable.** Held by the caller (web.admin_settings)
and passed in, so this module stays unaware of what the UI happens to expose,
and a key that is not on the list cannot be set even by a direct call.
"""

import datetime
import json
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional

from core.logging_setup import get_logger
from core.timeutil import utc_now

log = get_logger('core.settings_store')

# How long a cached overlay is trusted. Short enough that a change made by a
# cron-run CLI shows up in the web process within a reasonable time; long
# enough that the config table is not queried thousands of times a minute.
CACHE_TTL_SECONDS = 30


class SettingsOverlayError(RuntimeError):
    """A write that must not be applied. Message is for the administrator."""


class SettingsOverlay:
    """Database-backed overrides for an allow-listed set of config keys."""

    def __init__(self, db, allowed: Optional[Iterable[str]] = None,
                 clock: Callable[[], datetime.datetime] = utc_now):
        self.db = db
        self._allowed = set(allowed or ())
        self._clock = clock

        self._lock = threading.Lock()
        self._cache: Dict[str, Any] = {}
        self._loaded_at: Optional[datetime.datetime] = None
        self._warned = False

    # -- allow-list --------------------------------------------------------

    def allow(self, keys: Iterable[str]) -> None:
        """Declare which keys may be overridden.

        Called once at wiring time. Keys already stored but no longer allowed
        are ignored on read - so removing a key from the registry disables its
        override without needing a migration to delete the row.
        """
        self._allowed = set(keys)
        self.invalidate()

    def is_allowed(self, key: str) -> bool:
        return key in self._allowed

    # -- reads -------------------------------------------------------------

    def current(self) -> Dict[str, Any]:
        """Every live override, keyed by dotted path."""
        with self._lock:
            if self._fresh():
                return dict(self._cache)

        loaded = self._load()

        with self._lock:
            self._cache = loaded
            self._loaded_at = self._clock()
            return dict(loaded)

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self._allowed:
            return default
        values = self.current()
        return values[key] if key in values else default

    def has(self, key: str) -> bool:
        return key in self.current()

    def _fresh(self) -> bool:
        if self._loaded_at is None:
            return False
        age = (self._clock() - self._loaded_at).total_seconds()
        return age < CACHE_TTL_SECONDS

    def _load(self) -> Dict[str, Any]:
        try:
            rows = self.db.query(
                'SELECT setting_key, value_json FROM runtime_setting')
        except Exception:
            # Migration not applied, or the database is momentarily away.
            # Either way the right answer is the file, not an exception on a
            # code path that every request goes through.
            if not self._warned:
                log.warning(
                    'runtime_setting is not readable - configuration overrides '
                    'are being ignored and conf.json is in force. Apply '
                    'db/migrations/007_admin_settings.sql if this is a new '
                    'deployment.', exc_info=True)
                self._warned = True
            return {}

        self._warned = False
        values: Dict[str, Any] = {}
        for row in rows:
            key = row['setting_key']
            if key not in self._allowed:
                continue
            try:
                values[key] = json.loads(row['value_json'])
            except (TypeError, ValueError):
                # One unreadable row must not discard the rest. The file value
                # wins for this key, which is the conservative outcome.
                log.error('Override for %r is not valid JSON and was skipped', key)
        return values

    def invalidate(self) -> None:
        with self._lock:
            self._loaded_at = None

    # -- writes ------------------------------------------------------------

    def set(self, key: str, value: Any, changed_by: str) -> None:
        if key not in self._allowed:
            raise SettingsOverlayError(f'{key} is not an overridable setting.')

        encoded = json.dumps(value)
        previous = self.current().get(key)
        now = self._clock()

        self.db.upsert('runtime_setting', {
            'setting_key': key,
            'value_json': encoded,
            'updated_by': changed_by,
            'updated_at': now,
        }, update_columns=['value_json', 'updated_by', 'updated_at'])

        self._audit(key, previous, value, changed_by, now)
        self.invalidate()

        log.warning('Setting %s changed by %s: %r -> %r',
                    key, changed_by, previous, value)

    def clear(self, key: str, changed_by: str) -> bool:
        """Drop an override so the conf.json value applies again."""
        if key not in self.current():
            return False

        previous = self.current().get(key)
        now = self._clock()

        self.db.delete('runtime_setting',
                       conditions=[{'col': 'setting_key', 'op': 'eq', 'val': key}])
        self._audit(key, previous, None, changed_by, now)
        self.invalidate()

        log.warning('Override for %s removed by %s (was %r) - the conf.json '
                    'value now applies', key, changed_by, previous)
        return True

    def _audit(self, key: str, old: Any, new: Any,
               changed_by: str, when: datetime.datetime) -> None:
        try:
            self.db.insert('setting_audit', {
                'setting_key': key,
                'old_value': None if old is None else json.dumps(old),
                'new_value': None if new is None else json.dumps(new),
                'changed_by': changed_by,
                'changed_at': when,
            })
        except Exception:
            # The change itself has already been applied. Losing the audit row
            # is bad, and refusing the change that was asked for because the
            # history table is unavailable is worse - and would leave the
            # administrator with no way to tell which of the two happened.
            log.exception('Could not record the audit row for %s', key)

    def history(self, limit: int = 50,
                key: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = 'SELECT * FROM setting_audit'
        params: List[Any] = []
        if key:
            sql += ' WHERE setting_key = %s'
            params.append(key)
        sql += ' ORDER BY changed_at DESC, id DESC LIMIT %s'
        params.append(int(limit))
        try:
            return self.db.query(sql, params)
        except Exception:
            log.debug('setting_audit is not readable')
            return []
