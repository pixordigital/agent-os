# AgentOS Architecture (Wave 2)

```
Slack --(socket)--> intake -> tasks --(ARQ/Redis)--> worker -> OpenRouter
  |                     |                                |
  v                     v                                v
HITL cards        Postgres (fonte)              agent_runs (custo real)
```

## Decisões fechadas (com motivo)

- **Postgres é a fonte; Redis acelera.** Fila, lease, state machine e versões no banco.
  Worker morto libera lease em 5min. Sem estado em memória que importe.
- **Fail-closed em tudo:** sem policy = HITL, nunca ALLOW. Sem GUC de tenant = zero linhas.
  Sem token = erro alto, nunca no-op silencioso.
- **Orquestrador não executa; worker não decide autonomy.** Dispatch é função pura
  (testável); policy + humano decidem; worker obedece.
- **Leitura do dashboard via PostgREST + RLS; escrita server-side via asyncpg.**
  O `apikey` sozinho não troca o role no postgrest 14.6 — sempre mandar
  `Authorization: Bearer` junto (verificado no fio).
- **Prefixo de prompt determinístico** (BLOCK 0 imutável + BLOCK 1 diário + volátil).
  Texto externo nunca entra no bloco cacheável.
- **Custo medido, não estimado:** `usage.cost` do provedor em USD + constante documentada
  para BRL. Sem rate cards até a onda 5.
- **Socket Mode para Slack:** sem TLS público no servidor, Events API é inviável.
- **Supabase enxuto (db+rest):** 14 containers não cabem em 7.6Gi com swap esgotada.
  Kong/Auth/Realtime/Storage entram quando houver RAM ou segundo host.

## Fronteiras por módulo

| Módulo | Lê | Escreve | Nunca faz |
|---|---|---|---|
| `app/agents.py` | PostgREST (service) | PostgREST (service) | executar, rotear |
| `app/orchestrator/` | listas em memória | nada (retorna Route) | I/O, LLM |
| `app/worker/` | Postgres direto | tasks, runs, approvals, events | aprovar, decidir policy |
| `app/channels/` | Slack API | Slack API | rotear, executar |
| `app/llm/` | OpenRouter | nada | retry, routing |
| `app/guardrails/` | nada | nada | bloquear por keyword |
