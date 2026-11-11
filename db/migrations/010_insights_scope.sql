-- 010: preserve queue attribution in Insights history and delivery logs.
--
-- ticket_signal is append-only. Recording the queue on each snapshot lets
-- daily rollups keep a ticket in the queue it belonged to at that time,
-- rather than rewriting its history after reassignment.
--
-- digest_run needs the queue too: a lead digest is sent once per queue, and
-- Insights must not expose another queue's delivery counts to a scoped lead.
--
-- The guarded DDL keeps this migration safe to re-run on supported MySQL
-- versions without relying on ADD COLUMN IF NOT EXISTS.

SET @ata010_sql = IF(
    (SELECT COUNT(*) FROM information_schema.columns
      WHERE table_schema = DATABASE()
        AND table_name = 'ticket_signal'
        AND column_name = 'assignment_group') = 0,
    'ALTER TABLE ticket_signal ADD COLUMN assignment_group VARCHAR(150) NULL AFTER computed_at',
    'SELECT 1');
PREPARE ata010_stmt FROM @ata010_sql;
EXECUTE ata010_stmt;
DEALLOCATE PREPARE ata010_stmt;

SET @ata010_sql = IF(
    (SELECT COUNT(*) FROM information_schema.columns
      WHERE table_schema = DATABASE()
        AND table_name = 'digest_run'
        AND column_name = 'assignment_group') = 0,
    'ALTER TABLE digest_run ADD COLUMN assignment_group VARCHAR(150) NULL AFTER audience',
    'SELECT 1');
PREPARE ata010_stmt FROM @ata010_sql;
EXECUTE ata010_stmt;
DEALLOCATE PREPARE ata010_stmt;
