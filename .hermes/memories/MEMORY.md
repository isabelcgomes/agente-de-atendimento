Sabor da Maria: /home/opc/dona-maria. Regras em HERMES.md; MCP dona_maria_memoria em src/. Plugin dona-maria-receitas instalado (ferramentas: dona_maria_delegar, agente_resultado, pesquisar, calcular — NAO MCP, nao aparecem em tool_search). delegate_task com papeis pesquisa.md/custos.md. Fallback SQLite direto para precificacao/cardapio.
§
MCP receita_apresentar valida fonte_url contra o pesquisa_id da MCP web_search — a URL precisa estar nos resultados daquela pesquisa para ser aceita.
§
MCP receita_aprovar exige aprovacao explicita para salvar ('sim, pode salvar') — 'gostei' ou elogio nao sao aceitos como evidencia.
§
web_extract falha se o backend configurado for ddgs (search-only). Alternativa: browser_exec ou configurar extract_backend para firecrawl/tavily/exa.
§
MCP receita_apresentar: fonte_url deve ser uma URL retornada pelo MCP web_search associado ao pesquisa_id, nao de subagente delegate_task.
§
MCP receita_verificar_producao: para panelas (campo=tipos), usar igual com array simples ex: ["ferro_esmaltado","aco_inox"], nao objeto {"tipos":[...]}, para validar.
§
Custos operacionais R$20-30/porcao fixo (nao por lote). Precificacao = (CMV/0,90 + custo_op_porcao) / (1 - margem). 3 cenarios 10%, 20%, 30%. Sempre incluir custos operacionais na estimativa.
§
Sabor da Maria fluxo receita completo documentado na skill dona-maria-receita. Molho de tomate: 1g = 1ml autorizado (revisao 32-33 memoria). Leite: 1g = 1ml idem. despensa_verificar_receita nao converte massa-volume mesmo com registro na memoria — adaptar receitas para usar ingredientes em unidade compativel com o estoque. oleo_de_soja no estoque em L.
§
dona_maria_memoria MCP: memoria_registrar_lote valida evidencia contra banco state.db. Evidencia deve ser substring de uma unica mensagem role=user na sessao. Parafraseado ou concatenacao de duas mensagens gera declaracao_real_necessaria. O campo sessao no payload deve bater com o session_id que o Hermes usa no banco.
§
Dona Maria exige: sempre usar subagente de pesquisa (delegate_task com papel pesquisa.md) para pesquisar receitas na web — nunca sugerir receita da própria memória ou com web_search direto.