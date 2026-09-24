-- Aged Ticket Advisor - schema
-- Completely separate from itsm_analytics_db. No shared tables, no shared writes.
--   mysql -u root -p < db/schema.sql

CREATE DATABASE IF NOT EXISTS sd_advisor_db
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE sd_advisor_db;


-- ---------------------------------------------------------------------------
-- Sync bookkeeping
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_state (
    name            VARCHAR(64)  NOT NULL,
    watermark       DATETIME     NULL,
    last_run_at     DATETIME     NULL,
    last_status     VARCHAR(32)  NULL,
    detail          TEXT         NULL,
    PRIMARY KEY (name)
) ENGINE=InnoDB;


-- ---------------------------------------------------------------------------
-- Ticket snapshot. One row per open in-scope incident, refreshed by delta sync.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watched_ticket (
    incident_number     VARCHAR(50)  NOT NULL,
    sys_id              VARCHAR(64)  NOT NULL,
    short_description   TEXT         NULL,
    description         MEDIUMTEXT   NULL,
    caller_name         VARCHAR(150) NULL,
    caller_sys_id       VARCHAR(64)  NULL,
    assigned_to         VARCHAR(150) NULL,
    assignment_group    VARCHAR(150) NULL,
    state               VARCHAR(50)  NULL,
    hold_reason         VARCHAR(100) NULL,
    priority            VARCHAR(25)  NULL,
    impact              VARCHAR(25)  NULL,
    urgency             VARCHAR(25)  NULL,
    category            VARCHAR(100) NULL,
    subcategory         VARCHAR(100) NULL,
    ci                  VARCHAR(200) NULL,
    contact_type        VARCHAR(50)  NULL,
    rfc                 VARCHAR(50)  NULL,
    problem_id          VARCHAR(50)  NULL,
    opened_at           DATETIME     NULL,
    sys_updated_on      DATETIME     NULL,
    reassignment_count  INT          NOT NULL DEFAULT 0,
    reopen_count        INT          NOT NULL DEFAULT 0,
    active              TINYINT(1)   NOT NULL DEFAULT 1,
    content_hash        CHAR(64)     NULL,
    first_seen_at       DATETIME     NOT NULL,
    last_synced_at      DATETIME     NOT NULL,
    PRIMARY KEY (incident_number),
    KEY idx_wt_group   (assignment_group),
    KEY idx_wt_agent   (assigned_to),
    KEY idx_wt_updated (sys_updated_on),
    KEY idx_wt_active  (active, opened_at)
) ENGINE=InnoDB;


-- ---------------------------------------------------------------------------
-- Deterministic signals. Append-only so trends are queryable in Grafana.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ticket_signal (
    id                          BIGINT       NOT NULL AUTO_INCREMENT,
    incident_number             VARCHAR(50)  NOT NULL,
    computed_at                 DATETIME     NOT NULL,

    age_days                    DECIMAL(8,2) NOT NULL DEFAULT 0,
    idle_days                   DECIMAL(8,2) NOT NULL DEFAULT 0,
    days_in_state               DECIMAL(8,2) NOT NULL DEFAULT 0,
    last_agent_action_at        DATETIME     NULL,
    last_caller_activity_at     DATETIME     NULL,

    ball_in_court               VARCHAR(20)  NOT NULL DEFAULT 'AGENT',
    caller_replied_unanswered   TINYINT(1)   NOT NULL DEFAULT 0,

    dependency_ref              VARCHAR(50)  NULL,
    dependency_state            VARCHAR(50)  NULL,
    dependency_resolved         TINYINT(1)   NOT NULL DEFAULT 0,

    followup_count              INT          NOT NULL DEFAULT 0,
    days_since_last_followup    DECIMAL(8,2) NULL,
    auto_close_candidate        TINYINT(1)   NOT NULL DEFAULT 0,

    sla_breached                TINYINT(1)   NOT NULL DEFAULT 0,
    sla_pct_consumed            DECIMAL(6,2) NULL,
    sla_time_left_mins          DECIMAL(12,2) NULL,
    projected_breach_at         DATETIME     NULL,

    expected_resolution_hours   DECIMAL(10,2) NULL,
    p90_overrun                 TINYINT(1)   NOT NULL DEFAULT 0,

    kb_available                TINYINT(1)   NOT NULL DEFAULT 0,
    kb_attached                 TINYINT(1)   NOT NULL DEFAULT 0,

    attention_score             INT          NOT NULL DEFAULT 0,
    score_breakdown             JSON         NULL,
    risk_flags                  JSON         NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_sig (incident_number, computed_at),
    KEY idx_sig_latest (incident_number, computed_at DESC),
    KEY idx_sig_score  (computed_at, attention_score DESC)
) ENGINE=InnoDB;


