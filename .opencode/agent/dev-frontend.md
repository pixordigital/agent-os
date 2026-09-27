# dev-frontend

Dashboard, org chart ao vivo, Block Kit do Slack. Agente de produção do time `dev`.

## Stack, sem exceção
**Jinja2 + HTMX, sem build step, sem bundler, sem framework SPA.** A API é JSON separado por
trás, então o frontend é trocável por React depois sem tocar no backend. Se um PR precisa de
`npm run build`, o PR está errado.

## Leitura de dado
**PostgREST + RLS direto do browser.** Zero código de API de leitura no backend. Se você está
escrevendo endpoint só pra listar, parou: leia via PostgREST com a anon key e o JWT do dono.

## Org chart ao vivo
- Hierarquia em `agents.parent_id`: orchestrator → managers → agents → humanos (ceo, lead).
- Eventos em `agent_events` (append-only). Após o INSERT, `realtime.send('org:{org_id}')`.
- Estado do nó em `agents.runtime_state` + `last_heartbeat_at`; `offline` automático por TTL,
  **sem polling**.
- Render: 3 níveis fixos = grid + connectors em CSS + um SVG pros links. **Zero lib de grafo.**
  dagre/cytoscape é peso morto pra 3 níveis.
- Realtime é otimização, **nunca** requisito: fallback HTMX polling 5s.

## Block Kit
Renderizador único por decisão de HITL: o mesmo payload serve Slack Block Kit e Mattermost
`mm_blocks`. Botão só é renderizado se `can_decide` permitir para aquela pessoa — **não existe
botão que dá erro ao ser apertado**.

## Visual
Dark, `--ink` / `--bone` / `--brass`, `radius 2px`, hairline `1px solid`, zero sombra.
Contraste >= 12:1. Sparkline em SVG inline — sem chart lib.

## Proibido
Zero número na tela sem `fonte` + `timestamp`; projeção com `~` e `*estimativa`. Estado
derivado (ok/subdimensionado/superdimensionado) só com amostra suficiente — abaixo do mínimo,
`observação insuficiente`.
