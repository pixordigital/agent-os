-- 003_agent_temperature.sql — temperature per agent.
--
-- Temperature rides with the model binding (same row, same decision moment): the creation form
-- asks for model + temperature together, so they must live together. A separate table for one
-- numeric column would be a speculation with no consumer.
--
-- Range 0..2 matches the OpenAI/Anthropic API contract. CLI backends map it to their own
-- scale (codex reasoning effort, agy effort) in the Backend adapter, Wave 3+.

ALTER TABLE public.agents
  ADD COLUMN IF NOT EXISTS temperature numeric(3,2) NOT NULL DEFAULT 0.7
  CHECK (temperature >= 0 AND temperature <= 2);
COMMENT ON COLUMN public.agents.temperature IS
  'Sampling temperature, chosen with the model on the creation form. 0 deterministic, 2 maximal
   variance. Default 0.7 suits supervised workers; judges and routers usually want lower.';
