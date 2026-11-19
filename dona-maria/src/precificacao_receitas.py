"""CMV por custo médio ponderado e cenários de delivery; sem escolher preço."""
import json
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import despensa_compras as pantry

CENT = Decimal('.01')
TAXA = Decimal('.10')


def money(value):
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), '.2f')


def ceiling(value):
    return value.quantize(CENT, rounding=ROUND_CEILING)


def cost_basis(db):
    """Cruza Despensa/Precos originais e acrescenta só compras efetivamente registradas."""
    prices = {pantry.key(pantry.canonical(x[0])):x for x in pantry.rows_xlsx(pantry.ROOT/'despensa_dona_maria.xlsx',2)}
    basis = {}
    for name, quantity, unit in pantry.rows_xlsx(pantry.ROOT/'despensa_dona_maria.xlsx',1):
        name = pantry.canonical(name)
        price = prices[pantry.key(name)]
        qty, normalized, _ = pantry.normalize(name,quantity,unit)
        bought, bought_unit, _ = pantry.normalize(name,pantry.number(price[1],True),price[2])
        if bought_unit != normalized: raise ValueError('Unidades incompatíveis nas abas para '+name)
        paid = pantry.number(price[3]).quantize(CENT)
        unit_cost = paid/bought
        basis[pantry.key(name)] = {'quantidade':qty,'custo':qty*unit_cost,
            'unidade':normalized, 'origens':[{
                'origem':'planilha: Despensa × Precos','quantidade_em_estoque':str(qty),
                'quantidade_comprada':str(bought),'unidade':normalized,
                'preco_total_pago_BRL':money(paid),'custo_unitario_BRL':str(unit_cost)}]}
    for purchase in db.execute('SELECT c.id,q.itens_json,q.frete_centavos FROM despensa_compras c JOIN despensa_cotacoes q ON q.id=c.cotacao_id ORDER BY c.registrado_em,c.id'):
        items=json.loads(purchase['itens_json'])
        subtotal=sum(i['valor_centavos'] for i in items)
        freight=purchase['frete_centavos']
        remaining=freight
        for index,item in enumerate(items):
            allocation=remaining if index==len(items)-1 else (int(Decimal(freight)*item['valor_centavos']/subtotal) if subtotal else 0)
            remaining-=allocation
            qty=pantry.number(item['quantidade'],True)
            cost=Decimal(item['valor_centavos']+allocation)/100
            entry=basis.setdefault(pantry.key(item['ingrediente']),{'quantidade':Decimal(0),'custo':Decimal(0),'unidade':item['unidade'],'origens':[]})
            if entry['unidade']!=item['unidade']: raise ValueError('Compra com unidade incompatível para '+item['ingrediente'])
            entry['quantidade']+=qty;entry['custo']+=cost
            entry['origens'].append({'origem':'compra_autorizada','compra_id':purchase['id'],
                'quantidade_comprada':str(qty),'unidade':item['unidade'],
                'preco_total_pago_BRL':pantry.brl(item['valor_centavos']),
                'frete_rateado_BRL':pantry.brl(allocation),'custo_unitario_BRL':str(cost/qty)})
    return basis


def calculate(db, analysis, porcoes):
    if type(porcoes) is not int or not 1<=porcoes<=100000:
        raise ValueError('Informe o número inteiro positivo de porções reais do lote validado')
    if not analysis['analise_completa'] or any(pantry.number(x['faltante'])>0 for x in analysis['ingredientes']):
        return {'sucesso':False,'erro':'ingredientes_nao_garantidos',
                'acao':'Esclareça quantidades e garanta todos os ingredientes. Cotação ou intenção de comprar não é estoque; compra exige autorização.', 'comparacao_receita':analysis}
    bases=cost_basis(db)
    rows=[];total=Decimal(0)
    for item in analysis['ingredientes']:
        if item.get('recurso_operacional') or pantry.number(item['necessario']) == 0:
            continue
        base=bases.get(pantry.key(item['ingrediente']))
        stock=pantry.stock(db,item['ingrediente'])
        if not base or base['quantidade']<=0 or not stock or base['unidade']!=item['unidade'] or base['quantidade']!=pantry.number(stock['quantidade']):
            return {'sucesso':False,'erro':'base_de_custo_inconsistente','ingrediente':item['ingrediente'],
                    'acao':'Reconcilie a origem do estoque e seu custo; não atribua custo zero nem invente preço.'}
        qty=pantry.number(item['necessario'],True)
        unit=base['custo']/base['quantidade']
        cost=qty*unit;total+=cost
        rows.append({'ingrediente':item['ingrediente'],'quantidade_usada':str(qty),'unidade':item['unidade'],
                     'custo_unitario_BRL':str(unit),'custo_usado_BRL':money(cost),
                     'custo_usado_sem_arredondar_BRL':str(cost),'origens_custo':base['origens']})
    portion=total/porcoes
    minimum=ceiling(portion/(1-TAXA))
    scenarios=[]
    for margin in [Decimal('.10'),Decimal('.20'),Decimal('.30')]:
        price=ceiling(portion/(1-TAXA-margin))
        net=(1-TAXA)*price;profit=net-portion
        scenarios.append({'margem_alvo_sobre_venda_percentual':str(margin*100),
            'preco_por_porcao_BRL':money(price),'receita_liquida_por_porcao_BRL':money(net),
            'lucro_estimado_por_porcao_BRL':money(profit),'lucro_estimado_lote_BRL':money(profit*porcoes),
            'formula_preco':'CMV por porção ÷ (0,90 − '+str(margin).replace('.',',')+')',
            'formula_lucro':'0,90 × '+money(price)+' − '+str(portion)})
    return {'sucesso':True,'porcoes':porcoes,'ingredientes':rows,'CMV_lote_BRL':money(total),
            'CMV_lote_sem_arredondar_BRL':str(total),'CMV_por_porcao_BRL':money(portion),
            'CMV_por_porcao_sem_arredondar_BRL':str(portion),'taxa_delivery_percentual':'10',
            'preco_minimo_por_porcao_BRL':money(minimum), 'preco_minimo_lote_BRL':money(ceiling(total/(1-TAXA))),
            'cenarios':scenarios,'preco_escolhido':None,
            'metodo':'Custo unitário inicial = preço total pago ÷ quantidade comprada normalizada das abas Precos/Despensa. Compras complementares autorizadas entram no custo médio ponderado, com frete rateado. CMV = soma de quantidade usada × custo unitário, sem arredondamentos intermediários.',
            'limites':'Lucro estimado após ingredientes e taxa de delivery. Água de preparo é recurso operacional e não uma compra de água engarrafada. Não inclui água de serviço, mão de obra, energia/gás, embalagens de entrega, impostos ou outras despesas não informadas. Custo de compra de embalagem inteira é diferente do custo da fração usada.',
            'acao':'Explique cada custo e as fórmulas P mínimo = CMV/0,90 e lucro = 0,90×P−CMV. Mostre os três cenários e pergunte qual preço Dona Maria quer adotar. Não escolha por ela. Calcular não baixa estoque, não altera orçamento nem define preço.'}