-- ---------------------------------------------------------------------------
-- LLM recommendation. Keyed on input_hash so an unchanged ticket is never
-- re-analysed - this is the main cost control.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recommendation (
    id                      BIGINT        NOT NULL AUTO_INCREMENT,
    incident_number         VARCHAR(50)   NOT NULL,
    created_at              DATETIME      NOT NULL,
    model                   VARCHAR(100)  NULL,
    prompt_version          VARCHAR(20)   NULL,
    input_hash              CHAR(64)      NOT NULL,

    recommended_action      VARCHAR(40)   NOT NULL,
    confidence              DECIMAL(4,3)  NOT NULL DEFAULT 0,
    rationale               TEXT          NULL,
    suggested_target_group  VARCHAR(150)  NULL,
    draft_work_note         TEXT          NULL,
    draft_caller_message    TEXT          NULL,
    lead_feedback           TEXT          NULL,
    risk_flags              JSON          NULL,
    evidence                JSON          NULL,

    surfaced                TINYINT(1)    NOT NULL DEFAULT 1,
    superseded              TINYINT(1)    NOT NULL DEFAULT 0,
    latency_ms              INT           NULL,
    prompt_tokens           INT           NULL,
    completion_tokens       INT           NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uq_rec_input (incident_number, input_hash),
    KEY idx_rec_inc (incident_number, created_at DESC),
    KEY idx_rec_action (recommended_action, created_at)
) ENGINE=InnoDB;


-- ---------------------------------------------------------------------------
-- Lead feedback. This table is what makes "good accuracy" measurable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recommendation_feedback (
    id                  BIGINT       NOT NULL AUTO_INCREMENT,
    recommendation_id   BIGINT       NULL,
    incident_number     VARCHAR(50)  NOT NULL,
    user_name           VARCHAR(100) NOT NULL,
    decision            ENUM('ACCEPTED','REJECTED','MODIFIED','NOT_APPLICABLE','DONE') NOT NULL,
    actual_action       VARCHAR(40)  NULL,
    comment             TEXT         NULL,
    created_at          DATETIME     NOT NULL,
    PRIMARY KEY (id),
    KEY idx_fb_rec (recommendation_id),
    KEY idx_fb_inc (incident_number, created_at DESC),
    KEY idx_fb_when (created_at)
) ENGINE=InnoDB;


-- ---------------------------------------------------------------------------
-- Snooze / suppression. Stops the tool nagging about tickets already handled.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS suppression (
    incident_number VARCHAR(50)  NOT NULL,
    snoozed_until   DATETIME     NOT NULL,
    reason          VARCHAR(255) NULL,
    created_by      VARCHAR(100) NULL,
    created_at      DATETIME     NOT NULL,
    PRIMARY KEY (incident_number),
    KEY idx_sup_until (snoozed_until)
) ENGINE=InnoDB;


-- ---------------------------------------------------------------------------
-- Tunable attention weights. Same "must sum to 100" convention as the
-- existing audit tool's compliance_weightage_table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attention_weightage (
    component   VARCHAR(50)  NOT NULL,
    weightage   INT          NOT NULL,
    description VARCHAR(255) NULL,
    enabled     TINYINT(1)   NOT NULL DEFAULT 1,
    PRIMARY KEY (component)
) ENGINE=InnoDB;

INSERT INTO attention_weightage (component, weightage, description) VALUES
    ('sla_jeopardy',   25, 'How close the resolution SLA is to breaching'),
    ('stagnation',     25, 'Days since the last meaningful agent action'),
    ('age',            10, 'Age beyond the aged-after threshold'),
    ('priority',       10, 'Business priority and impact of the incident'),
    ('churn',           5, 'Reassignment and reopen churn'),
    ('blocked_stale',  15, 'Caller replied unanswered, or dependency already closed'),
    ('p90_overrun',    10, 'Past the p90 resolution time for tickets of this pattern')
ON DUPLICATE KEY UPDATE description = VALUES(description);


