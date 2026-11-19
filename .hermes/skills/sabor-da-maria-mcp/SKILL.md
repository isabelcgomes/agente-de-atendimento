---
name: sabor-da-maria-mcp
description: Consultar e registrar fatos da cozinha e suas evidências pelo MCP do Sabor da Maria.
license: MIT
metadata:
  title: Sabor da Maria — MCP Memory & Recipe Tools
  category: productivity
  version: 2
  author: hermes
  hermes:
    tags: [dona-maria, mcp, memoria, receitas, sabor-da-maria]
    related_skills: []
---

# Memória MCP

Siga o fluxo de `/home/opc/agente-de-atendimento/dona-maria/src/HERMES.md`. Use esta skill para memória; receitas e preços seguem `dona-maria-receita`.

- Consulte `memoria_consultar` sem filtro de grupo para obter fatos atuais e revisão antes de orientar sobre cozinha, capacidade ou compras. Reutilize consulta atual; memória persistida prevalece sobre resumos antigos. Fato ausente, desconhecido ou revogado não confirma disponibilidade.
- Registre declarações novas com evidência literal da usuária na sessão: um fato usa `memoria_registrar`; vários da mesma mensagem usam `memoria_registrar_lote(fatos, revisao_esperada)`, atomicamente. Preserve limitações, chaves existentes e correções explícitas (`corrige_anterior=true`).
- `valor_json`: booleanos, números, objetos e listas nativos; texto como string JSON serializada. Revogação usa `status="revogado"`, `valor_json=null`; desconhecido também exige null. Chaves: snake_case sem acentos, até 100 caracteres.
- Escritas são sequenciais com a revisão retornada. Em `revisao_desatualizada`, reconcilie `fatos_atuais` e tente com `revisao_atual`; lote rejeitado não gravou parcialmente. Não invente evidência ou repita argumentos desatualizados.
- Descubra nomes/esquemas atuais por `tool_search`/`tool_describe`, com prefixo `mcp__dona_maria_memoria__`. Não suponha disponibilidade nem contorne ferramenta ausente com subprocessos ou SQLite. Só confirme gravação após sucesso.
- Não salve receitas por memória genérica: arquivo usa `receita_aprovar`; inclusão com preço usa `cardapio_adicionar`, com autorizações distintas descritas no HERMES.md. Fatos financeiros não alteram saldo operacional.
