-- ---------------------------------------------------------------------------
-- Migration 002 - store UTC, and record exact minutes
--
-- Two related changes:
--
--   1. ticket_signal gains age_minutes and idle_minutes. age_days is retained
--      because the attention score works in days, but DECIMAL(8,2) cannot
--      resolve finer than about fifteen minutes, so it cannot drive a display
--      that names minutes.
--
--   2. Every timestamp the application writes is now naive UTC, taken from the
--      ServiceNow `value` field rather than `display_value`. The view's snooze
--      comparison moves from NOW() to UTC_TIMESTAMP() to match, because MySQL
--      NOW() returns the session timezone.
--
-- Apply AFTER 001_formalise_naming.sql.
--
--   mysql -u sd_advisor -p sd_advisor_db < db/migrations/002_utc_and_minutes.sql
--   mysql -u sd_advisor -p sd_advisor_db < db/schema.sql     # recreates the view
--
-- Stop the service first. Safe to re-run.
--
-- IMPORTANT - existing rows carry display-clock timestamps
--
-- watched_ticket.opened_at, watched_ticket.sys_updated_on and the ticket_signal
-- history were written from display_value, i.e. on the integration user's
-- timezone. If that user is not on UTC, those values are offset from everything
-- written after this migration, and no in-place correction is reliable because
-- the offset in force when each row was written is not recorded.
--
-- Both tables are fully derived from ServiceNow, so the supported fix is to
-- discard and rebuild rather than attempt a correction:
--
--   TRUNCATE TABLE ticket_signal;
--   DELETE FROM sync_state WHERE name = 'incident_delta';   -- forces a cold start
--   -- then:  python run.py sync --full && python run.py signals
--
-- Run ops/probe_timezone.py first. If it reports the integration user is on
-- UTC, the stored values are already correct and the rebuild is unnecessary.
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS v_current_board;


-- --- 1. Exact minute columns ----------------------------------------------

SET @needs_age := (
    SELECT COUNT(*) = 0 FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME   = 'ticket_signal'
       AND COLUMN_NAME  = 'age_minutes'
);
SET @sql := IF(@needs_age,
    'ALTER TABLE ticket_signal
       ADD COLUMN age_minutes INT NOT NULL DEFAULT 0 AFTER idle_days',
    'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @needs_idle := (
    SELECT COUNT(*) = 0 FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME   = 'ticket_signal'
       AND COLUMN_NAME  = 'idle_minutes'
);
SET @sql := IF(@needs_idle,
    'ALTER TABLE ticket_signal
       ADD COLUMN idle_minutes INT NOT NULL DEFAULT 0 AFTER age_minutes',
    'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;


-- --- 2. Backfill the minute columns from the day columns ------------------
-- Approximate, and only for rows that predate the change. The next signal
-- refresh overwrites both with exact values, so this exists purely so the
-- board does not show "0 Mins" between the migration and that refresh.

UPDATE ticket_signal
   SET age_minutes  = ROUND(age_days  * 1440),
       idle_minutes = ROUND(idle_days * 1440)
 WHERE age_minutes = 0
   AND idle_minutes = 0
   AND (age_days > 0 OR idle_days > 0);


-- --- 3. Recreate the view -------------------------------------------------
-- Re-apply schema.sql to restore v_current_board with the two new columns and
-- the UTC_TIMESTAMP() snooze comparison:
--
--   mysql -u sd_advisor -p sd_advisor_db < db/schema.sql


-- --- Verification ---------------------------------------------------------
--   SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS
--    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ticket_signal'
--      AND COLUMN_NAME IN ('age_minutes', 'idle_minutes');
--
--   -- After one signal refresh, days and minutes should agree to within a
--   -- rounding step. A large divergence means the refresh has not run.
--   SELECT incident_number, age_days, age_minutes,
--          ROUND(age_minutes / 1440.0, 2) AS minutes_as_days
--     FROM ticket_signal ORDER BY id DESC LIMIT 10;
--
--   -- Sanity-check the clock. These should agree if MySQL is on UTC; if they
--   -- differ, that is expected and is exactly why the application no longer
--   -- uses NOW().
--   SELECT NOW() AS mysql_session_now, UTC_TIMESTAMP() AS mysql_utc;
