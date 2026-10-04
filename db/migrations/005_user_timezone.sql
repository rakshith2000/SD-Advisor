-- ---------------------------------------------------------------------------
-- Migration 005 - per-user display timezone
--
-- Storage does not change. Every timestamp in this database is, and remains,
-- naive UTC: an elapsed time computed between two values recorded on different
-- clocks is wrong in a way nothing reports, and one clock everywhere is the
-- only defence. This column affects presentation only - what a person sees on
-- the board and in an email, converted at the edge and never written back.
--
-- NULL means "use display.timezone from conf.json", which is the service-wide
-- default. It is not backfilled to that value on purpose: a row holding NULL
-- follows the configured default if it is ever changed, whereas a row holding
-- an explicit copy of today's default would silently stop following it.
--
-- Values are IANA names ('America/Chicago'), never fixed offsets. The zone is
-- CST for part of the year and CDT for the rest; a stored -06:00 would be an
-- hour out for eight months of every year and the abbreviation shown beside
-- each timestamp would be wrong.
--
-- Apply AFTER 004_scope_provenance.sql.
--
--   mysql -u sd_advisor -p sd_advisor_db < db/migrations/005_user_timezone.sql
--
-- Safe to re-run. Creates no views.
-- ---------------------------------------------------------------------------

SET @needed := (SELECT COUNT(*) = 0 FROM information_schema.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'advisor_user'
                   AND COLUMN_NAME = 'timezone');
SET @sql := IF(@needed,
    'ALTER TABLE advisor_user
       ADD COLUMN timezone VARCHAR(64) NULL AFTER assignment_groups',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;


-- --- Verification ---------------------------------------------------------
--   SELECT username, timezone FROM advisor_user ORDER BY id;
--   -- Every existing row should read NULL, meaning "follow the configured
--   -- default". The navigation dropdown writes a value here the moment
--   -- someone picks a different zone.
