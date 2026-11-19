---
name: dona-maria-receita
description: Coordenar sugestões de receitas, validação da cozinha, pesquisa de faltantes e custos do Sabor da Maria.
---

# Receitas e preços

Siga `/home/opc/agente-de-atendimento/dona-maria/src/HERMES.md`, fonte única do fluxo, autorizações e continuidade. O coordenador conversa; especialistas executam seus contratos autenticados.

- Consulte memória/despensa atuais e recolha tarefas automáticas antes de delegar. Pesquisa usa `dona_maria_delegar(agente="pesquisa")`; custos usam `agente="custos", receita_id=...`; preços dos faltantes usam `agente="pesquisa", finalidade="precos", receita_id=...`. Resultados vêm de `dona_maria_agente_resultado`, nunca de resumo livre.
- Apresente uma receita completa somente com `receita_apresentar.fonte_conferida=true`. Respeite a proibição de peito de frango moído. Links com falha são descartados; continue fontes e buscas até validar ou esgotar o prazo.
- Interesse aciona conferência da fonte/memória e `receita_verificar_producao`; esclareça apenas dados faltantes e revalide, preservando aceite da mesma versão.
- Ordem: capacidade → comparação em lote → pesquisa de TODOS os faltantes → CMV e três cenários → escolha explícita → inclusão. Recolha tarefas existentes sem duplicar; não peça autorização extra para calcular ou salvar o cenário escolhido.
- A conversão é automática pela tabela `/home/opc/agente-de-atendimento/dona-maria/src/conversoes_medidas_dona_maria.json`. Não copie fatores para o prompt nem consulte medidas individualmente. Preserve quantidades/embalagens explícitas e convenções cadastradas; não invente rendimento, densidade ou custo zero. Resolva exceções concretas sem questionário de conversão.
- Obtenha estoque, preços e saldo pelas ferramentas, não por perguntas sobre dados disponíveis. Informe frete desconhecido como pendência. Substituições e compras exigem os aceites específicos do fluxo; não transforme “gostei” em autorização de compra ou escolha de preço.
- Mostre somente valores dos recibos determinísticos; não contorne o plugin com cálculos livres, arquivos ou banco. Retome `receita_consultar_fluxo` após falha ou reinício.
