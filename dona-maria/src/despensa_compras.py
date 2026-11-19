"""Estoque operacional e compras autorizadas. Valores monetários em centavos."""
import json
import re
import sqlite3
import unicodedata
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def key(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFD', unicodedata.normalize('NFKC', str(value)).casefold())
                            if not unicodedata.combining(c)).split())


def number(value, positive=False):
    if isinstance(value, bool):
        raise ValueError('Booleano não representa quantidade ou preço')
    try:
        text = str(value).strip().replace(',', '.')
        for symbol, fraction in conversion_data()['normalizacao_entrada']['quantidades']['unicode_fracoes'].items():
            text = text.replace(symbol, ' '+fraction)
        match = re.fullmatch(r'(?:(\d+)\s+)?(\d+)\s*/\s*(\d+)', text.strip())
        d = (Decimal(match[1] or 0)+Decimal(match[2])/Decimal(match[3])) if match else Decimal(text)
    except (InvalidOperation, ArithmeticError):
        raise ValueError('Número inválido; use decimal sem separador de milhar') from None
    if not d.is_finite() or d < 0 or (positive and d == 0) or (d and d.adjusted() > 12):
        raise ValueError('Quantidade deve ser positiva; preço não pode ser negativo ou infinito')
    return d


def cents(value):
    d = number(value)
    if d != d.quantize(Decimal('.01')):
        raise ValueError('Preço deve ter no máximo duas casas decimais')
    return int(d * 100)


def brl(value):
    return format(Decimal(value) / 100, '.2f')


def conversion_data():
    path=ROOT / 'conversoes_medidas_dona_maria.json'
    metadata=path.stat()
    return _conversion_data(str(path),metadata.st_mtime_ns,metadata.st_size)


@lru_cache(maxsize=2)
def _conversion_data(path, modified, size):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data.get('convencoes_usuario'), dict):
        return data
    # Formato 2: adaptar as tabelas, preservando todos os fatores do arquivo.
    conventions = [{'ingrediente':name,'medida_origem':measure,'unidade_destino':unit,
                    'fator':factor,'status':'convencao_usuario'}
                   for name,measure,unit,factor in data['convencoes_usuario']['fatores']]
    liquids = data['liquidos']
    data['conversoes_liquidos'] = [{'medida_origem':measure,'unidade_destino':'L','fator':factor,
        'ingredientes_permitidos':liquids['ingredientes'],'status':'convencao'}
        for measure,factor in liquids['fatores_para_L'].items()]
    data['convencoes_usuario'] = conventions
    data['equivalencias_solidos'] = []
    # Razões declaradas no próprio formato 2 para o perfil das referências.
    ratios = {'xicara_cha':(1,1),'colher_sopa':(1,16),'colher_cha':(1,48),
              'meia_xicara_cha':(1,2),'terco_xicara_cha':(1,3),'quarto_xicara_cha':(1,4)}
    for name,state,measure,factor in data['referencias_solidos']['fatores']:
        base_num,base_den = ratios[measure]
        derived = [{'medida_origem':target,'unidade_destino':'g','status':'estimativa_derivada',
                    'numerador':str(Decimal(str(factor))*num*base_den),'denominador':den*base_num}
                   for target,(num,den) in ratios.items() if target != measure]
        data['equivalencias_solidos'].append({'ingrediente':name,'estado':state,'medida_origem':measure,
            'unidade_destino':'g','fator':factor,'status':'estimativa_documentada','fatores_derivados':derived})
    data['ingredientes_despensa'] = []
    data['normalizacao_embalagens'] = []
    for source,names in data['estoque']['ingredientes_por_unidade_original'].items():
        for name in names:
            specific = next((r for r in conventions if r['ingrediente']==name and r['medida_origem']==source),None)
            exact = data['conversoes_exatas'].get(source) or data['estoque']['embalagens'].get(source)
            rule = ({'unidade_destino':specific['unidade_destino'],'fator':specific['fator'],'status':'convencao_usuario'}
                    if specific else {'unidade_destino':exact['destino'],'fator':exact['fator'],
                                      'status':'exata' if source in data['conversoes_exatas'] else 'conteudo_nominal'})
            data['ingredientes_despensa'].append({'ingrediente':name,'normalizacao_estoque':rule})
            data['aliases_ingredientes'].setdefault(name,name)
            if source in data['estoque']['embalagens']:
                data['normalizacao_embalagens'].append(dict(rule,ingrediente=name,unidade_origem=source,
                    medidas_receita_equivalentes=data['estoque']['aliases_embalagens'].get(name,[])))
    for row in conventions+data['equivalencias_solidos']:
        if row['ingrediente'] != '*':
            data['aliases_ingredientes'].setdefault(row['ingrediente'],row['ingrediente'])
    return data


