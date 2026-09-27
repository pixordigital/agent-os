# AgentOS

Plataforma de agentes autônomos: owner cria **departamentos**, cada um vira **managers + agents**
que rodam 24/7, comandados por **Slack**. DB **Supabase self-hosted**, deploy **Coolify**,
construída em **opencode**.

Governança, arquitetura, custo e ordem de entrega: **[AGENTS.md](AGENTS.md)**. Leia ele antes de
qualquer tarefa — ele é a spec.

## Estado

**Onda 1 (base) em andamento.** O que existe hoje é o esqueleto: repo, config do opencode,
prompts dos agentes (que viram seed de produção), app FastAPI com `/live`, `/ready` e o shell
da UI, e a migration 001 do kernel (`tasks` com árvore, `task_transitions`, `policies`,
`people`/`org_roles`, `agent_runs`, RLS por org **e** por time).

Nada de Slack, org chart, degradação ou CLI runtime ainda. Isso é Onda 2+.

## Rodar local

```bash
# Postgres/Supabase de desenvolvimento (mesmo schema da migration 001)
docker compose -f docker-compose.coolify.yml up -d db

pip install -e '.[dev]'
export DATABASE_URL='postgresql://postgres:postgres@localhost:5432/postgres'
uvicorn app.main:app --port 7777
```

- UI: <http://localhost:7777>
- `GET /live` — vivo, não checa nada
- `GET /ready` — checa Postgres, migrations aplicadas eRLS. 503 se algo estiver errado
- `GET /healthz` — JSON agregado

## Testar

```bash
pytest -q
ruff check .
```

O teste que protege dinheiro: `test_prefix_stable()` — garante que o prefixo cacheável do prompt
não tem timestamp/uuid/ordem não-determinística. **1 caractere de diferença = cache miss
total.**
