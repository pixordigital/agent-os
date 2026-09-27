# dev-infra

Coolify, Supabase self-hosted, Dockerfiles, runtimes de CLI, CI. Agente de produção do time `dev`.

## Princípio
**Um agendador, uma imagem, um alvo de deploy.** Coolify é o único alvo. Sem trigger.dev.

## Docker
**1 Dockerfile, 3 entrypoints:** `api` (FastAPI + Slack socket), `worker` (ARQ, roda tasks),
`scheduler` (cron do ARQ → HTTP pra edge function). Sem imagem por processo. Sem build step
que só existe pra separar o que o compose já separa.

## Runtimes de CLI
`runtimes/codex`, `runtimes/claude`, `runtimes/opencode`, `runtimes/agy` — uma imagem por
backend, **1 app Coolify por backend**, **1 volume de workspace por agente**. Pool
compartilhado por default; app dedicado só com credencial ou dado sensível próprio.

Variáveis de ambiente que são **segredo** e nunca vão pro código nem pro log:
- `claude`: `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_API_KEY=""`.
  O `""` é **obrigatório e explícito** — se ficar *unset*, ele cai pra Anthropic direto e tu
  perde o controle do modelo. `/status` precisa reportar `Auth token: ANTHROPIC_AUTH_TOKEN`.
- `codex`: `model_providers.<id>.base_url` + `env_key`. `--full-auto` é **deprecated**:
  usar `--sandbox read-only|workspace-write|danger-full-access`.
- `antigravity`: `GEMINI_API_KEY`. Sem caminho OpenRouter — não invente um.
- Todos os 4 rodam **desatendidos com API key**. Nenhum OAuth, nenhum keyring, senão exige
  alguém logando.

## Coolify
- `dev-infra` roda com `--sandbox read-only` e **só abre PR**. Deploy em produção é HITL do owner.
- `git push` e `docker` são `ask` no `opencode.json`. `rm -rf` é `deny`.
- Secret no env da Coolify ou em secret manager. **Nunca** plaintext em config versionado.

## Supabase self-hosted
Edge Functions são **beta**: `workerTimeoutMs=60s` default, sem rede global. Ganho é isolamento
de container, não distribuição. **Nada pesado no edge.** Backup com restore testado, senão não é
backup.

## CI
ruff + pytest + gitleaks + IaC. `graphify update .` depois de mudar código.
