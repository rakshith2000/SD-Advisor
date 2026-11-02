-- 008: make the knowledge base measurable.
--
-- Knowledge articles were already retrieved per ticket and put in front of
-- the model, and the citations the model returned were stored - inside the
-- `evidence` JSON column on `recommendation`, mixed in with similar
-- incidents, SLA references and signal names.
--
-- That is enough to display and not enough to measure. Answering "how often
-- does a recommendation rest on a knowledge article, and when it does, is it
-- the right article" meant unpacking a JSON blob per row in application code.
-- So nobody asked, and the one part of the pipeline with no accuracy figure
-- against it was the part the customer most wanted a figure for.
--
-- This table records both halves of the question:
--
--   offered  the article was retrieved and shown to the model
--   cited    the model said this article changed its answer
--
-- Both, because the interesting failures live in the gap. Offered-but-never-
-- cited across a whole category means retrieval is pulling the wrong
-- articles. Cited-and-rated-irrelevant means the model is reaching for
-- anything with matching words. Neither is visible if only citations are
-- stored.
--
-- Idempotent: safe to run more than once.

CREATE TABLE IF NOT EXISTS recommendation_kb (
    id                 BIGINT        NOT NULL AUTO_INCREMENT,
    recommendation_id  BIGINT        NOT NULL,
    incident_number    VARCHAR(50)   NOT NULL,
    kb_number          VARCHAR(50)   NOT NULL,

    -- How the article was found. A vector hit and a keyword fallback are not
    -- the same quality of evidence, and the fallback only runs when the
    -- index is empty or stale - so a sudden rise in 'text_search' is itself
    -- the signal that the nightly index build has stopped working.
    source             VARCHAR(20)   NOT NULL DEFAULT 'vector',
    similarity         DECIMAL(4,3)  NULL,

    offered            TINYINT(1)    NOT NULL DEFAULT 1,
    cited              TINYINT(1)    NOT NULL DEFAULT 0,

    -- Why the model said it used the article. Its own words, kept so a lead
    -- rating the article as wrong can see what it thought it was doing.
    cite_note          TEXT          NULL,

    -- The lead's verdict on the article itself, which is a different
    -- question from whether they agreed with the recommended action. An
    -- article can be the right one behind a judgement call the lead would
    -- have made differently, and a plausible wrong article can sit behind an
    -- action that happened to be correct. Rolling the two together would
    -- make both numbers unreadable.
    verdict            ENUM('UNRATED','RELEVANT','IRRELEVANT','OUTDATED')
                                     NOT NULL DEFAULT 'UNRATED',
    rated_by           VARCHAR(100)  NULL,
    rated_at           DATETIME      NULL,

    created_at         DATETIME      NOT NULL,

    PRIMARY KEY (id),
    -- One row per article per recommendation. The pipeline re-runs on the
    -- same input hash after a restart, and without this a single ticket
    -- would accumulate a duplicate set of offers on every pass and inflate
    -- every denominator on the accuracy page.
    UNIQUE KEY uq_rec_kb (recommendation_id, kb_number),
    KEY ix_rec_kb_incident (incident_number, created_at),
    KEY ix_rec_kb_article (kb_number, cited),
    KEY ix_rec_kb_verdict (verdict, rated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