def rows_xlsx(file, sheet):
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(file) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(x.itertext()) for x in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si', ns)]
        rows = []
        for row in ET.fromstring(z.read('xl/worksheets/sheet%d.xml' % sheet)).findall('.//m:sheetData/m:row', ns)[1:]:
            values = []
            for cell in row.findall('m:c', ns):
                v = cell.find('m:v', ns); inline = cell.find('m:is', ns)
                value = v.text if v is not None else ''.join(inline.itertext()) if inline is not None else ''
                values.append(strings[int(value)] if cell.get('t') == 's' else value)
            rows.append(values)
        return rows


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS despensa_controle(
        id INTEGER PRIMARY KEY CHECK(id=1), orcamento_inicial_centavos INTEGER NOT NULL,
        saldo_centavos INTEGER NOT NULL CHECK(saldo_centavos>=0), importado_em TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS despensa_estoque(
        ingrediente_id TEXT PRIMARY KEY, ingrediente TEXT NOT NULL, unidade TEXT NOT NULL,
        quantidade TEXT NOT NULL, custo_total_centavos INTEGER NOT NULL CHECK(custo_total_centavos>=0));
      CREATE TABLE IF NOT EXISTS despensa_cotacoes(
        id TEXT PRIMARY KEY, itens_json TEXT NOT NULL, frete_centavos INTEGER NOT NULL,
        total_centavos INTEGER NOT NULL, criado_em TEXT NOT NULL, expira_em TEXT NOT NULL,
        estado TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS despensa_compras(
        id TEXT PRIMARY KEY, cotacao_id TEXT NOT NULL UNIQUE REFERENCES despensa_cotacoes(id),
        total_centavos INTEGER NOT NULL, saldo_apos_centavos INTEGER NOT NULL,
        evidencia_usuario TEXT NOT NULL, sessao TEXT NOT NULL, registrado_em TEXT NOT NULL);
    ''')
    if db.execute('SELECT 1 FROM despensa_controle WHERE id=1').fetchone():
        return
    data = conversion_data()
    specs = {x['ingrediente']: x['normalizacao_estoque'] for x in data['ingredientes_despensa']}
    prices = {x[0]: x for x in rows_xlsx(ROOT / 'despensa_dona_maria.xlsx', 2)}
    db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute('SELECT 1 FROM despensa_controle WHERE id=1').fetchone():
            return
        for name, quantity, unit in rows_xlsx(ROOT / 'despensa_dona_maria.xlsx', 1):
            rule = specs[name]
            factor = number(rule['fator'], True)
            qty = number(quantity) * factor
            price = prices[name]
            if price[2] != unit:
                raise ValueError('Unidades divergentes entre as abas: ' + name)
            # Limpar a imprecisão binária presente no XLSX, como 79.90000000000001.
            paid = number(price[3]).quantize(Decimal('.01'))
            bought = number(price[1], True) * factor
            cost = int((qty / bought * paid * 100).quantize(Decimal('1')))
            db.execute('INSERT INTO despensa_estoque VALUES(?,?,?,?,?)',
                       (key(name), name, rule['unidade_destino'], str(qty), cost))
        db.execute('INSERT INTO despensa_controle VALUES(1,8000,8000,?)', (timestamp(),))
        db.commit()
    finally:
        if db.in_transaction:
            db.rollback()


def canonical(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise ValueError('Ingrediente deve ter de 1 a 160 caracteres')
    data = conversion_data()
    aliases = {key(k): v for k, v in data['aliases_ingredientes'].items()}
    label = key(name)
    if label in aliases:
        return aliases[label]
    # Somente o mesmo ingrediente/corte. Variedades diferentes não são sinônimos.
    same = {
        'carne moida':'Carne moída (patinho)', 'carne moida crua':'Carne moída (patinho)',
        'file de peito de frango':'Peito de frango', 'files de peito de frango':'Peito de frango',
        'peito de frango em files':'Peito de frango', 'peito de frango em cubos':'Peito de frango',
        'peito de frango moido':'Peito de frango',
        'arroz':'Arroz branco tipo 1', 'queijo ralado':'Queijo parmesão ralado',
        'oleo':'Óleo de soja', 'oleo vegetal':'Óleo de soja', 'leite':'Leite integral',
        'agua':'Água', 'agua quente':'Água', 'agua fervente':'Água', 'agua fria':'Água',
    }
    if label in same:
        return same[label]
    plain = re.sub(r'(?:\s+(?:medi[oa]s?|grandes?|pequen[oa]s?|picad[oa]s?|amassad[oa]s?|ralad[oa]s?|descascad[oa]s?|em cubos|cru[oa]s?))+$', '', label)
    for candidate in [plain, plain[:-1] if plain.endswith('s') else plain]:
        if candidate in aliases or candidate in same:
            return aliases.get(candidate, same.get(candidate))
    return name.strip()


AMOUNT = r'(?:\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:[.,]\d+)?|[¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞])'
CONTAINERS = r'\b(?:caixinhas?|caixas?|latas?|potes?|pacotes?|embalagens?|garrafas?|sach[eê]s?)\b'


def metric_pattern():
    aliases = conversion_data()['aliases_medidas']
    terms = {name for unit in ['kg','mg','g','ml','L'] for name in [unit,*aliases[unit]]}
    return '('+'|'.join(re.escape(term) for term in sorted(terms,key=len,reverse=True))+r')\b'


def measure_input(name, quantity, unit):
    """Separa a quantidade da medida uma vez, antes de consultar os fatores."""
    text = str(unit).strip()
    supplied = number(quantity) if quantity not in [None, ''] else Decimal(1)
    metric = re.match(r'^\s*('+AMOUNT+r')\s*'+metric_pattern(), text, re.I)
    if metric:
        amount = number(metric[1])
        if supplied not in [Decimal(1), amount]:
            raise ValueError('Quantidade diverge da medida informada.')
        return amount, metric[2]
    head = re.match(r'^\s*('+AMOUNT+r')\s*([^\W\d_].*)$', text, re.I)
    if head:
        amount = number(head[1])
        if supplied not in [Decimal(1), amount]:
            raise ValueError('Quantidade diverge da medida informada.')
        supplied, text = amount, head[2].strip()
    contents = list(re.finditer(r'('+AMOUNT+r')\s*'+metric_pattern(), text, re.I))
    if re.search(CONTAINERS, text, re.I) and len(contents) == 1:
        return supplied*number(contents[0][1]), contents[0][2]
    names = {key(alias): canonical_unit for canonical_unit, aliases in conversion_data()['aliases_medidas'].items()
             for alias in [canonical_unit, *aliases]}
    measure = names.get(key(text), text)
    if quantity in [None, ''] and not head and measure != 'a_gosto':
        raise ValueError('Quantidade ausente na receita.')
    if canonical(text) == canonical(name) and any(key(row['ingrediente']) == key(canonical(name))
            and row['medida_origem'] == 'un' for row in conversion_data()['convencoes_usuario']):
        measure = 'un'
    return supplied, measure


def recipe_measure(line, name):
    """Quantidades publicadas e convenções conhecidas prevalecem sobre a transcrição do modelo."""
    text = str(line).strip()
    words = {'uma':'1','um':'1','duas':'2','dois':'2','três':'3','tres':'3','quatro':'4',
             'cinco':'5','seis':'6','meia':'0.5','meio':'0.5'}
    first = re.match(r'^(\w+)\b', text)
    if first and first[1].casefold() in words:
        text = words[first[1].casefold()]+text[first.end():]
    head = re.match(r'^('+AMOUNT+r')\s*(.*)$', text, re.I)
    metrics = list(re.finditer(r'('+AMOUNT+r')\s*'+metric_pattern(), text, re.I))
    if len(metrics) == 1:
        amount = number(metrics[0][1])
        # Um conteúdo por embalagem é multiplicado pela contagem, nunca duas vezes.
        if head and re.match(CONTAINERS, head[2], re.I) and 'total' not in key(text):
            amount *= number(head[1])
        return amount, metrics[0][2]
    if len(metrics) > 1:
        label = key(canonical(name))
        anchor = next((term for term in ['parmesao','mussarela','ricota','basmati','frango','jamaica','reino']
                       if term in label), label.split()[0])
        matches = [value for i,value in enumerate(metrics) if re.search(r'\b'+re.escape(anchor)+r's?\b',
                   key(text[value.end():metrics[i+1].start() if i+1 < len(metrics) else len(text)]))]
        return (number(matches[0][1]),matches[0][2]) if len(matches) == 1 else None
    if 'a gosto' in key(text):
        return Decimal(1), 'a_gosto'
    if not head:
        return None
    rest = head[2].strip()
    aliases = [(alias, measure) for measure, values in conversion_data()['aliases_medidas'].items()
               for alias in [measure, *values]]
    for alias, measure in sorted(aliases, key=lambda item:len(item[0]), reverse=True):
        if re.match(r'^'+re.escape(key(alias))+r'(?:\s|$)', key(rest)):
            return number(head[1]), measure
    if canonical(re.sub(r'^de\s+', '', rest,flags=re.I)) == canonical(name) and any(key(row['ingrediente']) == key(canonical(name)) and row['medida_origem'] == 'un'
           for row in conversion_data()['convencoes_usuario']):
        return number(head[1]), 'un'
    return None


def normalize(name, quantity, unit, state=None):
    """Converte usando o arquivo do projeto, sem massa -> volume ou densidade genérica."""
    data = conversion_data()
    name = canonical(name)
    qty, measure = measure_input(name, quantity, unit)
    aliases = {key(alias):base for base, names in data['aliases_medidas'].items() for alias in [base,*names]}
    measure = aliases.get(key(measure),measure)
    direct = {'kg': ('g', Decimal(1000)), 'g': ('g', Decimal(1)), 'mg': ('g', Decimal('.001')),
              'L': ('L', Decimal(1)), 'ml': ('L', Decimal('.001')), 'cl': ('L', Decimal('.01')),
              'dl': ('L', Decimal('.1'))}
    if data.get('conversoes_exatas'):
        direct = {source:(rule['destino'],Decimal(str(rule['fator'])))
                  for source,rule in data['conversoes_exatas'].items()
                  if source != 'un' or not any(key(row['ingrediente']) == key(name) and row['medida_origem']=='un'
                                              for row in data['convencoes_usuario'])}
    if measure in direct:
        destination, factor = direct[measure]
        return qty * factor, destination, 'exata'
    candidates = [r for r in data['convencoes_usuario'] if key(r['ingrediente']) == key(name) and r['medida_origem'] == measure]
    if not candidates:
        candidates = [r for r in data['normalizacao_embalagens']
                      if key(r.get('ingrediente', '')) == key(name) and
                      (r['unidade_origem'] == unit or measure in r.get('medidas_receita_equivalentes', []))]
    if not candidates:
        # As convenções genéricas são de MASSA; nunca atribuir gramas a líquidos.
        liquid = any(key(name) in [key(n) for n in r['ingredientes_permitidos']] for r in data['conversoes_liquidos'])
        if not liquid:
            candidates = [r for r in data['convencoes_usuario'] if r['ingrediente'] == '*' and r['medida_origem'] == measure]
    if not candidates:
        candidates = [r for r in data['conversoes_liquidos'] if key(name) in [key(n) for n in r['ingredientes_permitidos']] and r['medida_origem'] == measure]
    if not candidates:
        for row in data['equivalencias_solidos']:
            if key(row['ingrediente']) != key(name):
                continue
            if state and not set(re.findall(r'\w+', key(state))).issubset(set(re.findall(r'\w+', key(row['estado']).replace('_',' ')))):
                continue
            if row['medida_origem'] == measure:
                candidates.append(row)
            for derived in row['fatores_derivados']:
                if derived['medida_origem'] == measure:
                    candidates.append(dict(derived, fator=number(derived['numerador']) / number(derived['denominador'], True)))
    factors = {(str(r['fator']), r['unidade_destino']) for r in candidates}
    if len(factors) != 1:
        raise ValueError('Medida sem fator ou ambígua para ' + name + ': ' + str(unit) + '. Informe g/L ou estado da preparação.')
    row = candidates[0]
    if 'numerador' in row:
        return qty*number(row['numerador'])/number(row['denominador'],True), row['unidade_destino'], row['status']
    return qty * number(row['fator'], True), row['unidade_destino'], row['status']


def stock(db, name):
    return db.execute('SELECT * FROM despensa_estoque WHERE ingrediente_id=?', (key(canonical(name)),)).fetchone()


def budget(db):
    row = db.execute('SELECT * FROM despensa_controle WHERE id=1').fetchone()
    return {'moeda': 'BRL', 'orcamento_inicial': brl(row['orcamento_inicial_centavos']),
            'total_utilizado': brl(row['orcamento_inicial_centavos'] - row['saldo_centavos']),
            'saldo_disponivel': brl(row['saldo_centavos'])}


def consultar(db, resumido=False):
    db.execute('BEGIN')
    try:
        items = []
        for row in db.execute('SELECT * FROM despensa_estoque ORDER BY ingrediente'):
            qty = number(row['quantidade'])
            items.append({'ingrediente': row['ingrediente'], 'quantidade': str(qty), 'unidade': row['unidade'],
                          'custo_total_BRL': brl(row['custo_total_centavos']),
                          'custo_unitario_BRL': str(Decimal(row['custo_total_centavos']) / 100 / qty) if qty else None})
        if resumido:
            return {'sucesso':True,'orcamento':budget(db),
                    'estoque':[{k:row[k] for k in ['ingrediente','quantidade','unidade']} for row in items]}
        purchases = []
        for row in db.execute('SELECT c.*,q.itens_json FROM despensa_compras c JOIN despensa_cotacoes q ON q.id=c.cotacao_id ORDER BY c.registrado_em DESC LIMIT 20'):
            item = dict(row)
            item['itens'] = json.loads(item.pop('itens_json'))
            item['valor_utilizado_BRL'] = brl(item['total_centavos'])
            purchases.append(item)
        pending = []
        for row in db.execute("SELECT * FROM despensa_cotacoes WHERE estado='pendente' AND expira_em>? ORDER BY criado_em DESC LIMIT 20", (timestamp(),)):
            pending.append({'cotacao_id': row['id'], 'itens': json.loads(row['itens_json']),
                            'total_BRL': brl(row['total_centavos']), 'frete_BRL': brl(row['frete_centavos']),
                            'expira_em': row['expira_em'], 'reserva_saldo': False})
        return {'sucesso': True, 'orcamento': budget(db), 'estoque': items, 'compras_recentes': purchases,
                'cotacoes_pendentes': pending,
                'observacao': 'Estoque inicial mais compras autorizadas. Consumo de produção e reservas ainda não são baixados por este módulo.'}
    finally:
        db.rollback()


# Sugestões qualitativas; não são equivalências de quantidade nem substituições automáticas.
SUBSTITUTIONS = {
    'pinoli': [('Amêndoa fatiada', 'Oleaginosa diferente: aceitar uma adaptação de sabor e textura antes de substituir.')],
    'arroz basmati cru': [('Arroz branco tipo 1', 'Variedade diferente: usar somente após aceitar uma adaptação da receita.')],
    'arroz basmati': [('Arroz branco tipo 1', 'Variedade diferente: usar somente após aceitar uma adaptação da receita.')],
    'creme de leite': [('Leite integral', 'Avaliar versão de molho com leite; espessura e teor de gordura mudam.'),
                      ('Manteiga', 'Pode compor uma adaptação do molho com leite; não substitui creme sozinha.')],
    'azeite': [('Óleo de soja', 'Para refogar, avaliar o sabor e usar a quantidade da receita adaptada.')],
    'azeite de oliva extra virgem': [('Óleo de soja', 'Para refogar, avaliar o sabor da receita adaptada.')],
    'oleo de soja': [('Azeite de oliva extra virgem', 'Avaliar sabor e técnica da receita adaptada.')],
    'queijo parmesao ralado': [('Queijo mussarela', 'Avaliar mudança de sal, textura e gratinação; não é equivalente automático.')],
    'queijo mussarela': [('Queijo parmesão ralado', 'Avaliar textura, sal e função; não substitui a elasticidade da mussarela.')],
    'molho de tomate': [('Tomate', 'Preparar molho caseiro, validando tempo e rendimento.'), ('Cebola', 'Apoio ao molho caseiro.'), ('Alho', 'Apoio ao molho caseiro.')],
    'extrato de tomate': [('Tomate', 'Reduzir um molho caseiro se a receita permitir; concentração e rendimento mudam.')],
    'manteiga': [('Óleo de soja', 'Avaliar apenas preparos compatíveis; não converter gramas em litros.')],
}


def verificar(db, ingredientes, receita_id=None):
    if not isinstance(ingredientes, list) or not 1 <= len(ingredientes) <= 100:
        raise ValueError('Informe de 1 a 100 ingredientes com nome, quantidade e unidade')
    requirements = {}
    pending = []
    for item in ingredientes:
        name = canonical(item['ingrediente'])
        try:
            qty, unit, kind = normalize(name, item['quantidade'], item['unidade'], item.get('estado'))
            row = stock(db, name)
            if row and row['unidade'] != unit:
                raise ValueError('Unidade incompatível com estoque: massa e volume não podem ser cruzados')
            identity = (key(name), unit)
            if identity in requirements:
                requirements[identity]['quantidade'] += qty
            else:
                requirements[identity] = {'nome': name, 'quantidade': qty, 'unidade': unit, 'tipo': kind}
        except ValueError as exc:
            row = stock(db, name)
            pending.append({'ingrediente': name, 'erro': str(exc),
                            'quantidade_origem':str(item['quantidade']), 'medida_origem':item['unidade'],
                            'em_estoque':bool(row and number(row['quantidade']) > 0),
                            'disponivel':row['quantidade'] if row else '0',
                            'unidade_estoque':row['unidade'] if row else None})
    result = []
    for requirement in requirements.values():
        name = requirement['nome']; row = stock(db, name)
        resource = key(name) == 'agua'
        have = number(row['quantidade']) if row else Decimal(0)
        missing = Decimal(0) if resource else max(Decimal(0), requirement['quantidade'] - have)
        suggestions = []
        if missing:
            for option, condition in SUBSTITUTIONS.get(key(name), []):
                alternative = stock(db, option)
                if alternative and number(alternative['quantidade']) > 0:
                    suggestions.append({'ingrediente': alternative['ingrediente'],
                        'quantidade_disponivel': alternative['quantidade'], 'unidade': alternative['unidade'],
                        'condicao': condition, 'requer_aceite_e_nova_versao_da_receita': True})
        result.append({'ingrediente': name, 'necessario': str(requirement['quantidade']),
                       'disponivel': str(have), 'faltante': str(missing), 'unidade': requirement['unidade'],
                       'usar_da_despensa': str(min(have, requirement['quantidade'])),
                       'sobra_na_despensa': str(max(Decimal(0), have-requirement['quantidade'])),
                       'tipo_conversao': requirement['tipo'], 'substituicoes_na_despensa': suggestions,
                       'recurso_operacional':resource,
                       'opcao_compra': bool(missing),
                       'acao': 'Oferecer substituição ou pesquisar cotação da embalagem inteira; aguardar autorização.' if missing else 'Disponível'})
    return {'sucesso': True, 'receita_id': receita_id, 'orcamento': budget(db), 'ingredientes': result,
            'ja_tem': [row for row in result if number(row['disponivel']) > 0],
            'faltantes': [row for row in result if row['opcao_compra']],
            'pendencias': pending, 'analise_completa': not pending,
            'substituicoes': 'São opções condicionais, nunca aplicadas automaticamente. Confirme proporção, técnica, restrições e aceite; não deduza alergias.'}


def cotar(db, itens, frete_BRL='0.00'):
    if not isinstance(itens, list) or not 1 <= len(itens) <= 50:
        raise ValueError('Cotação deve ter de 1 a 50 itens')
    normalized = []; seen = set()
    for item in itens:
        name = canonical(item['ingrediente'])
        if key(name) in seen:
            raise ValueError('Consolide a quantidade comprada do mesmo ingrediente em uma linha')
        seen.add(key(name))
        qty, unit, kind = normalize(name, item['quantidade'], item['unidade'])
        # Cotações precisam de conteúdo de embalagem mensurável, não colher/pitada estimadas.
        if kind not in ['exata', 'convencao_usuario', 'conteudo_nominal']:
            raise ValueError('Informe o conteúdo da embalagem em g, kg, ml, L ou unidade já cadastrada')
        if key(item['unidade']) in ['pitada', 'a gosto', 'a_gosto', 'colher cheia', 'colher_cheia', 'punhado', 'gota', 'gotas']:
            raise ValueError('Compra exige conteúdo da embalagem, não medidas caseiras')
        row = stock(db, name)
        if row and row['unidade'] != unit:
            raise ValueError('Unidade da compra incompatível com estoque; informe massa ou volume correspondente')
        price = cents(item['valor_total_BRL'])
        origin = item['origem']
        source = item['fonte']
        if origin not in ['web', 'usuario'] or not isinstance(source, str) or not source.strip():
            raise ValueError('Informe origem web/usuario e fonte da cotação')
        if origin == 'web' and not (urlsplit(source).scheme in ['http', 'https'] and urlsplit(source).netloc):
            raise ValueError('Cotação web exige URL de origem')
        normalized.append({'ingrediente': name, 'quantidade': str(qty), 'unidade': unit,
                           'valor_centavos': price, 'origem': origin, 'fonte': source,
                           'descricao_embalagem': item.get('descricao_embalagem', str(item['quantidade']) + ' ' + item['unidade'])})
    delivery = cents(frete_BRL)
    total = sum(x['valor_centavos'] for x in normalized) + delivery
    identifier = 'cot_' + uuid.uuid4().hex
    created = datetime.now(timezone.utc)
    with db:
        db.execute('INSERT INTO despensa_cotacoes VALUES(?,?,?,?,?,?,?)',
                   (identifier, json.dumps(normalized, ensure_ascii=False), delivery, total,
                    created.isoformat(), (created + timedelta(hours=24)).isoformat(), 'pendente'))
    balance = db.execute('SELECT saldo_centavos FROM despensa_controle WHERE id=1').fetchone()[0]
    return {'sucesso': True, 'cotacao_id': identifier, 'itens': normalized, 'frete_BRL': brl(delivery),
            'total_BRL': brl(total), 'saldo_atual_BRL': brl(balance), 'saldo_apos_compra_BRL': brl(balance - total),
            'cabe_no_orcamento': total <= balance, 'estado': 'aguardando_autorizacao',
            'acao': 'Apresente itens, quantidades de embalagens, fonte, frete, total e saldo. '
                    'Pergunte se o usuário autoriza esta compra. Cotação não altera estoque nem orçamento.'}


def autorizar(db, cotacao_id, aprovado, evidencia_usuario, sessao='nao_informada', _verificar_producao=None, _autorizacao_contextual=False):
    if type(aprovado) is not bool or not isinstance(evidencia_usuario, str) or not evidencia_usuario.strip():
        raise ValueError('Forneça aprovado booleano e a declaração real do usuário')
    text = re.sub(r'\batuorizo\b','autorizo',key(evidencia_usuario))
    if aprovado and (not (_autorizacao_contextual or re.search(r'\b(autorizo|aprovo|aceito|confirmo)\b.*\b(compra|comprar)\b|\bpode comprar\b', text))
                     or re.search(r'\b(nao|talvez|se|depois|quando)\b', text)):
        return {'sucesso': False, 'erro': 'autorizacao_compra_explicita_necessaria',
                'acao': 'Aprovar uma receita não autoriza compras; peça aceite explícito da cotação.'}
    db.execute('BEGIN IMMEDIATE')
    try:
        quote = db.execute('SELECT * FROM despensa_cotacoes WHERE id=?', (cotacao_id,)).fetchone()
        if not quote:
            raise ValueError('Cotação desconhecida')
        existing = db.execute('SELECT * FROM despensa_compras WHERE cotacao_id=?', (cotacao_id,)).fetchone()
        if existing:
            if not aprovado:
                return {'sucesso': False, 'erro': 'compra_ja_registrada', 'acao': 'Recusa posterior não desfaz estoque e orçamento; solicite correção explícita.'}
            return {'sucesso': True, 'alterado': False, 'compra': dict(existing), 'orcamento': budget(db)}
        if not aprovado:
            db.execute("UPDATE despensa_cotacoes SET estado='recusada' WHERE id=?", (cotacao_id,))
            db.commit()
            return {'sucesso': True, 'alterado': False, 'estado': 'recusada', 'orcamento': budget(db)}
        if quote['estado'] != 'pendente' or datetime.now(timezone.utc) > datetime.fromisoformat(quote['expira_em']):
            return {'sucesso': False, 'erro': 'cotacao_indisponivel_ou_expirada', 'acao': 'Faça nova cotação antes de pedir autorização.'}
        # O MCP fornece o guard; dentro do lock de escrita, uma correção de
        # capacidade não pode ocorrer entre a checagem e a movimentação financeira.
        if _verificar_producao is not None:
            blocked = _verificar_producao()
            if blocked:
                return blocked
        items = json.loads(quote['itens_json'])
        numbers={2:'dois|duas',3:'tres',4:'quatro',5:'cinco',6:'seis',7:'sete',8:'oito',9:'nove',10:'dez'}
        exact_count=bool(re.search(r'\b(?:os|as)\s+(?:'+str(len(items))+'|'+numbers.get(len(items),'(?!)')+r')\s+(?:itens|ingredientes)\b',text))
        if len(items) > 1 and not (_autorizacao_contextual or exact_count or re.search(r'\b(cotacao|todos|todas|tudo|opcao)\b', text)):
            if not all(key(item['ingrediente']) in text for item in items):
                return {'sucesso': False, 'erro': 'autorizacao_parcial_ou_ambigua',
                        'acao': 'Autorize explicitamente a cotação completa ou crie outra cotação só dos itens aceitos.'}
        cap = re.search(r'\bate\s+r\$\s*(\d+(?:[.,]\d{1,2})?)', text)
        if cap and quote['total_centavos'] > cents(cap.group(1)):
            return {'sucesso': False, 'erro': 'total_acima_do_limite_autorizado', 'alterado': False}
        balance = db.execute('SELECT saldo_centavos FROM despensa_controle WHERE id=1').fetchone()[0]
        if quote['total_centavos'] > balance:
            return {'sucesso': False, 'erro': 'orcamento_insuficiente', 'orcamento': budget(db),
                    'total_BRL': brl(quote['total_centavos']), 'alterado': False}
        subtotal = sum(x['valor_centavos'] for x in items)
        remaining_freight = quote['frete_centavos']
        updates = []
        for index, item in enumerate(items):
            old = stock(db, item['ingrediente'])
            if old and old['unidade'] != item['unidade']:
                raise ValueError('Estoque mudou de unidade; refaça a cotação')
            allocation = remaining_freight if index == len(items) - 1 else (
                int(Decimal(quote['frete_centavos']) * item['valor_centavos'] / subtotal) if subtotal else 0)
            remaining_freight -= allocation
            qty = (number(old['quantidade']) if old else Decimal(0)) + number(item['quantidade'], True)
            cost = (old['custo_total_centavos'] if old else 0) + item['valor_centavos'] + allocation
            db.execute('INSERT OR REPLACE INTO despensa_estoque VALUES(?,?,?,?,?)',
                       (key(item['ingrediente']), item['ingrediente'], item['unidade'], str(qty), cost))
            updates.append({'ingrediente': item['ingrediente'], 'quantidade_comprada': item['quantidade'],
                            'quantidade_em_estoque': str(qty), 'unidade': item['unidade'],
                            'valor_item_BRL': brl(item['valor_centavos']), 'frete_rateado_BRL': brl(allocation),
                            'custo_total_estoque_BRL': brl(cost)})
        new_balance = balance - quote['total_centavos']
        identifier = 'compra_' + uuid.uuid4().hex
        db.execute('UPDATE despensa_controle SET saldo_centavos=? WHERE id=1', (new_balance,))
        db.execute('INSERT INTO despensa_compras VALUES(?,?,?,?,?,?,?)',
                   (identifier, cotacao_id, quote['total_centavos'], new_balance, evidencia_usuario, sessao, timestamp()))
        db.execute("UPDATE despensa_cotacoes SET estado='registrada' WHERE id=?", (cotacao_id,))
        result = {'sucesso': True, 'alterado': True, 'compra_id': identifier, 'cotacao_id': cotacao_id,
                  'valor_utilizado_BRL': brl(quote['total_centavos']), 'orcamento': budget(db), 'estoque_atualizado': updates,
                  'observacao': 'Registro operacional da compra autorizada. Este sistema não faz pedidos externos, pagamento ou entrega.'}
        db.commit()
        return result
    finally:
        if db.in_transaction:
            db.rollback()


def comparar_orcamento(db, analise, cotacao=None):
    """Só atesta custo de uma receita se todos os faltantes têm cobertura da cotação."""
    current = budget(db)
    missing = [row for row in analise['ingredientes'] if number(row['faltante']) > 0]
    result = {'saldo_atual_BRL': current['saldo_disponivel'], 'custo_compras_BRL': None,
              'saldo_apos_compra_BRL': None, 'cabe_no_orcamento': None,
              'itens_sem_cobertura': [row['ingrediente'] for row in missing],
              'inclui_frete': False, 'reserva_saldo': False}
    if not analise['analise_completa']:
        return dict(result, estado='quantidades_pendentes', acao='Não conclua custo ou orçamento enquanto houver quantidades sem fator documentado no JSON ou na receita original.')
    if not missing:
        return dict(result, estado='sem_compras', custo_compras_BRL='0.00',
                    saldo_apos_compra_BRL=current['saldo_disponivel'], cabe_no_orcamento=True,
                    itens_sem_cobertura=[], acao='Todos os ingredientes estão disponíveis nas quantidades conferidas. Não é necessário comprar para este lote.')
    if cotacao is None:
        return dict(result, estado='a_cotar', acao='Pesquise preços atuais e cote TODOS os faltantes em embalagens inteiras, incluindo frete. Custo desconhecido não é zero.')
    if cotacao['estado'] != 'pendente' or datetime.now(timezone.utc) > datetime.fromisoformat(cotacao['expira_em']):
        return dict(result, estado='cotacao_indisponivel', acao='Refaça a cotação: está expirada, recusada ou já registrada.')
    quoted = {key(item['ingrediente']): item for item in json.loads(cotacao['itens_json'])}
    uncovered = []
    for row in missing:
        item = quoted.get(key(row['ingrediente']))
        if item:
            row['compra_cotada'] = {'quantidade': item['quantidade'], 'unidade': item['unidade'],
                                  'custo_embalagens_BRL': brl(item['valor_centavos']), 'fonte': item['fonte'],
                                  'cobre_faltante': item['unidade'] == row['unidade'] and number(item['quantidade']) >= number(row['faltante'])}
        if not item or not row['compra_cotada']['cobre_faltante']:
            uncovered.append(row['ingrediente'])
    result.update(cotacao_id=cotacao['id'], custo_cotacao_parcial_BRL=brl(cotacao['total_centavos']),
                  itens_sem_cobertura=uncovered, inclui_frete=True)
    if uncovered:
        return dict(result, estado='cotacao_incompleta', acao='A cotação não cobre todos os faltantes. Complete itens/quantidades; o custo total da receita ainda é desconhecido.')
    total = cotacao['total_centavos']
    balance = cents(current['saldo_disponivel'])
    return dict(result, estado='cabe' if total <= balance else 'excede',
                custo_compras_BRL=brl(total), saldo_apos_compra_BRL=brl(balance-total),
                cabe_no_orcamento=total <= balance,
                acao='Apresente total de embalagens e frete e compare com o saldo atual; somente autorização explícita movimenta estoque/orçamento.')
