-- ---------------------------------------------------------------------------
-- Migration 001 - formalise the system vocabulary
--
-- Applies the terminology change to an existing sd_advisor_db:
--
--   ticket_signal.ball_in_court   -> ticket_signal.pending_action_owner
--   value 'AGENT'                 -> 'SERVICE_DESK'
--   seven attention_weightage component names -> formal equivalents
--   risk-flag tokens in two JSON columns      -> formal equivalents
--
-- Enum values in recommendation.recommended_action are deliberately NOT
-- changed; only their presentation labels were formalised, so stored rows and
-- the language-model contract remain valid.
--
-- Run once:
--   mysql -u sd_advisor -p sd_advisor_db < db/migrations/001_formalise_naming.sql
--
-- Safe to re-run. Every statement is guarded, so a partially applied migration
-- can be completed by running the file again.
--
-- Stop the service first. The running process holds the old column names in
-- compiled SQL and will error between the ALTER and the restart.
-- ---------------------------------------------------------------------------

-- The view references the column being renamed, so it must go first and be
-- recreated from schema.sql afterwards.
DROP VIEW IF EXISTS v_current_board;


-- --- 1. Column rename ------------------------------------------------------
-- RENAME COLUMN needs MySQL 8.0+. CHANGE is used instead so this also applies
-- on 5.7, and the IF check keeps it idempotent.

SET @needs_rename := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME   = 'ticket_signal'
       AND COLUMN_NAME  = 'ball_in_court'
);

