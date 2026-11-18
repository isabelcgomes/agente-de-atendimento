"""Projeções de contexto: dados úteis sem histórico ou listas duplicadas."""


def despensa_curta(data):
    return {'orcamento': data.get('orcamento', {}),
            'estoque': [{k: row[k] for k in ['ingrediente','quantidade','unidade'] if k in row}
                        for row in data.get('estoque', [])]}


def busca_curta(data):
    hits=[]
    for row in data.get('resultado_web',{}).get('data',{}).get('web',[])[:5]:
        text=str(row.get('description',''))
        hits.append({'titulo':row.get('title',''),'url':row.get('url',''),
                     'trecho':text[:2400], 'truncado':len(text)>2400})
    return {'pesquisa_id':data.get('pesquisa_id'),'fontes':hits}


def fluxo_curto(data):
    result={k:data[k] for k in ['sucesso','erro','receita_id','etapa','aceite','memorizada','acao','precificacao_id','precificacao','prato_cardapio'] if k in data}
    recipe=data.get('receita',{})
    result['receita']={k:recipe[k] for k in ['nome','fonte_url','rendimento'] if k in recipe}
    if data.get('etapa') in ['normalizar_ingredientes','corrigir_ingredientes']:
        result['linhas_originais']=data.get('linhas_originais',[])
    if data.get('etapa')=='verificar_capacidade':
        result['receita']['modo_preparo']=recipe.get('modo_preparo',[])
        capacity=data.get('capacidade',{})
        result['capacidade']={k:capacity[k] for k in ['producao_validada','lote','pendencias','perguntas'] if k in capacity}
    if data.get('conferencia_execucao'):
        result['conferencia_execucao'] = data['conferencia_execucao']
        result['receita']['modo_preparo'] = recipe.get('modo_preparo', [])
    normalization=data.get('normalizacao',{})
    if normalization.get('pendencias'):
        result['pendencias']=normalization['pendencias']
    analysis=data.get('comparacao_receita')
    if analysis:
        result['despensa']={'orcamento':analysis.get('orcamento'),
            'ingredientes':[{k:row[k] for k in ['ingrediente','necessario','disponivel','faltante','unidade'] if k in row}
                            for row in analysis.get('ingredientes',[])],
            'pendencias':analysis.get('pendencias',[]),
            'viabilidade_orcamentaria':analysis.get('viabilidade_orcamentaria')}
    return result
