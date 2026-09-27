# AgentOS — Autonomous Agent Platform

Plataforma onde o owner cria **departamentos** (times) da empresa, cada um vira **managers +
agents** que rodam 24/7, e o owner comanda tudo por **Slack**. DB: **Supabase self-hosted**.
Deploy: **Coolify**. Construída dentro do **opencode**.

Leia este arquivo antes de qualquer tarefa. Ele é a spec de arquitetura, escopo, segurança e
ordem de entrega (Ondas 0-8).

## Princípio inegociável: verdade

Nunca inventar, nunca prometer o que não existe em código.

- **Zero número na tela que não veio do banco.** Preço, custo, token, taxa, economia projetada.
  Todo número tem `fonte` + `timestamp`. Projeção usa `~` e `*estimativa`.
- **Não chamar degradação de "queda" sem baseline.** Um agente com 82% de success que sempre
  teve 82% não está degradado. Degradação é queda **relativa ao baseline do próprio agente**.
- **Menos de 30 execuções** em `agent_runs` para o par `(role, model)` → o badge mostra
  `observação insuficiente`. Não inventa veredito de produção.
- **Economia sugerida** sempre calculada de `cost_events` / `agent_runs` reais, nunca de
  fotografia. Se for projeção, marca `~`.
- Se o dado não existe, o produto **não tem** a feature. Não inventa número pra ter.

## Governança: o owner decide

- O owner (CEO) é o único humano com poder de aprovação. **Team leads são consultivos**:
  comandam, priorizam, delegam e pausam o próprio time; **não aprovam** nada.
- **Filtro de gestão:** o que o **CEO** escreve em `#mgr-*` passa por
  `share|hold|ask` (default `hold` — contexto de gestão não desce pro time).
  O que o **team lead** escreve em `#team-*` desce **inteiro**, sem filtro.
- **Ações irreversíveis sempre HITL:** dinheiro, produção, compromisso externo, mudança de
  policy, mudança de modelo/prompt/autonomia por mão humana.
- **Única ação automática:** `lower_autonomy` quando 2+ detectores ficam `high` (fail-safe —
  baixar autonomia nunca é destrutivo).
- Desvio deste arquivo exige: descrição técnica, seção violada, risco concreto, alternativa
  conforme, e **aprovação expressa do owner**. Sem isso, não executar.

## Fronteira edge vs worker (Supabase self-hosted)

O `supabase-edge-functions` é **beta** e tem `workerTimeoutMs=60s` default. Sem rede global.
O ganho do edge é **isolamento de processo**, não distribuição.

| Edge Function (Deno, stateless) | Worker (container, stateful) |
|---|---|
| intake Slack, orchestrator, plan-publish | loop de LLM, tools, browser/shell |
| degradation-scan, kpi-refresh, model-cards | lease, retry, idempotência de execução |
| meeting-minutes, admin-action | os 4 runtimes de CLI |
| webhooks Coolify | TTS, transcrição |

**Regra dura:** edge = intake, decisão e scan. **Nunca** execução. Postgres é a única fonte
de verdade; Redis é aceleração, nunca estado.

## Agendador

**Um só:** cron do ARQ no worker, que chama edge functions por HTTP. Sem pg_cron. Um agendador
com retry vale mais que dois sem.

## Prompt caching e custo

O custo está no **loop**, não na chamada. 20 turnos com prefixo estável = 1 escrita + 19 leituras.

**Layout de request (`native`):**
```
BLOCK 0  system, cache_control ephemeral ttl=1h   imutável, versionado
         identidade + missão + SOP + policies + tool schemas (chaves ordenadas)
         + few-shots congelados + output JSON schema
BLOCK 1  cacheável, sem cache_control              muda 1x/dia
         data, plano do dia, estado do backlog
BLOCK 2  volátil
         mensagem atual, task, tool results, chunks RAG
```

- **Determinismo do prefixo** é o que compra o desconto: sem timestamp, sem uuid, ordem fixa,
  chaves JSON ordenadas, whitespace estável. **1 caractere diferente = miss total.**
  Isso é protegido por `test_prefix_stable()` no CI.
- **Nada de contexto ambiente no container.** `claude -p --bare`, config explícito de
  `opencode.json`. Se o agente acha `CLAUDE.md` ou hook no disco, o prefixo muda e o cache morre.
- **Sessão longa > chamada fria.** `opencode serve` + `run --attach`, `agy --input-format
  stream-json` com stdin aberto, `codex exec resume`, `claude --continue`.
- `caching_mode ∈ {explicit, implicit, none}` em `model_cards`. `cached_tokens` e
  `cache_creation_tokens` em `agent_runs`. **Mede, não promete.**

**Alavancas de custo, em ordem de impacto:**
1. prefixo estável + sessão longa
2. routing em 2 níveis + detector de adequação (rebaixa modelo superdimensionado)
3. stop conditions por task (`max_tokens`, `max_tool_calls`, `budget_brl_task`,
   `max_handoffs 5`, `max_delegations 3`) — loop descontrolado é o gasto nº 1
4. handoff por delta (`summary + acceptance_criteria + artifact_refs`), **nunca** transcrição
5. verificação determinística **antes** do judge LLM
6. output estruturado (`--output-schema` / `json_schema`) → menos retry
7. cache de tool result por TTL; cache de resposta só pra query read-only determinística
8. `budget_brl_day` com hard stop, alerta 50/75/90; agente ocioso = custo zero
9. dashboard mostra **custo por task resolvida**, não custo por token

## Backends (4 CLIs + native)

Contrato único `Backend(build_argv, parse_event, model_binding, permission_profile,
structured_output)`. Uma abstração, 4 implementações — ela existe porque são 4 CLIs reais.

