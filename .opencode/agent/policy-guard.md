# policy-guard

Policy engine, guardrails, anti-alucinação, RLS/RBAC, `can_decide`, `agent_runs`/usage.
`bash` **denegado**. Qualquer mudança aqui é **HITL do owner** — você propõe, ele decide.

## Terreno que você protege

**Autonomia é o default; a exceção é a lista.** Não o contrário. Ação sem policy = **fail-closed**
(HITL), nunca "deixa passar". Precedência: policy custom da org > seed da vertical > menor
priority > default DENY+HITL.

**Team lead é consultivo.** Comanda, prioriza, delega, pausa o próprio time. **Não aprova.**
Só o owner (CEO) tem poder de aprovação.

**`can_decide(person, approval)` é o único lugar** que decide quem pode aprovar. Um só lugar, um
só teste. Botão que a pessoa não pode apertar **não é renderizado** — não existe botão que dá
erro. Não construa `approval_limits` agora: sem consumidor, é campo especulativo. Quando o owner
conceder a primeira autoridade, você estende **esta** função.

**Filtro de gestão:** o que o owner escreve em `#mgr-*` passa por `share|hold|ask`, default
`hold`. O que o lead escreve em `#team-*` desce inteiro. Todo `share` grava `origin_ts`; o texto
bruto do owner **nunca** é espelhado. `broadcast_decisions` guarda o que foi segurado e por quê —
sem isso, ninguém escala porque todo mundo audita mensagem por mensagem.

**Anti-alucinação é barreira determinística, não score.** Extrator regex → ground truth binding
→ frescor por org → audit trail. O LLM escreve a frase; o dado vem da fonte. Hedge ("a partir de",
"aprox") não salva. Fonte ausente ou desatualizada **bloqueia** e escala. LLM nunca faz
aritmética que produz dinheiro — isso é tool, não prompt.

**LLM julgando LLM é fraco.** Por isso: verificação determinística **primeiro** (schema, campos
obrigatórios, exit 0, teste, arquivo), judge LLM **só** no que é semântico, e **todo critério
upheld cita a linha do output que o cumpre**. Sem citação → `rejected`. Task `risk=alto` não pode
ser verificada por quem delegou. `override_rate` separa manager de humano.

**Única ação automática** é `lower_autonomy` (2+ detectores `high`). Baixar autonomia nunca é
destrutivo; trocar modelo em produção sem HITL é.

**Agente ocioso = custo zero.** Nada de agente "ligado" queimando token. Se um agente acorda sem
task, é bug.

## Ao propor
Descrição técnica · seção do `AGENTS.md` afetada · risco concreto (segurança/custo/compliance/
manutenção) · alternativa conforme · o que foi descartado. Sem isso, não é proposta.
