-- 007: settings that an administrator can change without a shell.
--
-- Why a table rather than writing config/conf.json.
--
-- conf.json is what the service needs in order to start. It carries the Vault
-- address, the database host and the OIDC client registration - the things
-- that have to be right before anything can report that they are wrong. A web
-- process that rewrites that file can, on a bad day, leave behind a file the
-- service cannot parse, and the failure arrives at the next restart rather
-- than at the moment of the mistake. There is no UI to fix it from, because
-- the UI is what failed to start.
--
-- So the file stays the bootstrap, owned by whoever owns the host, and this
-- table holds overrides for an explicit allow-list of operational keys. An
-- override is one row, written in a transaction, attributable, and reversible
-- by deleting it. The settings page shows the file value beside the override
-- so the two can never silently disagree without somebody being told.
--
-- Idempotent: safe to run more than once.

CREATE TABLE IF NOT EXISTS runtime_setting (
    setting_key   VARCHAR(120)  NOT NULL,
    value_json    TEXT          NOT NULL,
    updated_by    VARCHAR(100)  NOT NULL,
    updated_at    DATETIME      NOT NULL,
    PRIMARY KEY (setting_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Every change, kept. Turning runtime.shadow_mode off starts live mail to
-- every lead and viewer in the estate; "who did that, and when" has to be
-- answerable afterwards, and the current value cannot answer it.
--
-- Deliberately not pruned by the nightly job. This table grows by a handful of
-- rows a month, and the one question it exists to answer is always about
-- something that happened longer ago than anybody expected.
CREATE TABLE IF NOT EXISTS setting_audit (
    id            BIGINT        NOT NULL AUTO_INCREMENT,
    setting_key   VARCHAR(120)  NOT NULL,
    old_value     TEXT          NULL,
    new_value     TEXT          NULL,
    changed_by    VARCHAR(100)  NOT NULL,
    changed_at    DATETIME      NOT NULL,
    PRIMARY KEY (id),
    KEY ix_setting_audit_key (setting_key, changed_at),
    KEY ix_setting_audit_when (changed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
