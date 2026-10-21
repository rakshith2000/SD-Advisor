-- 006 - remove the single-queue wording from seeded text
--
-- The service was built for one queue and now covers several. Nothing about
-- the schema is queue-specific, but one seeded row is: the description shown
-- beside the inactivity weight on the Weights page names the Service Desk,
-- which reads wrongly to a lead whose queue is Desktop Support.
--
-- schema.sql already carries the new text and re-applying it would fix this,
-- but re-applying schema.sql against a live database is not something anyone
-- should have to do for a label. Idempotent and safe to re-run.
--
--   mysql -u sd_advisor -p sd_advisor_db < db/migrations/006_generalise_team_wording.sql

USE sd_advisor_db;

UPDATE attention_weightage
   SET description = 'Elapsed time since the last substantive action by the assigned team'
 WHERE component = 'inactivity_duration'
   AND description = 'Elapsed time since the last substantive Service Desk action';

-- Deliberately NOT touched:
--
--   ticket_signal.pending_action_owner still stores 'SERVICE_DESK'. It is a
--   role, not a queue name - it means the team the incident is assigned to
--   owes the next action - and the label shown for it now comes from
--   display.team_label in conf.json. Rewriting the stored token would rewrite
--   history, invalidate every cached recommendation keyed on it, and buy
--   nothing a configurable label does not already give.
--
--   advisor_user.assignment_groups is per-account scope and was always a
--   list. Existing rows are correct as they stand.

SELECT component, weightage, description
  FROM attention_weightage
 WHERE component = 'inactivity_duration';
