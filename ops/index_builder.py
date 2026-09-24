"""Builds the evidence index: resolved-incident and KB embeddings, plus the
resolution-time baselines.

Run once as a backfill, then nightly for the delta. Embedding is skipped for
any record whose text hash is unchanged, so the nightly pass costs almost
nothing after the first run.
"""

import datetime
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from core.snow.base import parse_ts
from pipeline.retrieval import (INCIDENT, KB, incident_document, kb_document,
                                refresh_resolution_stats, text_hash)

log = get_logger('ops.index')


class IndexBuilder:
    def __init__(self, context):
        self.ctx = context
        self.db = context.db
        self.index = context.index

    # -- resolved incidents ------------------------------------------------

    def fetch_resolved(self, days: int = 180) -> List[Dict[str, Any]]:
        end = datetime.datetime.now()
        start = end - datetime.timedelta(days=days)

        log.info('Fetching resolved incidents from %s to %s', start.date(), end.date())
        raw = self.ctx.incidents.get_closed_between(start, end)

        records = []
        for row in raw:
            opened = parse_ts(row.get('opened_at')) or parse_ts(row.get('sys_created_on'))
            resolved = parse_ts(row.get('resolved_at'))
            hours = (round((resolved - opened).total_seconds() / 3600.0, 2)
                     if opened and resolved and resolved > opened else None)

            group = row.get('assignment_group')
            records.append({
                'number': (row.get('number') or '').strip(),
                'short_description': (row.get('short_description') or '').strip(),
                'description': row.get('description') or '',
                'category': (row.get('category') or '').strip(),
                'subcategory': (row.get('subcategory') or '').strip(),
                'ci': (group.get('display_value') if isinstance(row.get('cmdb_ci'), dict)
                       else row.get('cmdb_ci')) or '',
                'close_code': (row.get('close_code') or '').strip(),
                'close_notes': (row.get('close_notes') or '').strip(),
                'assignment_group': (group.get('display_value')
                                     if isinstance(group, dict) else group) or '',
                'resolution_hours': hours,
            })

        log.info('Fetched %d resolved incidents', len(records))
        return [r for r in records if r['number']]

    def index_resolved_incidents(self, days: int = 180,
                                 records: Optional[List[Dict[str, Any]]] = None) -> int:
        llm = self.ctx.llm
        if llm is None:
            log.warning('No LLM client - skipping incident indexing')
            return 0

        records = records if records is not None else self.fetch_resolved(days)

        # Only index tickets that actually carry a usable resolution - a blank
        # close note is not evidence of anything.
        usable = [r for r in records
                  if r['close_notes'] and len(r['close_notes']) > 30
                  and (r['short_description'] or r['description'])]
        log.info('%d of %d resolved incidents have usable closure notes',
                 len(usable), len(records))

        existing = self.index.existing_hashes(INCIDENT)
        pending, documents = [], []

        for record in usable:
            document = incident_document(record)
            digest = text_hash(document)
            if existing.get(record['number']) == digest:
                continue
            pending.append((record, digest))
            documents.append(document)

        if not pending:
            log.info('Incident index already current')
            return 0

        log.info('Embedding %d incidents', len(pending))
        vectors = llm.embed(documents)

        items = []
        for (record, digest), vector in zip(pending, vectors):
            items.append((record['number'], digest, vector, {
                'short_description': record['short_description'][:400],
                'close_code': record['close_code'],
                'close_notes': record['close_notes'][:1500],
                'category': record['category'],
                'subcategory': record['subcategory'],
                'assignment_group': record['assignment_group'],
                'resolution_hours': record['resolution_hours'],
            }))

        written = self.index.upsert_many(INCIDENT, items)
        log.info('Indexed %d resolved incidents', written)
        return written

    # -- knowledge ---------------------------------------------------------

    def index_knowledge(self) -> int:
        llm = self.ctx.llm
        if llm is None:
            log.warning('No LLM client - skipping KB indexing')
            return 0

        articles = self.ctx.knowledge.get_published()
        log.info('Fetched %d published KB articles', len(articles))

        existing = self.index.existing_hashes(KB)
        pending, documents = [], []

        for article in articles:
            key = article.get('number') or article.get('sys_id')
            if not key:
                continue
            document = kb_document(article)
            if not document.strip():
                continue
            digest = text_hash(document)
            if existing.get(key) == digest:
                continue
            pending.append((key, article, digest))
            documents.append(document)

        if not pending:
            log.info('KB index already current')
            return 0

        log.info('Embedding %d KB articles', len(pending))
        vectors = llm.embed(documents)

        items = [
            (key, digest, vector, {
                'short_description': article['short_description'][:400],
                'body': article['body'][:2000],
                'category': article.get('category', ''),
            })
            for (key, article, digest), vector in zip(pending, vectors)
        ]

        written = self.index.upsert_many(KB, items)
        log.info('Indexed %d KB articles', written)
        return written

    # -- baselines ---------------------------------------------------------

    def refresh_baselines(self, days: int = 180,
                          records: Optional[List[Dict[str, Any]]] = None) -> int:
        records = records if records is not None else self.fetch_resolved(days)
        return refresh_resolution_stats(self.db, records)

    # -- one-shot backfill -------------------------------------------------

    def full_backfill(self, days: int = 180) -> Dict[str, int]:
        """Fetch once, use for both the index and the baselines."""
        records = self.fetch_resolved(days)
        return {
            'fetched': len(records),
            'incidents_indexed': self.index_resolved_incidents(records=records),
            'kb_indexed': self.index_knowledge(),
            'baselines': self.refresh_baselines(records=records),
        }
