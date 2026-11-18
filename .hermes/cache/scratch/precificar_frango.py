#!/usr/bin/env python3
import json, sys, os, hashlib, uuid
sys.path.insert(0, '/home/opc/dona-maria/src')
os.chdir('/home/opc/dona-maria/src')
import memoria_agente as app
from memoria_agente import now
from decimal import Decimal

db = app.connect('dados/memoria_dona_maria.sqlite3')

ingredientes = [
    ("Manteiga sem sal", "30g", 0.60),
    ("Peito de frango", "330g", 5.28),
    ("Caldo SAZON Galinha", "1 sache", 0.50),
    ("Farinha de trigo", "30g", 0.09),
    ("Leite integral", "400ml", 1.40),
    ("Maca", "1 un", 0.80),
    ("Queijo parmesao ralado", "50g", 2.50),
    ("Azeite de oliva extra virgem", "15ml", 0.45),
    ("Tomate", "300g", 1.20),
    ("Alcaparras", "10g", 0.60),
]

cmv_total = sum(Decimal(str(c)) for _, _, c in ingredientes)
cmv_porcao = cmv_total / 6

# Custos operacionais R$25/porcao (faixa R$20-30)
custo_op_porcao = Decimal('25')
cmv_equilibrio = cmv_porcao / Decimal('0.90')

def d2f(v):
    return float(round(v, 2))

cenarios = []
for margem_pct in [10, 20, 30]:
    margem = Decimal(str(margem_pct)) / 100
    preco = (cmv_equilibrio + custo_op_porcao) / (1 - margem)
    receita = preco * Decimal('0.90')
    lucro = receita - cmv_porcao
    cenarios.append({
        'margem_alvo_sobre_venda_percentual': margem_pct,
        'preco_por_porcao_BRL': d2f(preco),
        'receita_liquida_por_porcao_BRL': d2f(receita),
        'lucro_estimado_por_porcao_BRL': d2f(lucro),
        'formula_preco': f'P = ({float(cmv_porcao):.2f}/0,90 + 25)/(1 - {margem_pct/100:.2f})'
    })

print('=== FRANGO AO MOLHO DE ALCAPARRAS ===')
print(f'CMV lote (6 porcoes): R$ {float(cmv_total):.2f}')
print(f'CMV por porcao: R$ {float(cmv_porcao):.2f}')
print(f'Custo operacional: R$ {float(custo_op_porcao):.2f}/porcao')
print()
for c in cenarios:
    print(f'Cenario {c["margem_alvo_sobre_venda_percentual"]}%: R$ {c["preco_por_porcao_BRL"]}/porcao (recebe R$ {c["receita_liquida_por_porcao_BRL"]}, lucro R$ {c["lucro_estimado_por_porcao_BRL"]})')

# Salva
precos_data = {
    'sucesso': True,
    'receita_id': 'manual-frango',
    'nome_receita': 'FRANGO AO MOLHO DE ALCAPARRAS',
    'lote_validado': '6 porcoes',
    'porcoes': 6,
    'CMV_lote_BRL': float(round(cmv_total, 2)),
    'CMV_por_porcao_BRL': float(round(cmv_porcao, 2)),
    'preco_minimo_por_porcao_BRL': float(round(cmv_equilibrio, 2)),
    'ingredientes': [{'ingrediente': i[0], 'quantidade_usada': i[1], 'unidade': '-',
                       'custo_usado_BRL': float(i[2])} for i in ingredientes],
    'cenarios': cenarios,
    'limites': 'Precos estimados com valores de mercado. Custos operacionais R$25/porcao.',
    'custo_operacional_porcao_BRL': float(custo_op_porcao)
}

prec_id = str(uuid.uuid4())
db.execute('INSERT INTO receita_precificacoes VALUES (?,?,?,?,?)',
           (prec_id, 'manual-frango', 'hermes', now(), json.dumps(precos_data, ensure_ascii=False)))

cenario = cenarios[1]
card_data = {
    'preco': cenario['preco_por_porcao_BRL'],
    'cenario': 2,
    'nome': 'FRANGO AO MOLHO DE ALCAPARRAS',
    'receita_id': 'manual-frango',
    'margem': 20
}
h = hashlib.sha256(json.dumps(card_data, sort_keys=True).encode()).hexdigest()
db.execute('INSERT INTO cardapio VALUES (?,?,?,?)',
           (h, prec_id, json.dumps(card_data), now()))
db.commit()

print()
print(f'Salvo no cardapio! Hash: {h}')
print(f'Prato: FRANGO AO MOLHO DE ALCAPARRAS - R$ {cenario["preco_por_porcao_BRL"]}/porcao')