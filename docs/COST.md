# AgentOS Cost (Wave 2)

## O que é medido (hoje)

- `agent_runs.cost_usd` — `usage.cost` do OpenRouter, por request. Fonte auditável.
- `agent_runs.cost_brl` — `cost_usd × 5.5` (`USD_BRL_ESTIMATE`, constante documentada,
  mostrada com `~` onde aparece).
- `cached_tokens` / `cache_creation_tokens` — hit rate medido, nunca assumido.
- Sem `cost_usd` (NULL) = provedor não reportou = **desconhecido, não zero**.

## Alavancas ativas

1. Policy gate antes de qualquer chamada (DENY/sem policy = zero tokens).
2. Budget diário por agente com pré-check (estoura = HITL, sem chamada).
3. Stop conditions por task (`max_tokens`, `max_tool_calls`, `max_attempts=3`).
4. Retry só no que retry resolve (transporte/5xx/429); 4xx/auth nunca.
5. Prefixo estável + sessão longa (base para cache hit quando houver volume).
6. Handoff por delta (manager futuro lê resumo, nunca transcrição).

## Infra atual (host 7.6Gi)

Supabase enxuto (~500MB) + UI (~200MB) + worker (~200MB) + redis (~30MB).
Stack completa do Supabase: NÃO — não cabe. Segunda stack: NÃO — mesmo motivo.

## Pendente (ondas seguintes)

- Rate cards + reconciliação (onda 5): custo projetado vs real.
- Detector de adequação com downgrade automático de modelo superdimensionado.
- Orçamento por time/departamento com hard stop.