-- ---------------------------------------------------------------------------
-- Delivery log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS digest_run (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    run_type      VARCHAR(40)  NOT NULL,
    audience      VARCHAR(40)  NOT NULL,
    recipient     VARCHAR(200) NOT NULL,
    ticket_count  INT          NOT NULL DEFAULT 0,
    new_count     INT          NOT NULL DEFAULT 0,
    sent_at       DATETIME     NOT NULL,
    status        VARCHAR(20)  NOT NULL,
    error         TEXT         NULL,
    PRIMARY KEY (id),
    KEY idx_dr_when (sent_at)
) ENGINE=InnoDB;


-- ---------------------------------------------------------------------------
-- Alert dedupe - one alert per ticket per type per day
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_log (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    incident_number VARCHAR(50)  NOT NULL,
    alert_type      VARCHAR(40)  NOT NULL,
    fired_on        DATE         NOT NULL,
    fired_at        DATETIME     NOT NULL,
    channel         VARCHAR(20)  NOT NULL,
    payload         JSON         NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_alert (incident_number, alert_type, fired_on)
) ENGINE=InnoDB;


-- ---------------------------------------------------------------------------
-- Vector index for similar-incident and KB retrieval
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS embedding_store (
    id          BIGINT       NOT NULL AUTO_INCREMENT,
    entity_type ENUM('incident','kb') NOT NULL,
    entity_key  VARCHAR(100) NOT NULL,
    text_hash   CHAR(64)     NOT NULL,
    dims        INT          NOT NULL,
    vector      MEDIUMBLOB   NOT NULL,
    meta        JSON         NULL,
    updated_at  DATETIME     NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_emb (entity_type, entity_key),
    KEY idx_emb_type (entity_type)
) ENGINE=InnoDB;


-- ---------------------------------------------------------------------------
-- Resolution-time baselines, used for expected duration and p90 overrun
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resolution_stat (
    scope_key   VARCHAR(255) NOT NULL,
    sample_size INT          NOT NULL,
    p50_hours   DECIMAL(10,2) NOT NULL,
    p90_hours   DECIMAL(10,2) NOT NULL,
    updated_at  DATETIME     NOT NULL,
    PRIMARY KEY (scope_key)
) ENGINE=InnoDB;


-- ---------------------------------------------------------------------------
-- UI users
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS advisor_user (
    id                BIGINT       NOT NULL AUTO_INCREMENT,
    username          VARCHAR(100) NOT NULL,
    full_name         VARCHAR(150) NULL,
    email             VARCHAR(200) NULL,
    password_hash     VARCHAR(255) NOT NULL,
    role              ENUM('ADMIN','LEAD','VIEWER') NOT NULL DEFAULT 'LEAD',
    assignment_groups TEXT         NULL,
    active            TINYINT(1)   NOT NULL DEFAULT 1,
    last_login_at     DATETIME     NULL,
    created_at        DATETIME     NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_user (username)
) ENGINE=InnoDB;


-- ---------------------------------------------------------------------------
-- Convenience view: latest signal per ticket, joined to latest recommendation.
-- Grafana and the UI board both read this.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_current_board AS
SELECT
    w.incident_number,
    w.short_description,
    w.assignment_group,
    w.assigned_to,
    w.caller_name,
    w.state,
    w.hold_reason,
    w.priority,
    w.category,
    w.subcategory,
    w.opened_at,
    s.computed_at,
    s.age_days,
    s.idle_days,
    s.ball_in_court,
    s.caller_replied_unanswered,
    s.dependency_resolved,
    s.auto_close_candidate,
    s.sla_breached,
    s.sla_pct_consumed,
    s.projected_breach_at,
    s.p90_overrun,
    s.kb_available,
    s.kb_attached,
    s.attention_score,
    s.risk_flags,
    r.id                     AS recommendation_id,
    r.recommended_action,
    r.confidence,
    r.rationale,
    r.suggested_target_group,
    r.lead_feedback,
    (sup.incident_number IS NOT NULL AND sup.snoozed_until > NOW()) AS snoozed
FROM watched_ticket w
JOIN ticket_signal s
      ON s.incident_number = w.incident_number
     AND s.id = (SELECT MAX(s2.id) FROM ticket_signal s2
                  WHERE s2.incident_number = w.incident_number)
LEFT JOIN recommendation r
      ON r.incident_number = w.incident_number
     AND r.superseded = 0
     AND r.id = (SELECT MAX(r2.id) FROM recommendation r2
                  WHERE r2.incident_number = w.incident_number AND r2.superseded = 0)
LEFT JOIN suppression sup
      ON sup.incident_number = w.incident_number
WHERE w.active = 1;
