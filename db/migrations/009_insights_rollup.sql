-- 009: a daily rollup, so the insight pages are not a table scan.
--
-- ticket_signal is written every stream tick - every ten minutes, for every
-- tracked ticket - and kept for 180 days. On a desk carrying three hundred
-- aged tickets that is roughly eight million rows. It is the right shape for
-- what it is: an append-only record of the computed state, which is the only
-- reason a trend is possible at all.
--
-- It is the wrong thing to put behind a web page. "How many aged tickets
-- were there each day for the last ninety days" over the raw table is a
-- range scan of four million rows, per panel, per page load. The page would
-- work, take several seconds, and people would stop opening it - the same
-- outcome as not building it, reached more expensively.
--
-- So: one row per queue per day, written by the nightly job from the last
-- signal each ticket had that day. Trend panels read this; anything about
-- right now reads v_current_board, which is already cheap.
--
-- Why the LAST signal of the day rather than an average: these panels answer
-- "what did the backlog look like", and a backlog is a state, not a rate.
-- Averaging a ticket's score across the day would smear the moment it
-- crossed a threshold into a fraction of a ticket, and the thresholds are
-- the entire story.
--
-- Idempotent: safe to run more than once.

CREATE TABLE IF NOT EXISTS signal_daily (
    day                   DATE          NOT NULL,
    assignment_group      VARCHAR(150)  NOT NULL,

    tickets               INT           NOT NULL DEFAULT 0,
    team_owned            INT           NOT NULL DEFAULT 0,
    sla_breached          INT           NOT NULL DEFAULT 0,
    sla_at_risk           INT           NOT NULL DEFAULT 0,
    prolonged_inactivity  INT           NOT NULL DEFAULT 0,
    caller_awaiting       INT           NOT NULL DEFAULT 0,
    closure_candidates    INT           NOT NULL DEFAULT 0,
    dependency_cleared    INT           NOT NULL DEFAULT 0,
    unassigned            INT           NOT NULL DEFAULT 0,
    kb_gap                INT           NOT NULL DEFAULT 0,

    -- Ageing bands. Ordered magnitude, which is why the charts render them
    -- as one hue light to dark rather than four unrelated colours.
    band_5_10             INT           NOT NULL DEFAULT 0,
    band_10_20            INT           NOT NULL DEFAULT 0,
    band_20_30            INT           NOT NULL DEFAULT 0,
    band_30_plus          INT           NOT NULL DEFAULT 0,

    -- Median and p90 together. The median says what a typical aged ticket
    -- looks like; p90 says how bad the bad ones are. They move
    -- independently, and a flat median over a rising p90 is the commonest
    -- way a backlog goes wrong without the headline changing.
    median_age_days       DECIMAL(8,2)  NULL,
    p90_age_days          DECIMAL(8,2)  NULL,
    median_idle_days      DECIMAL(8,2)  NULL,
    avg_score             DECIMAL(6,2)  NULL,
    max_score             INT           NULL,

    -- Flow. Derived from first and last appearance in ticket_signal, so
    -- "retired" covers resolved and reassigned-away alike. Both are
    -- departures from this board, and to a lead they are the same event.
    newly_aged            INT           NOT NULL DEFAULT 0,
    retired               INT           NOT NULL DEFAULT 0,

    computed_at           DATETIME      NOT NULL,

    PRIMARY KEY (day, assignment_group),
    KEY ix_signal_daily_day (day)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Deliberately not pruned alongside ticket_signal. The raw signals age out
-- at 180 days; these rows are a few hundred bytes per queue per day and are
-- the only thing that will still answer "what did last year look like" when
-- somebody asks at the annual review.
