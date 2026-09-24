"""Pipeline orchestration.

Two passes, deliberately separated:

  refresh_signals()  - cheap, deterministic, no LLM. Runs on the stream tier
                       every few minutes. This is what makes stagnation and
                       SLA alerts near-real-time.

  refresh_recommendations() - the LLM pass. Runs hourly and before each
                       digest, fanned out across a thread pool and gated by
                       the input-hash cache so unchanged tickets cost nothing.

Per-ticket failures are isolated: one bad ticket never takes down the run,
which is the one pattern from the existing tool worth copying verbatim.
"""

import datetime
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from core.logging_setup import get_logger
from pipeline.retrieval import load_baselines, scope_key
from pipeline.scoring import compute_attention_score
from pipeline.signals import extract_signals

log = get_logger('pipeline.orchestrator')


class Orchestrator:
    def __init__(self, context):
        self.ctx = context
        self.db = context.db
        self.settings = context.settings

    # -----------------------------------------------------------------
    # Pass 1 - deterministic signals
    # -----------------------------------------------------------------

    def refresh_signals(self, tickets: Optional[List[Dict[str, Any]]] = None,
                        max_workers: int = 6) -> Dict[str, Any]:
        """Recompute signals and attention scores for the aged backlog."""
        started = datetime.datetime.now()
        tickets = tickets if tickets is not None else self.ctx.sync.aged_tickets_needing_attention()

        if not tickets:
            log.info('No aged tickets to score')
            return {'scored': 0, 'failed': 0, 'duration_s': 0.0}

        weights = self.ctx.weights()
        thresholds = self.ctx.thresholds()
        baselines = load_baselines(self.db)
        system_accounts = self.settings.system_accounts

        scored, failed = 0, 0

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self._signal_one, ticket, weights, thresholds,
                            baselines, system_accounts): ticket
                for ticket in tickets
            }
            for future in as_completed(futures):
                ticket = futures[future]
                try:
                    future.result()
                    scored += 1
                except Exception:
                    failed += 1
                    log.exception('Signal extraction failed for %s',
                                  ticket.get('incident_number'))

        duration = (datetime.datetime.now() - started).total_seconds()
        log.info('Signals refreshed: %d scored, %d failed (%.1fs)', scored, failed, duration)
        return {'scored': scored, 'failed': failed, 'duration_s': round(duration, 1)}

    def _signal_one(self, ticket: Dict[str, Any], weights, thresholds,
                    baselines, system_accounts) -> Dict[str, Any]:
        number = ticket['incident_number']

        history = self.ctx.incidents.get_history(ticket['sys_id'])
        sla_summary = self.ctx.sla.summarise(self.ctx.sla.get_for_task(number))

        change_record = self._dependency_record('change', ticket.get('rfc'))
        problem_record = self._dependency_record('problem', ticket.get('problem_id'))

        attached_kb = self.ctx.incidents.get_attached_kb(ticket['sys_id'])
        baseline = baselines.get(scope_key(ticket.get('category', ''),
                                           ticket.get('subcategory', '')))

        signals = extract_signals(
            ticket=ticket,
            history=history,
            sla_summary=sla_summary,
            change_record=change_record,
            problem_record=problem_record,
            attached_kb=attached_kb,
            kb_available=self._kb_exists_for(ticket),
            baseline=baseline,
            system_accounts=system_accounts,
            thresholds=thresholds,
        )

        scoring = compute_attention_score(ticket, signals, weights, thresholds)
        signals['attention_score'] = scoring['score']

        self._store_signals(number, signals, scoring)
        return signals

    def _dependency_record(self, kind: str, number: Optional[str]) -> Optional[Dict[str, Any]]:
        if not number or not str(number).strip():
            return None
        try:
            if kind == 'change':
                return self.ctx.incidents.get_change_state(str(number).strip())
            return self.ctx.incidents.get_problem_state(str(number).strip())
        except Exception:
            log.debug('Could not read %s record %s', kind, number)
            return None

    def _kb_exists_for(self, ticket: Dict[str, Any]) -> bool:
        """Whether *any* KB article plausibly covers this ticket.

        Drives the KB_NOT_ATTACHED coaching flag. Uses the vector index when
        it is populated, so this costs one embedding at most and nothing when
        the index is empty.
        """
        retriever = self.ctx.retriever
        if retriever is None:
            return False
        try:
            _, articles = retriever.evidence_for(ticket, top_k=1)
            return bool(articles)
        except Exception:
            return False

    def _store_signals(self, number: str, signals: Dict[str, Any],
                       scoring: Dict[str, Any]) -> None:
        row = {
            'incident_number': number,
            'computed_at': datetime.datetime.now().replace(microsecond=0),
            'age_days': signals.get('age_days') or 0,
            'idle_days': signals.get('idle_days') or 0,
            'days_in_state': signals.get('days_in_state') or 0,
            'last_agent_action_at': signals.get('last_agent_action_at'),
            'last_caller_activity_at': signals.get('last_caller_activity_at'),
            'ball_in_court': signals.get('ball_in_court') or 'AGENT',
            'caller_replied_unanswered': int(bool(signals.get('caller_replied_unanswered'))),
            'dependency_ref': signals.get('dependency_ref'),
            'dependency_state': signals.get('dependency_state'),
            'dependency_resolved': int(bool(signals.get('dependency_resolved'))),
            'followup_count': int(signals.get('followup_count') or 0),
            'days_since_last_followup': signals.get('days_since_last_followup'),
            'auto_close_candidate': int(bool(signals.get('auto_close_candidate'))),
            'sla_breached': int(bool(signals.get('sla_breached'))),
            'sla_pct_consumed': signals.get('sla_pct_consumed'),
            'sla_time_left_mins': signals.get('sla_time_left_mins'),
            'projected_breach_at': signals.get('projected_breach_at'),
            'expected_resolution_hours': signals.get('expected_resolution_hours'),
            'p90_overrun': int(bool(signals.get('p90_overrun'))),
            'kb_available': int(bool(signals.get('kb_available'))),
            'kb_attached': int(bool(signals.get('kb_attached'))),
            'attention_score': scoring['score'],
            'score_breakdown': json.dumps(scoring['breakdown']),
            'risk_flags': json.dumps(signals.get('risk_flags') or []),
        }
        self.db.upsert('ticket_signal', row, update_columns=[
            c for c in row if c not in ('incident_number', 'computed_at')])

    # -----------------------------------------------------------------
    # Pass 2 - LLM recommendations
    # -----------------------------------------------------------------

    def refresh_recommendations(self, tickets: Optional[List[Dict[str, Any]]] = None,
                                limit: Optional[int] = None,
                                force: bool = False) -> Dict[str, Any]:
        started = datetime.datetime.now()

        rows = tickets if tickets is not None else self._tickets_with_signals()
        if limit:
            rows = rows[:limit]

        if not rows:
            return {'analysed': 0, 'cached': 0, 'failed': 0, 'fallback': 0, 'duration_s': 0.0}

        advisor = self.ctx.advisor
        if advisor is None:
            log.warning('No LLM available - writing rule-based fallbacks for %d tickets', len(rows))
            return self._fallback_all(rows, started)

        workers = int(self.settings.get('llm.max_concurrency', 8))
        analysed = cached = failed = 0

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._recommend_one, row, advisor, force): row
                       for row in rows}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    _, from_cache = future.result()
                    if from_cache:
                        cached += 1
                    else:
                        analysed += 1
                except Exception:
                    failed += 1
                    log.exception('Recommendation failed for %s', row.get('incident_number'))

        duration = (datetime.datetime.now() - started).total_seconds()
        usage = getattr(self.ctx.llm, 'usage', {})
        log.info('Recommendations: %d new, %d cached, %d failed (%.1fs) tokens=%s',
                 analysed, cached, failed, duration, usage.get('prompt_tokens'))

        return {'analysed': analysed, 'cached': cached, 'failed': failed,
                'fallback': 0, 'duration_s': round(duration, 1)}

    def _recommend_one(self, ticket: Dict[str, Any], advisor, force: bool):
        signals = self._load_signals(ticket['incident_number'])
        if signals is None:
            # No signals yet - compute them inline rather than skipping.
            signals = self._signal_one(
                ticket, self.ctx.weights(), self.ctx.thresholds(),
                load_baselines(self.db), self.settings.system_accounts)

        retriever = self.ctx.retriever
        similar, kb_articles = ([], [])
        if retriever is not None:
            try:
                similar, kb_articles = retriever.evidence_for(ticket, top_k=3)
            except Exception:
                log.debug('Retrieval failed for %s; proceeding without evidence',
                          ticket['incident_number'])

        return advisor.recommend(ticket, signals, similar, kb_articles, force=force)

    def _fallback_all(self, rows: List[Dict[str, Any]],
                      started: datetime.datetime) -> Dict[str, Any]:
        from pipeline.advisor import Advisor

        stub = Advisor(self.db, None, self.settings, self.ctx.known_assignment_groups())
        written = 0

        for ticket in rows:
            try:
                signals = self._load_signals(ticket['incident_number']) or {}
                record = stub.deterministic_fallback(ticket, signals)
                record['input_hash'] = 'fallback-' + datetime.date.today().isoformat()
                record['prompt_version'] = 'fallback'
                record['superseded'] = 0
                self.db.upsert('recommendation', record, update_columns=[
                    'created_at', 'recommended_action', 'confidence', 'rationale',
                    'risk_flags', 'evidence', 'surfaced', 'model', 'superseded'])
                written += 1
            except Exception:
                log.exception('Fallback failed for %s', ticket.get('incident_number'))

        duration = (datetime.datetime.now() - started).total_seconds()
        return {'analysed': 0, 'cached': 0, 'failed': 0,
                'fallback': written, 'duration_s': round(duration, 1)}

    # -----------------------------------------------------------------
    # shared reads
    # -----------------------------------------------------------------

    def _tickets_with_signals(self) -> List[Dict[str, Any]]:
        return self.ctx.sync.aged_tickets_needing_attention()

    def _load_signals(self, number: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one(
            'SELECT * FROM ticket_signal WHERE incident_number = %s '
            ' ORDER BY id DESC LIMIT 1', (number,))
        if not row:
            return None

        signals = dict(row)
        raw_flags = signals.get('risk_flags')
        if isinstance(raw_flags, (str, bytes)):
            try:
                signals['risk_flags'] = json.loads(raw_flags)
            except (ValueError, TypeError):
                signals['risk_flags'] = []

        # The stored row has no timeline; rebuild it only when a recommendation
        # is actually going to be generated.
        ticket = self.db.query_one(
            'SELECT sys_id, caller_name FROM watched_ticket WHERE incident_number = %s',
            (number,))
        if ticket:
            from pipeline.signals import build_timeline
            try:
                history = self.ctx.incidents.get_history(ticket['sys_id'])
                signals['_timeline'] = build_timeline(
                    history, ticket.get('caller_name') or '', self.settings.system_accounts)
            except Exception:
                signals['_timeline'] = []
        else:
            signals['_timeline'] = []

        return signals

    # -----------------------------------------------------------------
    # Stream-tier alerting
    # -----------------------------------------------------------------

    def detect_new_alerts(self) -> List[Dict[str, Any]]:
        """Risk events that have not already been alerted on today.

        Deduped via alert_log so a ticket that stays breached does not
        re-alert every ten minutes.
        """
        alertable = {'SLA_BREACHED', 'SLA_JEOPARDY', 'CALLER_AWAITING_REPLY',
                     'DEPENDENCY_CLEARED', 'CRITICALLY_STALE'}

        rows = self.db.query("""
            SELECT incident_number, assignment_group, assigned_to, short_description,
                   attention_score, risk_flags, projected_breach_at, ball_in_court
              FROM v_current_board
             WHERE snoozed = 0
             ORDER BY attention_score DESC
        """)

        today = datetime.date.today()
        fired: List[Dict[str, Any]] = []

        for row in rows:
            flags = row.get('risk_flags')
            if isinstance(flags, (str, bytes)):
                try:
                    flags = json.loads(flags)
                except (ValueError, TypeError):
                    flags = []

            for flag in (flags or []):
                if flag not in alertable:
                    continue

                inserted = self.db.insert('alert_log', {
                    'incident_number': row['incident_number'],
                    'alert_type': flag,
                    'fired_on': today,
                    'fired_at': datetime.datetime.now(),
                    'channel': 'email',
                    'payload': json.dumps({
                        'attention_score': row.get('attention_score'),
                        'ball_in_court': row.get('ball_in_court'),
                    }, default=str),
                }, ignore=True)

                if inserted:
                    fired.append({**row, 'alert_type': flag})

        if fired:
            log.info('%d new risk alerts detected', len(fired))
        return fired
