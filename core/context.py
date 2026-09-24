"""Application context.

One place that wires every dependency together, built once per process and
shared by the scheduler, the web app and the CLI entry points. The existing
audit tool re-creates its Vault session, database connection and LLM client
once per ticket; this exists specifically so that does not happen here.

Construction is lazy where a component is optional: the service must still
start and serve the board if Azure OpenAI is unreachable.
"""

import threading
from typing import Any, Dict, List, Optional

from core.config import Settings, load_settings
from core.db import Database, build_audit_database, build_database
from core.logging_setup import get_logger, setup_logging
from core.llm.client import LlmClient
from core.snow.base import ServiceNowClient
from core.snow.incidents import IncidentReader
from core.snow.knowledge import KnowledgeReader
from core.snow.sla import SlaReader
from core.vault_client import VaultClient
from pipeline.advisor import Advisor
from pipeline.retrieval import Retriever, VectorIndex
from pipeline.scoring import load_weights
from pipeline.sync import TicketSync

log = get_logger('core.context')

_context: Optional['AppContext'] = None
_context_lock = threading.Lock()


class AppContext:
    def __init__(self, settings: Settings):
        self.settings = settings

        setup_logging(
            log_dir=settings.resolve_path('runtime.log_dir', 'logs'),
            level=settings.get('runtime.log_level', 'INFO'),
        )

        self.vault = VaultClient(settings)
        self.db: Database = build_database(settings, self.vault)
        self.audit_db: Optional[Database] = build_audit_database(settings, self.vault)

        self.snow = ServiceNowClient(settings, self.vault)
        self.incidents = IncidentReader(self.snow, settings.assignment_groups)
        self.sla = SlaReader(self.snow, settings)
        self.knowledge = KnowledgeReader(self.snow)

        self.sync = TicketSync(self.db, self.incidents, settings)

        self._llm: Optional[LlmClient] = None
        self._llm_failed = False
        self._llm_lock = threading.Lock()

        self.index = VectorIndex(self.db, int(settings.get('llm.embedding_dims', 1536)))

        self._retriever: Optional[Retriever] = None
        self._advisor: Optional[Advisor] = None

        log.info('Application context ready (customer=%s, shadow_mode=%s)',
                 settings.customer_name, settings.shadow_mode)

    # -- lazily built, optional components ---------------------------------

    @property
    def llm(self) -> Optional[LlmClient]:
        """None when Azure OpenAI cannot be reached.

        Callers fall back to deterministic behaviour rather than failing, so a
        model outage degrades the digest instead of cancelling it.
        """
        if self._llm is not None or self._llm_failed:
            return self._llm

        with self._llm_lock:
            if self._llm is None and not self._llm_failed:
                try:
                    self._llm = LlmClient(self.settings, self.vault)
                except Exception:
                    log.exception('LLM unavailable - recommendations will use the rule fallback')
                    self._llm_failed = True
        return self._llm

    @property
    def retriever(self) -> Optional[Retriever]:
        if self._retriever is None and self.llm is not None:
            self._retriever = Retriever(
                db=self.db,
                llm=self.llm,
                index=self.index,
                knowledge_reader=self.knowledge,
                min_similarity=float(self.settings.get('llm.min_similarity', 0.55)),
            )
        return self._retriever

    @property
    def advisor(self) -> Optional[Advisor]:
        if self._advisor is None and self.llm is not None:
            self._advisor = Advisor(
                db=self.db,
                llm=self.llm,
                settings=self.settings,
                valid_groups=self.known_assignment_groups(),
            )
        return self._advisor

    # -- helpers -----------------------------------------------------------

    def known_assignment_groups(self) -> List[str]:
        """Real groups the advisor is allowed to suggest.

        Sourced from groups actually seen in the ticket data plus anything
        configured, so the model cannot invent a destination.
        """
        configured = list(self.settings.assignment_groups)
        try:
            rows = self.db.query(
                'SELECT DISTINCT assignment_group FROM watched_ticket '
                ' WHERE assignment_group IS NOT NULL AND assignment_group != ""')
            observed = [r['assignment_group'] for r in rows]
        except Exception:
            observed = []

        merged: Dict[str, str] = {}
        for group in configured + observed:
            if group and group.strip():
                merged.setdefault(group.strip().lower(), group.strip())
        return sorted(merged.values())

    def thresholds(self) -> Dict[str, Any]:
        return dict(self.settings.get('thresholds', {}) or {})

    def weights(self) -> Dict[str, int]:
        return load_weights(self.db)

    def close(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass
        if self.audit_db:
            try:
                self.audit_db.close()
            except Exception:
                pass


def get_context(config_path: Optional[str] = None) -> AppContext:
    global _context
    with _context_lock:
        if _context is None:
            _context = AppContext(load_settings(config_path))
        return _context


def reset_context() -> None:
    """Test hook."""
    global _context
    with _context_lock:
        if _context is not None:
            _context.close()
        _context = None
