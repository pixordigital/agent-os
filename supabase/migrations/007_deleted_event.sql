-- 007_deleted_event.sql — allow the 'deleted' kind on the operational timeline.
--
-- Agent deletion emits one org-scoped timeline row (agent_id NULL, since the agent's own
-- rows cascade away with it). The CHECK inline in 004 cannot take a new value without
-- dropping, so it is replaced here with the same list plus 'deleted'.

ALTER TABLE public.agent_events DROP CONSTRAINT IF EXISTS agent_events_kind_check;
ALTER TABLE public.agent_events ADD CONSTRAINT agent_events_kind_check
  CHECK (kind IN (
    'created','updated','status_changed','deleted',
    'task_assigned','task_started','task_completed','task_failed',
    'heartbeat','model_swapped',
    'alert_opened','alert_resolved','hitl_pending','note'));
