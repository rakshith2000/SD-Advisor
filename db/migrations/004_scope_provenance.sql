-- ---------------------------------------------------------------------------
-- Migration 004 - where an account's assignment group scope came from
--
-- On first single sign-on the advisor can derive a new VIEWER's scope from
-- their ServiceNow group membership (sys_user_grmember) rather than leaving it
-- empty for an administrator to fill in by hand.
--
-- That makes it necessary to record how the current scope was arrived at.
-- Without it, a later refresh - or a reconcile - cannot tell the difference
-- between a scope it derived itself and one an administrator deliberately set
-- when approving a Lead request, and would overwrite the second along with the
-- first. A granted scope being silently reverted is the kind of fault nobody
-- reports as a bug; they just quietly lose access to half the board.
--
--   MANUAL      set by an administrator, or by run.py grant
--   REQUEST     granted through an approved role request
--   SERVICENOW  derived from sys_user_grmember at sign-in
--
-- Only SERVICENOW scopes are ever re-derived automatically.
--
-- Apply AFTER 003_sso_and_access_requests.sql.
--
--   mysql -u sd_advisor -p sd_advisor_db < db/migrations/004_scope_provenance.sql
--
-- Safe to re-run. Creates no views.
-- ---------------------------------------------------------------------------

SET @needed := (SELECT COUNT(*) = 0 FROM information_schema.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'advisor_user'
                   AND COLUMN_NAME = 'groups_source');
SET @sql := IF(@needed,
    "ALTER TABLE advisor_user
       ADD COLUMN groups_source ENUM('MANUAL','REQUEST','SERVICENOW')
                  NOT NULL DEFAULT 'MANUAL' AFTER role_source",
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;


SET @needed := (SELECT COUNT(*) = 0 FROM information_schema.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'advisor_user'
                   AND COLUMN_NAME = 'groups_synced_at');
SET @sql := IF(@needed,
    'ALTER TABLE advisor_user ADD COLUMN groups_synced_at DATETIME NULL',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;


-- Every account that exists at this point was scoped by a human - either at
-- creation or through run.py grant - so MANUAL is correct for all of them and
-- the default handles it. Nothing is reclassified: marking an existing scope
-- as SERVICENOW would make it eligible for automatic overwrite, which is the
-- opposite of what this migration is for.


-- --- Verification ---------------------------------------------------------
--   SELECT username, role, role_source, groups_source, assignment_groups
--     FROM advisor_user ORDER BY id;
--   -- Every pre-existing row should read MANUAL / MANUAL.
