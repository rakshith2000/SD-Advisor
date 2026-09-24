"""Knowledge base reads.

Two jobs: bulk-fetch published articles for the vector index, and provide a
no-index fallback search so Phase 0 works before the embedding backfill has
run.
"""

import re
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from core.snow.base import ServiceNowClient

log = get_logger('core.snow.knowledge')

KB_FIELDS = 'sys_id,number,short_description,text,kb_category,workflow_state,sys_updated_on'

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


def strip_html(value: Any, limit: int = 4000) -> str:
    """KB bodies are HTML; the model only needs the prose."""
    if not value:
        return ''
    text = _TAG_RE.sub(' ', str(value))
    text = (text.replace('&nbsp;', ' ').replace('&amp;', '&')
                .replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"'))
    text = _WS_RE.sub(' ', text).strip()
    return text[:limit]


class KnowledgeReader:
    def __init__(self, client: ServiceNowClient):
        self.client = client

    def get_published(self, updated_since: Optional[str] = None) -> List[Dict[str, Any]]:
        query = 'workflow_state=published'
        if updated_since:
            query += f'^sys_updated_on>{updated_since}'

        rows = self.client.get_all('kb_knowledge', {
            'sysparm_query': query,
            'sysparm_fields': KB_FIELDS,
            'sysparm_display_value': 'true',
        })

        articles = []
        for row in rows:
            articles.append({
                'sys_id': row.get('sys_id', ''),
                'number': row.get('number', ''),
                'short_description': (row.get('short_description') or '').strip(),
                'body': strip_html(row.get('text')),
                'category': row.get('kb_category', ''),
                'updated_on': row.get('sys_updated_on', ''),
            })
        return articles

    def search(self, terms: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Keyword fallback when the vector index is empty or stale."""
        cleaned = ' '.join(re.findall(r'[A-Za-z0-9]{3,}', terms or ''))[:120]
        if not cleaned:
            return []

        try:
            rows = self.client.get('kb_knowledge', {
                'sysparm_query': f'workflow_state=published^123TEXTQUERY321={cleaned}',
                'sysparm_fields': KB_FIELDS,
                'sysparm_display_value': 'true',
                'sysparm_limit': limit,
            })
        except Exception:
            log.warning('KB text search failed for terms %r', cleaned[:60])
            return []

        return [{
            'number': row.get('number', ''),
            'short_description': (row.get('short_description') or '').strip(),
            'body': strip_html(row.get('text'), limit=1200),
            'score': None,
            'source': 'text_search',
        } for row in rows]