SET @sql := IF(@needs_rename > 0,
    'ALTER TABLE ticket_signal
       CHANGE COLUMN ball_in_court pending_action_owner
       VARCHAR(20) NOT NULL DEFAULT ''SERVICE_DESK''',
    'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Reset the default even when the column was already renamed by a prior run.
ALTER TABLE ticket_signal
  ALTER COLUMN pending_action_owner SET DEFAULT 'SERVICE_DESK';


-- --- 1b. p90_overrun -> duration_overrun ----------------------------------
-- The signal field adopts the same formal name as the score component it
-- feeds. resolution_stat.p90_hours is deliberately left alone: the 90th
-- percentile is a named statistical measure, not informal shorthand.

SET @needs_rename := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME   = 'ticket_signal'
       AND COLUMN_NAME  = 'p90_overrun'
);

SET @sql := IF(@needs_rename > 0,
    'ALTER TABLE ticket_signal
       CHANGE COLUMN p90_overrun duration_overrun
       TINYINT(1) NOT NULL DEFAULT 0',
    'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;


-- --- 2. Value rename ------------------------------------------------------

UPDATE ticket_signal
   SET pending_action_owner = 'SERVICE_DESK'
 WHERE pending_action_owner = 'AGENT';


-- --- 3. Attention-score component names -----------------------------------
-- Weights carry over unchanged; only the identifiers move. Done as explicit
-- statements rather than a loop so the mapping is auditable.

UPDATE attention_weightage SET component = 'sla_risk'              WHERE component = 'sla_jeopardy';
UPDATE attention_weightage SET component = 'inactivity_duration'   WHERE component = 'stagnation';
UPDATE attention_weightage SET component = 'unactioned_delay'      WHERE component = 'blocked_stale';
UPDATE attention_weightage SET component = 'reassignment_activity' WHERE component = 'churn';
UPDATE attention_weightage SET component = 'duration_overrun'      WHERE component = 'p90_overrun';
UPDATE attention_weightage SET component = 'ticket_age'            WHERE component = 'age';
UPDATE attention_weightage SET component = 'business_priority'     WHERE component = 'priority';

UPDATE attention_weightage SET description = 'Proximity of the resolution SLA to breach'
 WHERE component = 'sla_risk';
UPDATE attention_weightage SET description = 'Elapsed time since the last substantive Service Desk action'
 WHERE component = 'inactivity_duration';
UPDATE attention_weightage SET description = 'Caller response unanswered, or dependency already closed'
 WHERE component = 'unactioned_delay';
UPDATE attention_weightage SET description = 'Age beyond the aged-after threshold'
 WHERE component = 'ticket_age';
UPDATE attention_weightage SET description = 'Business priority and impact of the incident'
 WHERE component = 'business_priority';
UPDATE attention_weightage SET description = 'Beyond the 90th-percentile resolution time for comparable incidents'
 WHERE component = 'duration_overrun';
UPDATE attention_weightage SET description = 'Reassignment and reopen frequency'
 WHERE component = 'reassignment_activity';


-- --- 4. Risk-flag tokens in stored JSON -----------------------------------
-- risk_flags is a JSON array of strings in both tables. REPLACE over the text
-- form is used because JSON_REPLACE cannot rewrite array elements by value.
--
-- Each pattern includes its surrounding double quotes. That is what keeps
-- 'STALE' from matching inside 'CRITICALLY_STALE' - the character preceding
-- STALE there is an underscore, not a quote. Without the quotes this
-- substitution would silently downgrade every critical row. CRITICALLY_STALE
-- is still handled first as a second line of defence.

UPDATE ticket_signal
   SET risk_flags = CAST(
         REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
           CAST(risk_flags AS CHAR),
           '"CRITICALLY_STALE"',       '"PROLONGED_INACTIVITY"'),
           '"STALE"',                  '"NO_RECENT_ACTIVITY"'),
           '"SLA_JEOPARDY"',           '"SLA_AT_RISK"'),
           '"NEVER_TOUCHED"',          '"NO_ACTION_RECORDED"'),
           '"AUTO_CLOSE_CANDIDATE"',   '"CLOSURE_CANDIDATE"'),
           '"KB_NOT_ATTACHED"',        '"KB_ARTICLE_NOT_LINKED"'),
           '"PAST_EXPECTED_DURATION"', '"EXPECTED_DURATION_EXCEEDED"')
         AS JSON)
 WHERE risk_flags IS NOT NULL
   AND CAST(risk_flags AS CHAR) REGEXP
       'CRITICALLY_STALE|STALE|SLA_JEOPARDY|NEVER_TOUCHED|AUTO_CLOSE_CANDIDATE|KB_NOT_ATTACHED|PAST_EXPECTED_DURATION';

UPDATE recommendation
   SET risk_flags = CAST(
         REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
           CAST(risk_flags AS CHAR),
           '"CRITICALLY_STALE"',       '"PROLONGED_INACTIVITY"'),
           '"STALE"',                  '"NO_RECENT_ACTIVITY"'),
           '"SLA_JEOPARDY"',           '"SLA_AT_RISK"'),
           '"NEVER_TOUCHED"',          '"NO_ACTION_RECORDED"'),
           '"AUTO_CLOSE_CANDIDATE"',   '"CLOSURE_CANDIDATE"'),
           '"KB_NOT_ATTACHED"',        '"KB_ARTICLE_NOT_LINKED"'),
           '"PAST_EXPECTED_DURATION"', '"EXPECTED_DURATION_EXCEEDED"'),
           '"UNKNOWN_TARGET_GROUP"',   '"UNVERIFIED_TARGET_GROUP"')
         AS JSON)
 WHERE risk_flags IS NOT NULL
   AND CAST(risk_flags AS CHAR) REGEXP
       'CRITICALLY_STALE|STALE|SLA_JEOPARDY|NEVER_TOUCHED|AUTO_CLOSE_CANDIDATE|KB_NOT_ATTACHED|PAST_EXPECTED_DURATION|UNKNOWN_TARGET_GROUP';


-- --- 5. score_breakdown component keys ------------------------------------
-- Stored for audit only; nothing reads the old keys. Rewritten so historical
-- rows stay readable alongside new ones.

UPDATE ticket_signal
   SET score_breakdown = CAST(
         REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
           CAST(score_breakdown AS CHAR),
           '"sla_jeopardy"',  '"sla_risk"'),
           '"stagnation"',    '"inactivity_duration"'),
           '"blocked_stale"', '"unactioned_delay"'),
           '"churn"',         '"reassignment_activity"'),
           '"p90_overrun"',   '"duration_overrun"'),
           '"age"',           '"ticket_age"'),
           '"priority"',      '"business_priority"')
         AS JSON)
 WHERE score_breakdown IS NOT NULL
   AND CAST(score_breakdown AS CHAR) REGEXP
       '"(sla_jeopardy|stagnation|blocked_stale|churn|p90_overrun|age|priority)"';


-- --- 6. Invalidate the recommendation cache -------------------------------
-- The prompt now names pending_action_owner and the formal flag tokens, so
-- every stored input_hash was computed against a different prompt. Marking
-- them superseded forces one clean re-analysis rather than serving answers
-- that cite a vocabulary no longer in use.
--
-- Expect the next `run.py recommend` pass to call the model for the whole
-- backlog once. This is the intended one-off cost of the rename.

UPDATE recommendation SET superseded = 1 WHERE superseded = 0;


-- --- 7. Recreate the view -------------------------------------------------
-- Re-apply schema.sql afterwards to restore v_current_board with the new
-- column name:
--
--   mysql -u sd_advisor -p sd_advisor_db < db/schema.sql
--
-- schema.sql is idempotent (CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE
-- VIEW), so re-applying it is the supported way to finish this migration.


-- --- Verification ---------------------------------------------------------
-- Expected: pending_action_owner present, ball_in_court absent, seven formal
-- component names summing to 100, and no legacy flag tokens remaining.
--
--   SELECT COLUMN_NAME FROM information_schema.COLUMNS
--    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ticket_signal'
--      AND COLUMN_NAME IN ('ball_in_court', 'pending_action_owner');
--
--   SELECT component, weightage FROM attention_weightage ORDER BY weightage DESC;
--   SELECT SUM(weightage) AS must_be_100 FROM attention_weightage WHERE enabled = 1;
--
--   SELECT COUNT(*) AS must_be_0 FROM ticket_signal
--    WHERE CAST(risk_flags AS CHAR) REGEXP 'CRITICALLY_STALE|SLA_JEOPARDY|NEVER_TOUCHED';
--
--   SELECT COUNT(*) AS must_be_0 FROM ticket_signal
--    WHERE pending_action_owner = 'AGENT';
