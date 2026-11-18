---
name: sabor-da-maria-mcp
title: Sabor da Maria — MCP Memory & Recipe Tools
category: productivity
version: 2
author: hermes
license: MIT
description: 'Use when registering facts or approving recipes via MCP.'
metadata:
  hermes:
    tags: [dona-maria, mcp, memoria, receitas, sabor-da-maria]
    related_skills: []
---

# Sabor da Maria — MCP Memory & Recipe Tools

## When to Use

Load this skill at the start of any session within the Sabor da Maria project that involves:
- Registering facts about Dona Maria's kitchen (equipment, skills, preferences, restrictions, decisions)
- Searching and suggesting recipes based on her ingredients and pantry
- Approving and persisting recipes to memory

It works alongside the always-loaded HERMES.md project context.

## Part 1: Registering Facts

### `valor_json` — prefer native JSON values

The tool now accepts **native JSON types** directly, not just serialized strings. Follow this preference order:

| What you want | Correct `valor_json` |
|---|---|
| Boolean true | `true` (Python bool) |
| Boolean false | `false` (Python bool) |
| Number 4 | `4` (Python int) |
| Dict `{"bocas": 4}` | `{"bocas": 4}` (Python dict) |
| List `["facas"]` | `["facas"]` (Python list) |
| Plain text `"sem forno"` | `"\"sem forno\""` (JSON-serialized string) |

Do NOT serialize booleans, numbers, objects, or lists as `json.dumps()` strings — pass them as native Python types. The tool description says "Prefira JSON NATIVO: false, true, 4, {"bocas":4}, ou uma lista."

### Batch multiple facts with `memoria_registrar_lote`

When Dona Maria provides several facts in the same message (e.g. two equipment items, or equipment + skill), call `memoria_registrar_lote` once with ALL facts and a single `revisao_esperada`. This is the PREFERRED path — the tool description marks it as such. It's an all-or-nothing transaction.

Single facts still use `memoria_registrar`.

### Workflow for N facts

1. `memoria_consultar()` (no grupo filter) → gets current `revisao` and all existing facts
2. If 2+ facts from the same message: call `memoria_registrar_lote(fatos=[...], revisao_esperada=<revisao>)`
3. If 1 fact: call `memoria_registrar(revisao_esperada=<revisao>, ...)`
4. On success, use the returned `revisao` for the next write batch
5. **Never** send parallel writes with the same `revisao_esperada` — only the first commits

### Handling conflicts and corrections

- `corrige_anterior=true`: use when the user explicitly corrects a previously registered fact on the same `chave`. Do not ask for re-confirmation if the correction is clear.
- `revisao_desatualizada` error: reconcile `fatos_atuais` from the error response, then retry with `revisao_atual`
- Conflict is RECOVERABLE: do not wait for cooldown or declare the server dead — just re-read and retry
- To revoke a fact: `status="revogado"`, `valor_json=null`, with explicit evidence quoting the user

### Tool naming

Tool names are full MCP paths. Load schemas with `tool_describe(names=[...])` before first use. O gateway expoe apenas 15 das 19 ferramentas do server. As 4 abaixo NAO estao disponiveis como MCP e **subprocess MCP isolado tambem nao funciona** (o dict `_SEARCHES` e processo-local):
- `mcp__dona_maria_memoria__receita_ler_fonte` — indisponivel
- `mcp__dona_maria_memoria__receita_conferir_execucao` — indisponivel
- `mcp__dona_maria_memoria__receita_aprovar` — indisponivel
- `mcp__dona_maria_memoria__cardapio_adicionar` — indisponivel

A unica forma de acessa-las e via `dona_maria_pesquisar` (registrada pelo plugin `dona-maria-receitas` no runtime do coordenador).

Ferramentas CONFIRMADAS como disponiveis:
- `mcp__dona_maria_memoria__memoria_consultar`
- `mcp__dona_maria_memoria__memoria_registrar`
- `mcp__dona_maria_memoria__memoria_registrar_lote`
- `mcp__dona_maria_memoria__memoria_historico`
- `mcp__dona_maria_memoria__web_search`
- `mcp__dona_maria_memoria__despensa_consultar`
- `mcp__dona_maria_memoria__despensa_verificar_receita`
- `mcp__dona_maria_memoria__compra_cotar`
- `mcp__dona_maria_memoria__compra_autorizar`
- `mcp__dona_maria_memoria__receita_apresentar`
- `mcp__dona_maria_memoria__receita_verificar_producao`
- `mcp__dona_maria_memoria__receita_precificar`
- `mcp__dona_maria_memoria__receita_consultar_fluxo`
- `mcp__dona_maria_memoria__cardapio_consultar`
- `mcp__dona_maria_memoria__medidas_consultar`

## Part 2: Recipe Workflow

Siga a skill `dona-maria-receita` em `/home/opc/.hermes/skills/dona-maria-receita/SKILL.md` e os contratos de `/home/opc/dona-maria/src/HERMES.md`.

- O Hermes propõe uma receita real por iniciativa própria no início do atendimento ou com estoque disponível e sem receita em andamento. Consulte a despensa e inicie a pesquisa, sem pedir tipo de prato, ingrediente ou ideia à usuária. Recusa da sugestão inicia outra pesquisa. Respeite pedidos expressos de apenas consultar/manter dados ou de não sugerir.
- O aceite inicial da receita é solicitado uma única vez. Preserve a evidência da sessão e retome com `receita_consultar_fluxo`; não reapresente para obter outro aceite após falha ou delegação.
- Depois do aceite, confira automaticamente preparo, habilidades, utensílios e equipamentos na memória com `receita_conferir_execucao` e `receita_verificar_producao`. Essas ferramentas consultam fatos confirmados de habilidades. Pergunte apenas requisitos desconhecidos e registre respostas reais antes de revalidar.
- Produção validada inicia automaticamente o especialista de custos por `dona_maria_delegar(agente="custos", receita_id=...)`; recolha tarefas automáticas existentes sem duplicá-las. Não pergunte autorização para calcular. Fonte e estoque continuam obrigatórios.
- Mostre os três cenários reais e informe que escolher um autoriza incluí-lo no cardápio. Após a escolha, execute `cardapio_adicionar(precificacao_id, cenario, evidencia_usuario)` sem novo pedido de confirmação. `receita_aprovar` arquiva receita; não substitui a inclusão no cardápio.
- Use `dona_maria_delegar` e `dona_maria_agente_resultado`, sem `delegate_task` genérico, subprocess MCP paralelo ou escrita direta em banco para contornar etapas.
- A fonte deve ser lida antes da apresentação. Trecho de busca não comprova ingredientes e preparo completos. Se a ferramenta estiver indisponível, informe a pendência sem inventar conclusão ou repetir o aceite.

## Part 3: First-contact Protocol

At session start, before answering anything about:
- Menu planning or recipe suggestions
- Preferences, equipment, or skills
- Cooking capability or shopping decisions

Call `memoria_consultar()` (no grupo filter — gets ALL groups) to read current memory. The disk-persisted facts prevail over any conversation summary from earlier sessions.

When the user mentions a new fact about their kitchen (equipment, skill, limitation, preference, decision), register it immediately with evidence from the user's exact words. Use `memoria_registrar_lote` for 2+ facts in the same message, `memoria_registrar` for a single fact.

After initial recipe acceptance and before pricing or approving a purchase, cross-reference the recipe requirements against current equipment, skills, and operational facts. Unknown means ask; unavailable blocks approval until a confirmed workaround exists.
