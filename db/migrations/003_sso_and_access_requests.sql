-- ---------------------------------------------------------------------------
-- Migration 003 - SSO identity columns and the role request workflow
--
-- Keycloak becomes the authentication provider. It does NOT become the
-- authorisation provider: ADMIN / LEAD / VIEWER stay in advisor_user.role,
-- and Keycloak is never told these roles exist. That choice is what keeps the
-- service account read-only (view-users) on a realm shared with ATK, rather
-- than needing manage-users, which would let a credential on this host rewrite
-- another application's users.
--
-- Two groups of change:
--
--   1. advisor_user gains an external identity key and provenance columns.
--      password_hash becomes nullable, because an SSO account has no password.
--
--   2. role_request records the self-service elevation workflow: a VIEWER asks
--      for LEAD, every administrator is emailed, one of them decides.
--
-- Apply AFTER 002_utc_and_minutes.sql.
--
--   mysql -u sd_advisor -p sd_advisor_db < db/migrations/003_sso_and_access_requests.sql
--
-- Safe to re-run. Creates no views, so it does not need the CREATE VIEW grant
-- that 001 and 002 assume and the sd_advisor account does not hold.
-- ---------------------------------------------------------------------------


-- --- 1. advisor_user ------------------------------------------------------

-- password_hash must accept NULL before any SSO account can be written.
-- Unconditional: re-running it is a no-op.
ALTER TABLE advisor_user MODIFY password_hash VARCHAR(255) NULL;


-- The Entra object ID. This, not username, is the real identity key: a
-- username can change (a name change, or the unresolved kohler.com vs
-- kohlerco.com question), and a JIT upsert keyed on username would then create
-- a second account and orphan that person's recommendation_feedback history.
SET @needed := (SELECT COUNT(*) = 0 FROM information_schema.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'advisor_user'
                   AND COLUMN_NAME = 'external_id');
SET @sql := IF(@needed,
    'ALTER TABLE advisor_user ADD COLUMN external_id VARCHAR(100) NULL AFTER username',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;


-- LOCAL accounts survive an OIDC cutover as break-glass; the reconcile job
-- must be able to tell them apart from SSO accounts so it never deactivates
-- the one credential that still works when Keycloak is down.
SET @needed := (SELECT COUNT(*) = 0 FROM information_schema.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'advisor_user'
                   AND COLUMN_NAME = 'auth_source');
SET @sql := IF(@needed,
    "ALTER TABLE advisor_user
       ADD COLUMN auth_source ENUM('LOCAL','OIDC') NOT NULL DEFAULT 'LOCAL' AFTER role",
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;


-- How the current role was arrived at. Answers "why does this person have
-- LEAD?" without reading the log, and lets a manually pinned role resist
-- anything automated.
SET @needed := (SELECT COUNT(*) = 0 FROM information_schema.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'advisor_user'
                   AND COLUMN_NAME = 'role_source');
SET @sql := IF(@needed,
    "ALTER TABLE advisor_user
       ADD COLUMN role_source ENUM('MANUAL','REQUEST','RECONCILE')
                  NOT NULL DEFAULT 'MANUAL' AFTER auth_source",
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;


SET @needed := (SELECT COUNT(*) = 0 FROM information_schema.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'advisor_user'
                   AND COLUMN_NAME = 'role_changed_at');
SET @sql := IF(@needed,
    'ALTER TABLE advisor_user ADD COLUMN role_changed_at DATETIME NULL',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;


-- Last time Keycloak confirmed this account still exists and is enabled.
-- The reconcile job's working column.
SET @needed := (SELECT COUNT(*) = 0 FROM information_schema.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'advisor_user'
                   AND COLUMN_NAME = 'last_seen_idp_at');
SET @sql := IF(@needed,
    'ALTER TABLE advisor_user ADD COLUMN last_seen_idp_at DATETIME NULL',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;


-- UNIQUE rather than a plain index: two rows sharing an Entra object ID is
-- the duplicate-account bug this column exists to prevent, so the database
-- should refuse it rather than leave it to the upsert to get right.
-- NULLs do not collide in MySQL, so existing LOCAL accounts are unaffected.
SET @needed := (SELECT COUNT(*) = 0 FROM information_schema.STATISTICS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'advisor_user'
                   AND INDEX_NAME = 'uq_external');
SET @sql := IF(@needed,
    'ALTER TABLE advisor_user ADD UNIQUE KEY uq_external (external_id)',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;


-- --- 2. role_request ------------------------------------------------------

CREATE TABLE IF NOT EXISTS role_request (
    id               BIGINT       NOT NULL AUTO_INCREMENT,

    -- Denormalised deliberately. The approval email and the decision page are
    -- read long after the fact, and must show who asked and what they asked
    -- for as it stood at the time, not as the user record reads today.
    username         VARCHAR(100) NOT NULL,
    external_id      VARCHAR(100) NULL,
    full_name        VARCHAR(150) NULL,
    email            VARCHAR(200) NULL,

    from_role        ENUM('ADMIN','LEAD','VIEWER') NOT NULL,
    to_role          ENUM('ADMIN','LEAD','VIEWER') NOT NULL,
    justification    TEXT         NOT NULL,

    -- requested_groups is what the user asked for; granted_groups is what the
    -- administrator actually allowed, which may be narrower. Keeping both is
    -- what makes the approval an auditable decision rather than a rubber stamp.
    requested_groups TEXT         NULL,
    granted_groups   TEXT         NULL,

    status           ENUM('PENDING','APPROVED','REJECTED','EXPIRED','CANCELLED')
                                  NOT NULL DEFAULT 'PENDING',
    decided_by       VARCHAR(100) NULL,
    decided_at       DATETIME     NULL,
    decision_note    TEXT         NULL,
    notified_at      DATETIME     NULL,
    expires_at       DATETIME     NOT NULL,
    created_at       DATETIME     NOT NULL,

    -- One open request per user per target role, enforced by the schema.
    --
    -- The column is NULL for every row that is not PENDING, and MySQL does not
    -- treat NULLs as colliding in a unique index, so this constrains open
    -- requests without constraining history - a user may be rejected, wait,
    -- and ask again.
    --
    -- Done here rather than as a check-then-insert in Python because two
    -- submissions arriving together would both pass the check and both insert.
    pending_key      VARCHAR(160) GENERATED ALWAYS AS
                     (IF(status = 'PENDING', CONCAT(username, ':', to_role), NULL)) STORED,

    PRIMARY KEY (id),
    UNIQUE KEY uq_pending (pending_key),
    KEY idx_status (status, created_at),
    KEY idx_user (username, created_at)
) ENGINE=InnoDB;


-- --- Verification ---------------------------------------------------------
--   SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
--     FROM information_schema.COLUMNS
--    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'advisor_user'
--    ORDER BY ORDINAL_POSITION;
--
--   -- password_hash must read YES. If it reads NO, no SSO account can be
--   -- created and the first OIDC login will fail on insert.
--
--   -- The generated column must reject a second open request:
--   --   INSERT INTO role_request (...) VALUES ('a.user', ..., 'PENDING', ...);
--   --   INSERT INTO role_request (...) VALUES ('a.user', ..., 'PENDING', ...);
--   -- The second must fail with ER_DUP_ENTRY on uq_pending.
--
--   SELECT username, role, auth_source, role_source, external_id
--     FROM advisor_user ORDER BY id;
--   -- Every pre-existing account should read LOCAL / MANUAL / NULL.