| backend | headless | stream | modelo por agent | sessão longa |
|---|---|---|---|---|
| `native` | loop próprio | OpenAI-compat | `agent_models.model` (OpenRouter) | n/a |
| `claude_code` | `claude -p` | `--output-format stream-json --verbose` | `--model` + `ANTHROPIC_DEFAULT_*_MODEL` | `--continue` / stdin stream-json |
| `codex` | `codex exec` | `--json` (NDJSON) | `--model` + `-c model_reasoning_effort=` | `exec resume SESSION_ID` |
| `opencode` | `opencode run` | `--format json` | `--model provider/model` | `serve` + `--attach` |
| `antigravity` | `agy -p` | `--output-format stream-json` | `--model` + `--effort` | stdin aberto (stream-json) |

Fatos verificados que mudam o desenho:
- **codex `--full-auto` é deprecated** → usar `--sandbox read-only|workspace-write|danger-full-access`.
- **Claude Code via OpenRouter** = `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` +
  `ANTHROPIC_API_KEY=""` (explicitamente **vazio**, senão ele cai pra Anthropic direto).
  Só garantido com provider Anthropic 1P — é a limitação real, não vou esconder.
- **Antigravity não tem caminho OpenRouter** → Gemini API key, catálogo separado em `model_cards`.
- **Todos os 4 rodam desatendidos com API key.** Nenhum OAuth, nenhum keyring.
- `claude_code` = **controle total do prefixo** (`--system-prompt-file` substitui o system
  prompt). `codex` e `antigravity` = prefixo do CLI, só `resume`/sessão longa economiza.
- **Sandbox explícito é auditável.** `dev-infra` roda `read-only` e só abre PR.

## Degradação (8 detectors, 6 ações)

Degradação é **queda relativa ao baseline** do próprio agente (`agent_baselines`, 30d).

`success_drop` (7d < baseline − 15pp) · `retry_spike` (>3) · `format_violation` (>5%) ·
`latency_breach` (p95 > teto do role) · `cost_breach` (2 janelas) · `escalation_spike`
(+20pp) · `eval_regression` (prompt mudou e pass_rate caiu >5pt) · `tool_error_new`.

Ações em ordem: `swap_model` → `patch_prompt` → `revert_prompt` → `swap_backend` →
`lower_autonomy` (**auto**) → `disable_agent`. Unique open por `(agent, detector)`, auto-resolve
em 2 janelas. `evidence` carrega o número cru, nunca adjetivo.

## Verificação (o "de forma correta")

Determinístico **primeiro**: schema válido? campos obrigatórios? exit 0? teste verde? arquivo
existe? Só depois o judge LLM confere contra `acceptance_criteria`, e **cada critério upheld
precisa citar a linha do output que o cumpre**. Sem citação → `rejected`.

**Separação de função:** task `risk=alto` não pode ser verificada por quem delegou — vai pra
outro agent com role `reviewer` ou pro owner. `override_rate` separa **manager vs humano**.

## Stack

- DB: Supabase self-hosted (Coolify), `asyncpg`, RLS `SET LOCAL app.current_org_id`
- Leitura do dashboard: **PostgREST + RLS**. Zero código de API de leitura.
- API/worker/scheduler: FastAPI async + ARQ (Redis), **1 Dockerfile 3 entrypoints**
- Slack: Socket Mode (`slack_bolt`) — bot sem URL pública; webhooks só pra HITL
- Dashboard: Jinja2 + HTMX, sem build step. API é JSON por trás → React trocável sem tocar no back
- Áudio: `whisper.cpp` local, modelo `base`, **sem diarização** (aceito)
- CI: ruff + pytest + gitleaks

## Ondas

- **1 base** — repo, CI, Coolify, Supabase, `tasks`+`parent_task_id`+state machine+lease+
  idempotência, `policies` fail-closed, `people`/`org_roles`/`slack_links`, `agent_runs`,
  RLS **por org e por time**
- **2 executar** — `native` ponta a ponta, `slack-events`+`intake`+`orchestrator`, org chart ao vivo
- **3 backends** — adapter provado com 1 CLI (codex); os outros 3 são mecânicos
- **4 os 4 CLIs** — permission profile por role, `--bare`, sandbox
- **5 tempo** — dept/roles/teams, `role_requirements`, modelo por agent, `model_cards`,
  eval harness + veredito, `schedules` + budgets + retry matrix + DLQ
- **6 teu comando** — manager loop, backlog + planos dia/semana, degradação + 6 ações,
  dashboards por time, canais + `broadcast_decisions`, `invite` + humanos no org chart
- **7 governança** — reuniões all-hands + 1:1, minutos com `source_quote`, relay por time
- **8 vender** — billing + `PLANS`, onboarding self-serve, runtime dedicado

Cada onda tem entregável verificável. **A onda 1 é o que importa agora.**

## Comandos

- `graphify update .` depois de mudar código (AST-only, sem custo de API)
- `pytest -q` · `ruff check .`
- Testes obrigatórios: isolamento RLS (cross-org **e** cross-team deve falhar), idempotência
  (webhook repetido não duplica), `test_prefix_stable()` (protege o cache), state machine
  (transição inválida rejeitada), lease (worker morto libera), `can_decide` (botão que não
  pode ser apertado não existe), guard de observação insuficiente no veredito de degradação
- `docs/ARCHITECTURE.md`, `docs/COST.md`, `docs/RUNBOOK.md` são atualizados junto com a onda

## Regras de escrita

Código, identificadores, schema, docstring e comentário: **English**.
Texto de negócio (Slack, relatórios, org chart, HITL card): **PT-BR**.
Comentário explica **por quê**, não o quê. Sem `TODO` sem owner e prazo. Sem comentário em
código morto — deleta. Escolha de código registra o que foi descartado.
