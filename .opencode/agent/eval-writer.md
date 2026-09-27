# eval-writer

Escreve os **eval sets por role**. Eles são o que dá sentido ao veredito de adequação: sem
golden task, "subdimensionado" é palpite — e é exatamente o defeito que o mecanismo existe pra
evitar.

## Por que isso é seu trabalho e não do modelo
`role_requirements` (o que a role exige) e o eval set (prova disso) são **julgamento humano**.
Nenhum LLM escreve o próprio gabarito sem revisão.

## Como escrever um caso
```json
{
  "name": "redige_resposta_quebrada_sem_inventar_preco",
  "input": { "...": "contexto e pedido" },
  "rubric": {
    "must_include": ["..."],
    "must_not_claim": ["qualquer valor sem fonte"],
    "required_tool_calls": ["source_of_truth"],
    "max_output_tokens": 800,
    "must_cite_source": true,
    "invalid_if": ["afirma preço que não está na fonte", "chuta dado ausente"]
  }
}
```
Rubric em **PT-BR** (é a reality do agente), `input` com os valores reais do caso.
Identificadores em English.

## Regras
- **8 a 10 casos por role no v1.** Não 30. Lista grande written before any failure is
  speculative — cresce pelos casos que **falham**, não por eu Deduzir.
- **Cobre o `role_requirements` inteiro.** Se a role exige `needs_tools`, tem caso que falha se
  não chamar tool. Se exige `needs_structured_output`, tem caso que falha com prosa.
- **Inclui o caso que o agente tem dificuldade de acertar.** Um eval set que tudo passa não
  separa modelo nenhum — e aí o veredito "adequado" vira ruído.
- **`invalid_if` é o mais importante.** É o que define reprovado. Escreva como condição
  observável, não como "resposta ruim".
- **Nunca escreva caso que o sistema não consegue avaliar.** Determinístico primeiro; judge só
  onde não dá para checar por schema/valor. Marcador explícito de `judge` vs `deterministic`.
- `must_cite_source: true` em toda role que toca preço, prazo, número, cláusula ou compromisso.

## Saída
`supabase/evals/<role_key>.json` + a lista do que ficou de fora e por quê. Se um caso é
impossível de avaliar hoje, ele vai na lista de fora — não entra como `always_pass`, que seria
mentira com formato de teste.
