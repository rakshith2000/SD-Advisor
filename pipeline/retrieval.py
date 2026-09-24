"""Grounded retrieval: comparable resolved incidents and relevant KB articles.

This is what separates a useful recommendation from a plausible-sounding one.
Instead of asking the model "what should happen next" in a vacuum, we hand it
three tickets that looked like this one and what actually fixed them, plus the
KB articles that apply.

Storage is MySQL - vectors as float32 blobs, cosine in numpy. At service-desk
scale (tens of thousands of resolved incidents, hundreds of KB articles) an
in-process matrix is both simpler and faster than standing up a vector
database, and it keeps the deployment footprint identical to the existing tool.

If the index is empty (before the backfill has run) everything degrades to
ServiceNow text search rather than failing.
"""

import datetime
import hashlib
import json
import threading
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.logging_setup import get_logger

log = get_logger('pipeline.retrieval')

INCIDENT = 'incident'
KB = 'kb'


def text_hash(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def pack_vector(vector: List[float]) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def unpack_vector(blob: bytes, dims: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32, count=dims)


def incident_document(ticket: Dict[str, Any]) -> str:
    """The text we embed for an incident. Short, symptom-focused."""
    parts = [
        ticket.get('short_description') or '',
        (ticket.get('description') or '')[:1500],
        f"category: {ticket.get('category') or ''} / {ticket.get('subcategory') or ''}",
        f"ci: {ticket.get('ci') or ''}",
    ]
    return '\n'.join(p for p in parts if p.strip())


def kb_document(article: Dict[str, Any]) -> str:
    return '\n'.join(filter(None, [
        article.get('short_description') or '',
        (article.get('body') or '')[:3000],
    ]))


class VectorIndex:
    """In-memory cosine index backed by the embedding_store table."""

    def __init__(self, db, dims: int):
        self.db = db
        self.dims = dims
        self._lock = threading.RLock()
        self._matrix: Dict[str, Optional[np.ndarray]] = {INCIDENT: None, KB: None}
        self._keys: Dict[str, List[str]] = {INCIDENT: [], KB: []}
        self._meta: Dict[str, List[Dict[str, Any]]] = {INCIDENT: [], KB: []}
        self._loaded_at: Dict[str, Optional[datetime.datetime]] = {INCIDENT: None, KB: None}

    # -- loading -----------------------------------------------------------

    def load(self, entity_type: str, force: bool = False) -> int:
        with self._lock:
            if not force and self._matrix.get(entity_type) is not None:
                return len(self._keys[entity_type])

            rows = self.db.retrieve(
                'embedding_store',
                columns=['entity_key', 'dims', 'vector', 'meta'],
                conditions=[{'col': 'entity_type', 'op': 'eq', 'val': entity_type}],
            )

            if not rows:
                self._matrix[entity_type] = None
                self._keys[entity_type] = []
                self._meta[entity_type] = []
                log.info('Vector index for %s is empty', entity_type)
                return 0

            vectors, keys, metas = [], [], []
            for row in rows:
                dims = int(row['dims'])
                if dims != self.dims:
                    continue
                vectors.append(unpack_vector(row['vector'], dims))
                keys.append(row['entity_key'])
                raw_meta = row.get('meta')
                if isinstance(raw_meta, (str, bytes)):
                    try:
                        raw_meta = json.loads(raw_meta)
                    except (ValueError, TypeError):
                        raw_meta = {}
                metas.append(raw_meta or {})

            if not vectors:
                self._matrix[entity_type] = None
                return 0

            matrix = np.vstack(vectors).astype(np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            matrix = matrix / norms                      # pre-normalise once

            self._matrix[entity_type] = matrix
            self._keys[entity_type] = keys
            self._meta[entity_type] = metas
            self._loaded_at[entity_type] = datetime.datetime.now()

            log.info('Loaded %d %s vectors into memory', len(keys), entity_type)
            return len(keys)

    def invalidate(self, entity_type: Optional[str] = None) -> None:
        with self._lock:
            targets = [entity_type] if entity_type else [INCIDENT, KB]
            for target in targets:
                self._matrix[target] = None

    # -- search ------------------------------------------------------------

    def search(self, entity_type: str, query_vector: List[float], top_k: int = 5,
               where: Optional[Dict[str, Any]] = None,
               min_score: float = 0.0) -> List[Tuple[str, float, Dict[str, Any]]]:
        """Cosine search with optional metadata pre-filtering."""
        with self._lock:
            self.load(entity_type)
            matrix = self._matrix.get(entity_type)
            if matrix is None or not len(matrix):
                return []

            keys = self._keys[entity_type]
            metas = self._meta[entity_type]

            candidate_idx = np.arange(len(keys))
            if where:
                mask = [
                    all(str(metas[i].get(k, '')).lower() == str(v).lower()
                        for k, v in where.items())
                    for i in candidate_idx
                ]
                candidate_idx = candidate_idx[np.array(mask, dtype=bool)]
                if not len(candidate_idx):
                    return []

            query = np.asarray(query_vector, dtype=np.float32)
            norm = np.linalg.norm(query) or 1.0
            query = query / norm

            scores = matrix[candidate_idx] @ query

            take = min(top_k, len(scores))
            best = np.argpartition(-scores, take - 1)[:take]
            best = best[np.argsort(-scores[best])]

            results = []
            for position in best:
                score = float(scores[position])
                if score < min_score:
                    continue
                index = int(candidate_idx[position])
                results.append((keys[index], score, metas[index]))
            return results

    # -- writing -----------------------------------------------------------

    def upsert_many(self, entity_type: str,
                    items: List[Tuple[str, str, List[float], Dict[str, Any]]]) -> int:
        """items: (entity_key, text_hash, vector, meta)"""
        written = 0
        now = datetime.datetime.now()

        for entity_key, digest, vector, meta in items:
            self.db.upsert('embedding_store', {
                'entity_type': entity_type,
                'entity_key': entity_key,
                'text_hash': digest,
                'dims': self.dims,
                'vector': pack_vector(vector),
                'meta': json.dumps(meta, default=str),
                'updated_at': now,
            }, update_columns=['text_hash', 'dims', 'vector', 'meta', 'updated_at'])
            written += 1

        if written:
            self.invalidate(entity_type)
        return written

    def existing_hashes(self, entity_type: str) -> Dict[str, str]:
        rows = self.db.retrieve(
            'embedding_store', columns=['entity_key', 'text_hash'],
            conditions=[{'col': 'entity_type', 'op': 'eq', 'val': entity_type}])
        return {r['entity_key']: r['text_hash'] for r in rows}


class Retriever:
    """Facade used by the advisor: give me evidence for this ticket."""

    def __init__(self, db, llm, index: VectorIndex, knowledge_reader=None,
                 min_similarity: float = 0.55):
        self.db = db
        self.llm = llm
        self.index = index
        self.knowledge_reader = knowledge_reader
        self.min_similarity = min_similarity

    def evidence_for(self, ticket: Dict[str, Any], top_k: int = 3
                     ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Return (similar_incidents, kb_articles)."""
        document = incident_document(ticket)
        if not document.strip():
            return [], []

        try:
            query_vector = self.llm.embed_one(document)
        except Exception:
            log.warning('Embedding failed for %s; falling back to text search',
                        ticket.get('incident_number'))
            return [], self._kb_text_fallback(ticket, top_k)

        similar = self._similar_incidents(ticket, query_vector, top_k)
        kb_articles = self._kb_articles(ticket, query_vector, top_k)

        if not kb_articles:
            kb_articles = self._kb_text_fallback(ticket, top_k)

        return similar, kb_articles

    # -- internals ---------------------------------------------------------

    def _similar_incidents(self, ticket: Dict[str, Any], query_vector: List[float],
                           top_k: int) -> List[Dict[str, Any]]:
        # Try category-scoped first - a printer fix is not evidence for a VPN
        # fault even if the wording is similar - then widen if nothing lands.
        scoped = self.index.search(
            INCIDENT, query_vector, top_k=top_k,
            where={'category': ticket.get('category') or ''},
            min_score=self.min_similarity)

        results = scoped or self.index.search(
            INCIDENT, query_vector, top_k=top_k, min_score=self.min_similarity)

        similar = []
        for key, score, meta in results:
            if key == ticket.get('incident_number'):
                continue
            similar.append({
                'number': key,
                'score': round(score, 3),
                'short_description': meta.get('short_description', ''),
                'close_code': meta.get('close_code', ''),
                'close_notes': meta.get('close_notes', ''),
                'resolution_hours': meta.get('resolution_hours'),
                'assignment_group': meta.get('assignment_group', ''),
                'source': 'vector',
            })
        return similar[:top_k]

    def _kb_articles(self, ticket: Dict[str, Any], query_vector: List[float],
                     top_k: int) -> List[Dict[str, Any]]:
        results = self.index.search(KB, query_vector, top_k=top_k,
                                    min_score=self.min_similarity)
        return [{
            'number': key,
            'score': round(score, 3),
            'short_description': meta.get('short_description', ''),
            'body': meta.get('body', ''),
            'source': 'vector',
        } for key, score, meta in results]

    def _kb_text_fallback(self, ticket: Dict[str, Any], top_k: int) -> List[Dict[str, Any]]:
        if not self.knowledge_reader:
            return []
        terms = f"{ticket.get('short_description') or ''} {ticket.get('subcategory') or ''}"
        try:
            return self.knowledge_reader.search(terms, limit=top_k)
        except Exception:
            log.debug('KB fallback search failed for %s', ticket.get('incident_number'))
            return []


# ---------------------------------------------------------------------------
# Resolution-time baselines (feeds expected_resolution_hours / p90_overrun)
# ---------------------------------------------------------------------------

def scope_key(category: str, subcategory: str) -> str:
    return f"{(category or 'unknown').strip().lower()}|{(subcategory or 'unknown').strip().lower()}"


def refresh_resolution_stats(db, resolved_incidents: List[Dict[str, Any]],
                             min_sample: int = 5) -> int:
    """Rebuild p50/p90 resolution hours per category+subcategory.

    A ticket is only flagged as overrunning when it is slow *for its own kind*,
    which stops every long-by-nature request being permanently red.
    """
    buckets: Dict[str, List[float]] = {}

    for incident in resolved_incidents:
        hours = incident.get('resolution_hours')
        if hours is None or hours <= 0:
            continue
        key = scope_key(incident.get('category', ''), incident.get('subcategory', ''))
        buckets.setdefault(key, []).append(float(hours))

    now = datetime.datetime.now()
    written = 0

    for key, samples in buckets.items():
        if len(samples) < min_sample:
            continue
        array = np.array(samples, dtype=np.float64)
        db.upsert('resolution_stat', {
            'scope_key': key,
            'sample_size': int(len(samples)),
            'p50_hours': round(float(np.percentile(array, 50)), 2),
            'p90_hours': round(float(np.percentile(array, 90)), 2),
            'updated_at': now,
        }, update_columns=['sample_size', 'p50_hours', 'p90_hours', 'updated_at'])
        written += 1

    log.info('Refreshed resolution baselines for %d category buckets', written)
    return written


def load_baselines(db) -> Dict[str, Dict[str, float]]:
    rows = db.retrieve('resolution_stat')
    return {
        r['scope_key']: {
            'p50_hours': float(r['p50_hours']),
            'p90_hours': float(r['p90_hours']),
            'sample_size': int(r['sample_size']),
        }
        for r in rows
    }
