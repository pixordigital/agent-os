# AgentOS Runbook

Procedimentos operacionais. Em inglês técnico onde for comando, PT-BR onde for decisão.

## Go-live checklist (Wave 2 -> produção real)

Duas chaves, um app. Sem eles, o worker falha alto e contabilizado (nunca silencioso):

1. `OPENROUTER_API_KEY` — chave `sk-or-v1-...` do OpenRouter. Sem ela, toda task falha com
   `missing OPENROUTER_API_KEY` (erro claro, run registrado, zero token gasto).
   Passar como env do container worker (`-e OPENROUTER_API_KEY=...`).
2. `SLACK_BOT_TOKEN` (`xoxb-*`) — instalar o app no workspace (OAuth & Permissions).
3. `SLACK_APP_TOKEN` (`xapp-*`) — Socket Mode ativo + App-Level Token. Sem URL pública
   porque Socket Mode é outbound-only (o servidor não tem proxy/TLS).

## Subir o stack (host atual: 178.105.181.38)

```bash
# Supabase enxuto (db + rest) — serviço Coolify `supabase` no projeto agent-os.
# Se cair: ver `docker logs supabase-db-<uuid>`; causa #1 é senha do authenticator.

# UI + worker + redis (containers locais, mesma rede do serviço):
docker run -d --name agentos-ui --network <svc-net> -p 7777:7777 \
  -e APP_ENV=production -e PORT=7777 -e SUPABASE_PATH_PREFIX="" \
  -e DATABASE_URL="postgresql://postgres:$PW@supabase-db:5432/postgres" \
  -e SUPABASE_URL="http://supabase-rest:3000" \
  -e SUPABASE_ANON_KEY="$ANON" -e SUPABASE_SERVICE_KEY="$SVC" \
  agent-os:0.1.0
docker run -d --name agentos-redis --network <svc-net> redis:7-alpine
docker run -d --name agentos-worker --network <svc-net> \
  -e DATABASE_URL="postgresql://postgres:$PW@supabase-db:5432/postgres" \
  -e REDIS_URL="redis://agentos-redis:6379" \
  agent-os:0.1.0 python -m app.worker
```

## Bootstrap de banco novo (one-time, via supabase_admin)

```sql
GRANT service_role TO postgres;              -- migration precisa (OWNER TO service_role)
ALTER SCHEMA public OWNER TO postgres;       -- app migra como postgres
ALTER ROLE authenticator WITH PASSWORD '<POSTGRES_PASSWORD>';  -- PostgREST loga como ele
```

Detalhe e motivos em `supabase/bootstrap/README.md`.

## Incidentes

| Sintoma | Causa provável | Ação |
|---|---|---|
| Task presa em `queued` | worker fora do ar ou sem Redis | `docker ps` nos 3 containers; `docker logs agentos-worker` |
| Task `failed: missing OPENROUTER_API_KEY` | chave não configurada | go-live item 1 |
| Task `waiting_human` sem card no Slack | sem `SLACK_BOT_TOKEN` | go-live item 2; a approval existe em `pending_approvals` e pode ser resolvida pela UI futura |
| `/ready` 503 | Postgres fora ou migration pendente | log do boot mostra qual migration quebrou |
| Fila não anda, worker vivo | lease preso (`lease_until` futuro de worker morto) | expira sozinho em 5min; não mexer à mão |
| Disco >85% | imagens acumuladas | `docker image prune -af` (liberou 640MB da última vez; 21GB eram reclamáveis) |
| RAM esgotada | stack completa do Supabase não cabe (7.6Gi) | NÃO subir os 14 containers; o enxuto (db+rest) é o teto até a onda de infra |
