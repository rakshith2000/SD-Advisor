"""Whether the knowledge base actually drives the advice, and whether we can tell.

Two defects this exists for, both of which looked like working software.

**The model could see that a relevant article existed and not what it said.**
Articles reached the prompt truncated at 700 characters. Knowledge articles
are written symptom-first - scope, affected versions, background, and the
procedure last - so 700 characters reliably delivered the description of the
problem and cut off before the steps. Retrieval was working, citation was
working, and the one thing the article was for never arrived.

**Nothing measured any of it.** Citations were stored inside the `evidence`
JSON blob alongside similar incidents and signal names, so "how often does a
recommendation rest on an article, and is it the right article" could not be
asked in SQL. The accuracy page reported agreement, precision and coverage,
and said nothing at all about the knowledge base.

Both halves are now recorded - offered and cited - because the useful
failures live in the gap between them.

    python -m pytest tests/test_kb_evidence.py -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm.prompts import (KB_BODY_BUDGET, PROMPT_VERSION, SYSTEM_PROMPT,
                              build_user_prompt, kb_excerpt)
from core.llm.schemas import Evidence, EvidenceType, Recommendation
from pipeline.advisor import Advisor

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope='module')
def template():
    return (ROOT / 'web' / 'templates' / 'accuracy.html').read_text(encoding='utf-8')

# A symptom-first article, which is how they are actually written: scope and
# background first, the thing you came for last.
SYMPTOM_FIRST = (
    'Symptom: Users on the VPN report that mapped drives disappear after '
    'roughly ten minutes of inactivity. Affects Windows 11 builds 22H2 and '
    'later on the corporate image. ' + ('Background detail. ' * 90) +
    '\nResolution steps:\n'
    '1. Open Credential Manager.\n'
    '2. Remove the stale entry for fileserver01.\n'
    '3. Reconnect the drive with persistent mapping enabled.\n'
    '4. If it recurs, reassign to Network Operations for a DFS check.'
)


# ---------------------------------------------------------------------------
# getting the steps in front of the model
# ---------------------------------------------------------------------------

class TestTheExcerptReachesTheProcedure:
    def test_a_short_article_passes_through_whole(self):
        body = 'Restart the print spooler service and retry the job.'
        assert kb_excerpt(body) == body

    def test_the_old_window_would_have_lost_the_steps(self):
        """The defect, stated as a fact about the data rather than about the
        code - so this still means something if the implementation changes."""
        assert 'Credential Manager' not in SYMPTOM_FIRST[:700]

    def test_the_steps_now_arrive(self):
        out = kb_excerpt(SYMPTOM_FIRST)
        assert 'Credential Manager' in out
        assert 'persistent mapping' in out

    def test_the_last_step_is_not_clipped(self):
        """Step 4 names the team to reassign to. Losing the tail of a
        procedure loses exactly the part that decides the action."""
        assert 'Network Operations' in kb_excerpt(SYMPTOM_FIRST)

    def test_the_scope_is_kept_alongside_the_steps(self):
        """Steps without scope are how a Windows fix gets applied to a Mac."""
        out = kb_excerpt(SYMPTOM_FIRST)
        assert 'Symptom' in out
        assert 'Windows 11' in out

    def test_it_stays_within_budget(self):
        assert len(kb_excerpt(SYMPTOM_FIRST)) <= KB_BODY_BUDGET

    def test_a_long_article_with_no_procedure_heading_keeps_the_opening(self):
        body = 'Context. ' * 500
        out = kb_excerpt(body)
        assert out.startswith('Context.')
        assert len(out) <= KB_BODY_BUDGET + 4

    @pytest.mark.parametrize('heading', [
        'Resolution:', 'Workaround:', 'Procedure:', 'Troubleshooting:',
        'How to:', 'Remediation:',
    ])
    def test_the_common_headings_are_recognised(self, heading):
        body = ('Preamble. ' * 200) + f'\n{heading}\nDo the specific thing.'
        assert 'Do the specific thing' in kb_excerpt(body)

    def test_a_marker_word_in_prose_does_not_hijack_the_window(self):
        """"no action required" early in a body is not a procedure heading.
        Treating it as one would start the window at the wrong place and
        drop the real steps."""
        body = ('There is no action required during business hours. ' * 40 +
                '\nResolution:\nThe actual fix is here.')
        assert 'The actual fix is here' in kb_excerpt(body)

    @pytest.mark.parametrize('value', [None, '', '   '])
    def test_an_empty_body_is_not_an_error(self, value):
        assert kb_excerpt(value) == ''


# ---------------------------------------------------------------------------
# telling the model what to do with an article
# ---------------------------------------------------------------------------

class TestThePromptConnectsArticlesToActions:
    """Before v2.2 the articles arrived under an "EVIDENCE" heading with no
    rule attached. Whether the content drove the action was left to model
    discretion, which means it was not specified behaviour, which means it
    was not reliable behaviour."""

    def test_the_model_is_told_to_read_the_body(self):
        assert 'not only its title' in SYSTEM_PROMPT

    def test_a_high_similarity_score_is_not_treated_as_applicability(self):
        """Retrieval matches wording. Wording is not fault."""
        assert 'it does not mean the article applies' in SYSTEM_PROMPT

    def test_steps_must_be_carried_into_the_draft(self):
        assert 'draft_work_note' in SYSTEM_PROMPT
        assert 'in the article\'s own order' in SYSTEM_PROMPT

    def test_a_named_team_maps_onto_reassign(self):
        assert 'suggested_target_group' in SYSTEM_PROMPT
        assert 'AVAILABLE GROUPS' in SYSTEM_PROMPT

    def test_a_team_outside_the_allow_list_is_refused_not_invented(self):
        assert 'do not recommend REASSIGN' in SYSTEM_PROMPT

    def test_an_already_attempted_procedure_is_not_repeated(self):
        assert 'already been followed without success' in SYSTEM_PROMPT

    def test_citing_is_reserved_for_articles_that_changed_the_answer(self):
        """Otherwise every recommendation cites all three and the cite rate
        becomes a measure of nothing."""
        assert 'only evidence if you used it' in SYSTEM_PROMPT
        assert 'merely because it was supplied' in SYSTEM_PROMPT

    def test_no_applicable_article_is_a_valid_finding(self):
        """A knowledge gap only gets noticed if saying so is allowed."""
        assert 'No applicable knowledge' in SYSTEM_PROMPT

    def test_the_version_was_bumped_for_this(self):
        """PROMPT_VERSION is inside the input hash. Changing the prompt
        without bumping it files two different prompts under one label and
        corrupts the accuracy figure."""
        assert PROMPT_VERSION >= 'v2.2'

    def test_the_article_body_reaches_the_user_prompt(self):
        prompt = build_user_prompt(
            ticket={'incident_number': 'INC1', 'short_description': 'drives drop'},
            signals={}, timeline=[],
            similar=[],
            kb_articles=[{'number': 'KB0001', 'score': 0.81,
                          'short_description': 'Mapped drives drop on VPN',
                          'body': SYMPTOM_FIRST}],
            available_groups=['Network Operations'])
        assert 'KB0001' in prompt
        assert 'Credential Manager' in prompt

    def test_no_articles_is_said_plainly(self):
        prompt = build_user_prompt(
            ticket={'incident_number': 'INC1'}, signals={}, timeline=[],
            similar=[], kb_articles=[], available_groups=[])
        assert 'no relevant knowledge articles found' in prompt


# ---------------------------------------------------------------------------
# recording what happened
# ---------------------------------------------------------------------------

class FakeDb:
    def __init__(self):
        self.rows = {}

    def upsert(self, table, data, update_columns=None):
        assert table == 'recommendation_kb'
        self.rows[(data['recommendation_id'], data['kb_number'])] = dict(data)
        return 1


def advisor(db):
    obj = Advisor.__new__(Advisor)
    obj.db = db
    return obj


def recommendation(*refs):
    return Recommendation(
        recommended_action='RESOLVE', confidence=0.8,
        pending_action_owner='SERVICE_DESK', rationale='because',
        evidence=[Evidence(type=EvidenceType.KB, ref=r, note=f'used {r}')
                  for r in refs])


def article(number, score=0.8, source='vector'):
    return {'number': number, 'score': score, 'source': source,
            'short_description': 'x', 'body': 'y'}


class TestBothHalvesAreRecorded:
    def test_an_offered_article_is_stored_even_when_unused(self):
        """The gap is the finding. An article offered fifty times and cited
        never means retrieval is matching wording rather than fault, and
        storing only citations hides that completely."""
        db = FakeDb()
        advisor(db)._record_kb({'id': 1}, 'INC1', [article('KB1')],
                               recommendation())
        row = db.rows[(1, 'KB1')]
        assert row['offered'] == 1
        assert row['cited'] == 0

    def test_a_cited_article_is_marked(self):
        db = FakeDb()
        advisor(db)._record_kb({'id': 1}, 'INC1', [article('KB1')],
                               recommendation('KB1'))
        assert db.rows[(1, 'KB1')]['cited'] == 1

    def test_the_models_reason_is_kept(self):
        """So a lead rating the article as wrong can see what it thought it
        was doing."""
        db = FakeDb()
        advisor(db)._record_kb({'id': 1}, 'INC1', [article('KB1')],
                               recommendation('KB1'))
        assert db.rows[(1, 'KB1')]['cite_note'] == 'used KB1'

    def test_mixed_offers_are_recorded_individually(self):
        db = FakeDb()
        advisor(db)._record_kb(
            {'id': 1}, 'INC1',
            [article('KB1'), article('KB2'), article('KB3')],
            recommendation('KB2'))
        assert [db.rows[(1, k)]['cited'] for k in ('KB1', 'KB2', 'KB3')] == [0, 1, 0]

    def test_citation_matching_ignores_case(self):
        db = FakeDb()
        advisor(db)._record_kb({'id': 1}, 'INC1', [article('KB0001')],
                               recommendation('kb0001'))
        assert db.rows[(1, 'KB0001')]['cited'] == 1

    def test_the_retrieval_source_is_kept(self):
        """The keyword fallback only runs when the index is empty or stale,
        so a rise in it is the signal that the nightly build has stopped."""
        db = FakeDb()
        advisor(db)._record_kb({'id': 1}, 'INC1',
                               [article('KB1', source='text_search')],
                               recommendation())
        assert db.rows[(1, 'KB1')]['source'] == 'text_search'

    def test_an_invented_citation_is_recorded_not_dropped(self):
        """The model citing an article it was never given is a fabricated
        number, and the KB accuracy figure is exactly where that should
        surface."""
        db = FakeDb()
        advisor(db)._record_kb({'id': 1}, 'INC1', [article('KB1')],
                               recommendation('KB9999'))
        ghost = db.rows[(1, 'KB9999')]
        assert ghost['offered'] == 0
        assert ghost['cited'] == 1
        assert ghost['source'] == 'not_offered'

    def test_non_kb_evidence_is_ignored(self):
        """Similar incidents and signal names live in the same evidence
        array and are not knowledge articles."""
        db = FakeDb()
        rec = Recommendation(
            recommended_action='RESOLVE', confidence=0.8,
            pending_action_owner='SERVICE_DESK', rationale='x',
            evidence=[Evidence(type=EvidenceType.SIMILAR_INCIDENT, ref='INC77'),
                      Evidence(type=EvidenceType.SIGNAL, ref='sla_breached')])
        advisor(db)._record_kb({'id': 1}, 'INC1', [article('KB1')], rec)
        assert set(db.rows) == {(1, 'KB1')}
        assert db.rows[(1, 'KB1')]['cited'] == 0

    def test_a_duplicate_offer_is_written_once(self):
        """The pipeline re-runs on the same input hash after a restart.
        Without de-duplication a single ticket accumulates a fresh set of
        offers on every pass and inflates every denominator on the
        accuracy page."""
        db = FakeDb()
        advisor(db)._record_kb({'id': 1}, 'INC1',
                               [article('KB1'), article('KB1')],
                               recommendation())
        assert len(db.rows) == 1

    def test_nothing_is_written_without_a_recommendation_id(self):
        db = FakeDb()
        advisor(db)._record_kb({}, 'INC1', [article('KB1')], recommendation())
        assert db.rows == {}

    def test_a_write_failure_does_not_lose_the_recommendation(self):
        """The recommendation is already stored. Losing it because its
        accounting row would not write trades something valuable for
        something less so."""
        class Broken(FakeDb):
            def upsert(self, table, data, update_columns=None):
                raise RuntimeError('recommendation_kb does not exist')

        advisor(Broken())._record_kb({'id': 1}, 'INC1', [article('KB1')],
                                     recommendation('KB1'))


# ---------------------------------------------------------------------------
# the shape the accuracy page depends on
# ---------------------------------------------------------------------------

class TestTheAccuracyPageContract:
    """The page reads kb.* off the metrics dict. Jinja renders a missing key
    as empty rather than raising, so a renamed field shows a blank tile and
    no error."""

    REQUIRED = ('recommendations', 'with_offer', 'with_citation', 'reach_pct',
                'offered', 'cited', 'use_pct', 'rated', 'relevant',
                'irrelevant', 'outdated', 'relevance_pct', 'invented',
                'fallback', 'backed_judged', 'backed_pct', 'unbacked_judged',
                'unbacked_pct', 'top')

    def test_the_metrics_function_builds_every_field(self):
        source = (Path(__file__).resolve().parent.parent / 'web' / 'app.py'
                  ).read_text(encoding='utf-8')
        block = source[source.index("'kb': {"):]
        block = block[:block.index("'total_feedback'")]
        for field in self.REQUIRED:
            assert f"'{field}'" in block, field

    def test_the_page_reads_only_fields_that_exist(self, template):
        import re
        used = set(re.findall(r'\bkb\.([a-z_]+)', template))
        assert sorted(used - set(self.REQUIRED)) == []

    def test_reach_and_relevance_are_reported_separately(self, template):
        """Rolled into one number, high relevance on a tiny reach reads as
        success - and it is a knowledge gap."""
        assert 'kb.reach_pct' in template
        assert 'kb.relevance_pct' in template

    def test_the_backed_comparison_shows_its_sample_size(self, template):
        """A ten-point gap on nine tickets is not a finding."""
        assert 'kb.backed_judged' in template
        assert 'kb.unbacked_judged' in template

    def test_a_thin_sample_is_called_out(self, template):
        assert 'Too few judgements' in template

    def test_fabricated_citations_are_surfaced(self, template):
        assert 'kb.invented' in template

    def test_the_stale_index_warning_is_present(self, template):
        assert 'kb.fallback' in template
