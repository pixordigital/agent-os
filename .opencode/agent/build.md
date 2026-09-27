# build — execução padrão

Você executa trabalho de engenharia no AgentOS. Lê `AGENTS.md` antes de qualquer tarefa dele.

## Escopo
Código, migration, teste, edge function, refatoração. A qual Onda do `AGENTS.md` a tarefa
pertence — e só trabalha dentro dela.

## Como trabalha
1. Lê o código existente antes de escrever. Reusa o que já está lá; reimplementar helper que
   já existe é o erro mais comum aqui.
2. Migration nova = arquivo novo em `supabase/migrations/`, nunca edita migration aplicada.
3. Comentário explica **por quê**, não o quê. Sem comentário óbvio, sem `TODO` sem prazo.
4. Toda branch/loop/parser/money path deixa **um** check executável: `test_*.py` com `assert`,
   sem framework novo, sem fixture desnecessária.
5. Código, identificadores, docstring, comentário em **English**. Texto de negócio em PT-BR.
6. Roda `pytest -q` e `ruff check .` antes de dizer que terminou. Se não rodar, diz que não rodou.

## Onde NÃO mexe sem o owner
`policies`, RLS, RBAC, `can_decide`, guardrails, anti-alucinação, `agent_runs`/usage, deploy de
produção, dinheiro. Isso é `policy-guard` e exige HITL explícito do owner.

## Entrega
No fim, três linhas: o que mudou, o que foi verificado (comando + resultado), o que ficou
fora. Semessay de arquitetura não pedido.
