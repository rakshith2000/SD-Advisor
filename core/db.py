"""MySQL access layer.

Keeps the parameterised-condition idea from the existing tool (it reads well at
call sites) but fixes the things that bite at scale:
  * one long-lived connection per Database object, with ping-reconnect, instead
    of a fresh connection per ticket
  * cursors always closed via context managers
  * failures raise by default; callers opt into swallowing
  * upsert helper, since delta sync re-writes the same rows constantly
"""

import datetime
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pymysql
import pymysql.cursors

from core.logging_setup import get_logger

log = get_logger('core.db')

_OPS = {
    'eq': '= %s',
    'ne': '!= %s',
    'gt': '> %s',
    'ge': '>= %s',
    'lt': '< %s',
    'le': '<= %s',
    'l': 'LIKE %s',
    'nl': 'NOT LIKE %s',
    'null': 'IS NULL',
    'notnull': 'IS NOT NULL',
}


class Database:
    def __init__(self, host: str, name: str, user: str, password: str,
                 port: int = 3306, connect_timeout: int = 10):
        self._params = dict(
            host=host, user=user, password=password, database=name, port=int(port),
            connect_timeout=int(connect_timeout),
            cursorclass=pymysql.cursors.DictCursor,
            charset='utf8mb4',
            autocommit=False,
        )
        self._lock = threading.RLock()
        self._conn = pymysql.connect(**self._params)
        log.info('Database session established (%s/%s)', host, name)

    # -- plumbing ----------------------------------------------------------

    def _connection(self):
        try:
            self._conn.ping(reconnect=True)
        except Exception:
            log.warning('Database ping failed, reconnecting')
            self._conn = pymysql.connect(**self._params)
        return self._conn

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    @staticmethod
    def _build_conditions(conditions: Sequence[Dict[str, Any]]) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []

        for condition in conditions or []:
            col = condition['col']
            op = condition.get('op', 'eq')
            val = condition.get('val')

            if op in ('i', 'ni'):
                values = list(val or [])
                if not values:
                    # An empty IN list must match nothing rather than everything.
                    clauses.append('1 = 0' if op == 'i' else '1 = 1')
                    continue
                placeholders = ', '.join(['%s'] * len(values))
                keyword = 'IN' if op == 'i' else 'NOT IN'
                clauses.append(f'{col} {keyword} ({placeholders})')
                params.extend(values)
                continue

            if op in ('null', 'notnull'):
                clauses.append(f'{col} {_OPS[op]}')
                continue

            if op == 'raw':
                clauses.append(f'{col} {condition["expr"]}')
                if 'params' in condition:
                    params.extend(condition['params'])
                continue

            if op not in _OPS:
                raise ValueError(f'Unsupported condition operator: {op}')

            clauses.append(f'{col} {_OPS[op]}')
            params.append(val)

        return (' AND '.join(clauses), params)

    # -- reads -------------------------------------------------------------

    def query(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        with self._lock:
            with self._connection().cursor() as cursor:
                cursor.execute(sql, params or ())
                return list(cursor.fetchall())

    def query_one(self, sql: str, params: Optional[Sequence[Any]] = None) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def retrieve(self, table: str, columns: Optional[Sequence[str]] = None,
                 conditions: Optional[Sequence[Dict[str, Any]]] = None,
                 order_by: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        selected = ', '.join(columns) if columns else '*'
        sql = f'SELECT {selected} FROM {table}'

        clause, params = self._build_conditions(conditions or [])
        if clause:
            sql += f' WHERE {clause}'
        if order_by:
            sql += f' ORDER BY {order_by}'
        if limit:
            sql += f' LIMIT {int(limit)}'

        return self.query(sql, params)

    # -- writes ------------------------------------------------------------

    def insert(self, table: str, data: Dict[str, Any], ignore: bool = False) -> int:
        columns = list(data.keys())
        keyword = 'INSERT IGNORE' if ignore else 'INSERT'
        sql = (
            f'{keyword} INTO {table} ({", ".join(columns)}) '
            f'VALUES ({", ".join("%(" + c + ")s" for c in columns)})'
        )
        return self._write(sql, data)

    def upsert(self, table: str, data: Dict[str, Any],
               update_columns: Optional[Sequence[str]] = None) -> int:
        """INSERT ... ON DUPLICATE KEY UPDATE.

        Delta sync re-writes the same ticket rows on every poll, so this is the
        hot path.
        """
        columns = list(data.keys())
        updatable = list(update_columns) if update_columns else columns
        assignments = ', '.join(f'{c} = VALUES({c})' for c in updatable)
        sql = (
            f'INSERT INTO {table} ({", ".join(columns)}) '
            f'VALUES ({", ".join("%(" + c + ")s" for c in columns)}) '
            f'ON DUPLICATE KEY UPDATE {assignments}'
        )
        return self._write(sql, data)

    def update(self, table: str, values: Dict[str, Any],
               conditions: Optional[Sequence[Dict[str, Any]]] = None) -> int:
        assignments = ', '.join(f'{col} = %s' for col in values)
        sql = f'UPDATE {table} SET {assignments}'

        clause, cond_params = self._build_conditions(conditions or [])
        if clause:
            sql += f' WHERE {clause}'

        return self._write(sql, list(values.values()) + cond_params)

    def delete(self, table: str, conditions: Sequence[Dict[str, Any]]) -> int:
        clause, params = self._build_conditions(conditions)
        if not clause:
            raise ValueError('delete() requires at least one condition')
        return self._write(f'DELETE FROM {table} WHERE {clause}', params)

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> int:
        """Run an arbitrary write statement. Returns affected row count."""
        return self._write(sql, params or ())

    def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        batch = list(rows)
        if not batch:
            return 0
        with self._lock:
            connection = self._connection()
            with connection.cursor() as cursor:
                affected = cursor.executemany(sql, batch)
            connection.commit()
            return affected or 0

    def _write(self, sql: str, params: Any) -> int:
        with self._lock:
            connection = self._connection()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    affected = cursor.rowcount
                connection.commit()
                return affected
            except Exception:
                connection.rollback()
                log.exception('Database write failed: %s', sql.split('\n')[0][:160])
                raise

    # -- helpers -----------------------------------------------------------

    def get_sync_watermark(self, name: str) -> Optional[datetime.datetime]:
        row = self.query_one('SELECT watermark FROM sync_state WHERE name = %s', (name,))
        return row['watermark'] if row else None

    def set_sync_watermark(self, name: str, watermark: Optional[datetime.datetime],
                           status: str = 'OK', detail: str = '') -> None:
        self.upsert('sync_state', {
            'name': name,
            'watermark': watermark,
            'last_run_at': datetime.datetime.now(),
            'last_status': status,
            'detail': detail[:2000] if detail else None,
        }, update_columns=['watermark', 'last_run_at', 'last_status', 'detail'])


def build_database(settings, vault) -> Database:
    """Primary advisor database, credentials from Vault."""
    path = settings.get('vault.paths.database', 'sd_advisor_db')
    username, password = vault.credential_pair(path)
    return Database(
        host=settings.require('database.host'),
        name=settings.require('database.name'),
        user=username,
        password=password,
        port=settings.get('database.port', 3306),
        connect_timeout=settings.get('database.connect_timeout', 10),
    )


def build_audit_database(settings, vault) -> Optional[Database]:
    """Read-only handle on the existing audit DB, for agent coaching rollups.

    Returns None when disabled - the feature degrades quietly rather than
    taking the service down if that database is unreachable.
    """
    if not settings.get('audit_database.enabled', False):
        return None

    try:
        path = settings.get('audit_database.vault_path', 'itsm_analytics_db')
        username, password = vault.credential_pair(path)
        return Database(
            host=settings.require('audit_database.host'),
            name=settings.require('audit_database.name'),
            user=username,
            password=password,
        )
    except Exception:
        log.exception('Audit database unavailable; coaching rollup will be skipped')
        return None
