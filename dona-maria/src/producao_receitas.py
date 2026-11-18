"""Confronta requisitos de um lote com declarações confirmadas; nunca infere capacidade."""
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation


def fold(text):
    return ''.join(c for c in unicodedata.normalize('NFD', str(text).lower()) if not unicodedata.combining(c))


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS producao_planos(
        hash_versao TEXT PRIMARY KEY, requisitos_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS producao_cotacoes(
        cotacao_id TEXT PRIMARY KEY REFERENCES despensa_cotacoes(id), hash_versao TEXT NOT NULL
        REFERENCES producao_planos(hash_versao), requisitos_json TEXT NOT NULL);
    ''')


def required_hints(recipe):
    text = fold(' '.join([recipe['nome']] + recipe['modo_preparo']))
    hints = []
    for group, name, pattern in [
        ('equipamentos','forno',r'\bforno\b|\bassad\w*|\bassar\b|\bgratin\w*'),
        ('equipamentos','panela_pressao',r'pressao'),
        ('equipamentos','air_fryer',r'air\s*fryer|airfryer'),
        ('equipamentos','liquidificador',r'liquidificador'),
        ('equipamentos','fogao',r'refog|ferv|cozinh|frit|frigideira'),
        ('equipamentos','panela',r'\bpanela\b(?! de pressao)'),
        ('equipamentos','frigideira',r'\bfrigideira\b'),
        ('equipamentos','tigela',r'\btigela\b'),
        ('equipamentos','faca',r'\bfaca\b'),
        ('equipamentos','tabua',r'\btabua\b'),
        ('equipamentos','peneira',r'\bpeneir'),
        ('equipamentos','forma',r'\bforma\b|\bassadeir'),
        ('habilidades','massa_fresca',r'massa fresca'),
        ('habilidades','molho_bechamel',r'bechamel|bechamel|molho branco'),
        ('habilidades','ponto_da_carne',r'ponto.*carne|carne.*ponto|malpassad|ao ponto'),
        ('habilidades','cozinhar_arroz',r'cozinh.*arroz|arroz.*cozinh'),
        ('habilidades','cortar',r'\bcort\w*|\bpic(?:ar|ad\w*|ue)\b'),
        ('habilidades','refogar',r'\brefog\w*'),
    ]:
        if re.search(pattern,text): hints.append((group,name))
    return hints


def validate_spec(recipe, spec):
    if not isinstance(spec,dict) or set(spec) != {'lote','equipamentos','habilidades','operacao'}:
        raise ValueError('Informe lote, equipamentos, habilidades e operacao do lote exato da candidata')
    if not isinstance(spec['lote'],str) or not spec['lote'].strip(): raise ValueError('Descreva o lote/porções')
    for group in ['equipamentos','habilidades']:
        if not isinstance(spec[group],list) or not 1 <= len(spec[group]) <= 40:
            raise ValueError('Liste os requisitos de '+group+', incluindo utensílios e técnicas simples')
        for req in spec[group]:
            if not isinstance(req,dict) or not {'chave','pergunta'} <= set(req) or not set(req) <= {'chave','campo','minimo','igual','pergunta'}:
                raise ValueError('Requisito deve conter chave, pergunta e exatamente um de minimo/igual')
            if ('minimo' in req) == ('igual' in req): raise ValueError('Use minimo ou igual')
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,99}',req['chave']): raise ValueError('Chave inválida')
            if not isinstance(req['pergunta'],str) or not req['pergunta'].strip(): raise ValueError('Pergunta obrigatória')
            if 'campo' in req and (not isinstance(req['campo'],str) or not req['campo']): raise ValueError('Campo inválido')
            if 'igual' in req and req['igual'] in [None,False,'',[],{}]: raise ValueError('Disponibilidade não pode ser validada por valor ausente/negativo')
            if 'minimo' in req: number(req['minimo'],positive=True)
            if group=='equipamentos' and req['chave']=='fogao' and (req.get('campo')!='bocas' or 'minimo' not in req):
                raise ValueError('Fogão exige campo=bocas e minimo de bocas simultâneas para o lote')
    for group,name in required_hints(recipe):
        keys=[r['chave'] for r in spec[group]]
        aliases={'air_fryer':['air_fryer','airfryer'],'forno':['forno','forno_eletrico'],
                 'fogao':['fogao','fogao_bocas']}.get(name,[name])
        if not any(k in aliases for k in keys): raise ValueError('Requisito detectado no preparo ausente: '+group+'.'+name)
    op=spec['operacao']
    if not isinstance(op,dict) or set(op) != {'energia','gas','tempo_minutos','geladeira_litros','bancada_livre'}:
        raise ValueError('Operacao exige energia/gas booleanos, tempo_minutos, geladeira_litros e bancada_livre=true')
    if type(op['energia']) is not bool or type(op['gas']) is not bool or op['bancada_livre'] is not True:
        raise ValueError('Defina a fonte de energia/gás necessária e a bancada de trabalho')
    number(op['tempo_minutos'],positive=True); number(op['geladeira_litros'])
    if any(g=='equipamentos' and n in ['fogao','forno','air_fryer'] for g,n in required_hints(recipe)) and not (op['energia'] or op['gas']):
        raise ValueError('Preparo aquecido exige confirmar energia e/ou gás conforme o equipamento real')


def number(value,positive=False):
    if isinstance(value,bool): raise ValueError('Quantidade deve ser numérica')
    try: result=Decimal(str(value))
    except (InvalidOperation,ValueError): raise ValueError('Quantidade inválida')
    if not result.is_finite() or result < 0 or (positive and result == 0): raise ValueError('Quantidade inválida')
    return result


def evaluate(db, recipe, spec):
    validate_spec(recipe or {'nome':'','modo_preparo':[]},spec)
    facts={(r['grupo'],r['chave']):r for r in db.execute('SELECT * FROM fatos')}
    requirements=[]
    for group in ['equipamentos','habilidades']:
        requirements.extend((group,r) for r in spec[group])
    op=spec['operacao']
    for key,target,question in [
        ('tempo_por_cozinhada_minutos',op['tempo_minutos'],'Quanto tempo você tem disponível por cozinhada, incluindo preparo, cocção e limpeza?'),
        ('espaco_geladeira_litros',op['geladeira_litros'],'Quanto espaço livre há na geladeira para esse lote e sua conservação?'),
        ('bancada_livre',True,'Há bancada e espaço de trabalho suficientes para preparar esse lote?'),
    ]:
        requirements.append(('operacao',{'chave':key,'pergunta':question,('igual' if type(target) is bool else 'minimo'):target}))
    for resource in ['energia','gas']:
        # A fonte real confirmada do aparelho prevalece sobre flags do modelo.
        needed=op[resource]
        for req in spec['equipamentos']:
            row=facts.get(('equipamentos',req['chave']))
            value=json.loads(row['valor_json']) if row and row['status']=='confirmado' else None
            kind=fold(value.get('tipo','')) if isinstance(value,dict) else ''
            if resource=='energia' and (req['chave'] in ['forno_eletrico','air_fryer','airfryer','liquidificador','micro_ondas','batedeira','abridor_massas','fatiador_massas'] or 'eletric' in kind or 'inducao' in kind): needed=True
            if resource=='gas' and 'gas' in kind: needed=True
        if needed: requirements.append(('operacao',{'chave':resource+'_disponivel','igual':True,
            'pergunta':'Você tem '+('energia elétrica' if resource=='energia' else 'gás')+' disponível, sem limitação que impeça preparar esse lote?'}))
    if any(req['chave'] in ['fogao','fogao_bocas'] for req in spec['equipamentos']):
        row=facts.get(('equipamentos','fogao'))
        value=json.loads(row['valor_json']) if row and row['status']=='confirmado' else None
        kind=fold(value.get('tipo','')) if isinstance(value,dict) else ''
        if not any(tag in kind for tag in ['gas','eletric','inducao']):
            requirements.append(('equipamentos',{'chave':'fogao','campo':'tipo','igual':'gas/eletrico/inducao',
                'pergunta':'Seu fogão é a gás, elétrico ou de indução? Preciso conferir a fonte de energia disponível.'}))
    checks=[]
    for group,req in requirements:
        row=facts.get((group,req['chave']))
        value=json.loads(row['valor_json']) if row and row['status']=='confirmado' else None
        if req.get('campo'):
            for part in req['campo'].split('.'):
                value=value.get(part) if isinstance(value,dict) else None
        state='desconhecido'
        if value is not None:
            try:
                valid=(number(value)>=number(req['minimo'])) if 'minimo' in req else (type(value) is type(req['igual']) and value==req['igual'])
                state='atendido' if valid else 'impedimento'
            except ValueError: state='desconhecido'
        checks.append({'grupo':group,'chave':req['chave'],'estado':state,'valor_confirmado':value,'pergunta':req['pergunta']})
    pending=[x for x in checks if x['estado']!='atendido']
    return {'sucesso':True,'producao_validada':not pending,'lote':spec['lote'],
            'verificacoes':checks,'pendencias':pending,'perguntas':[x['pergunta'] for x in pending if x['estado']=='desconhecido'],
            'acao':('Capacidade declarada compatível com este lote. Aceite e compra ainda exigem autorização.' if not pending else
                    'Pergunte pelos dados desconhecidos e resolva os impedimentos. Registre as respostas reais com memoria_registrar_lote e revalide. Não aceite o prato nem autorize compras; adapte a receita ou escolha outra.')}


def guard(db, version):
    row=db.execute('SELECT * FROM producao_planos WHERE hash_versao=?',(version,)).fetchone()
    if row:
        result=evaluate(db,None,json.loads(row['requisitos_json']))
        if result['producao_validada']: return None
        return dict(result,sucesso=False,erro='producao_nao_validada')
    return {'sucesso':False,'producao_validada':False,'erro':'producao_nao_validada','acao':'Use receita_verificar_producao para este lote antes de aceitar o prato ou oferecer/autorizar compras.'}


def discovery(db, recipe):
    """Perguntas iniciais; a checagem quantitativa do lote continua obrigatória."""
    facts={(r['grupo'],r['chave']):r for r in db.execute('SELECT * FROM fatos')}
    questions=[]
    def known(group,key):
        row=facts.get((group,key))
        return json.loads(row['valor_json']) if row and row['status']=='confirmado' else None
    required = set(required_hints(recipe))
    stove=known('equipamentos','fogao')
    if ('equipamentos', 'fogao') in required and (not isinstance(stove,dict) or type(stove.get('bocas')) is not int or not stove.get('tipo')):
        questions.append('Seu fogão tem quantas bocas utilizáveis e funciona a gás, eletricidade ou indução?')
    for key,question in [
        ('forno_eletrico','Você tem forno disponível? De que tipo e capacidade?'),
        ('panela_pressao','Você tem panela de pressão? Qual a capacidade?'),
        ('air_fryer','Você tem air fryer disponível? Qual a capacidade?'),
        ('liquidificador','Você tem liquidificador disponível?'),
    ]:
        required_key = 'forno' if key == 'forno_eletrico' else key
        if ('equipamentos', required_key) in required and known('equipamentos',key) is None and not (key=='forno_eletrico' and known('equipamentos','forno') is not None): questions.append(question)
    for group,name in required_hints(recipe):
        if group=='habilidades' and known(group,name) is None:
            questions.append('Você domina a técnica '+name.replace('_',' ')+' necessária para esse prato?')
        elif group == 'equipamentos' and name in ['panela', 'frigideira', 'tigela', 'faca', 'tabua', 'peneira', 'forma'] and known(group, name) is None:
            questions.append('Você tem '+name.replace('_', ' ')+' disponível para preparar esse lote?')
    for key,question in [
        ('energia_disponivel','Há limitação de energia elétrica que possa impedir essa produção?'),
        ('gas_disponivel','Há gás disponível, se algum equipamento necessário depender dele?'),
        ('espaco_geladeira_litros','Quanto espaço livre há na geladeira para os ingredientes e o lote pronto?'),
        ('tempo_por_cozinhada_minutos','Quanto tempo você pode dedicar a cada cozinhada, incluindo preparo e limpeza?'),
        ('bancada_livre','Há espaço de bancada suficiente para trabalhar nesse lote?'),
    ]:
        if known('operacao',key) is None: questions.append(question)
    return questions


def conferir_memoria(db, recipe, version):
    """Conferência automática: só um plano completo pode confirmar o lote."""
    plan = db.execute('SELECT requisitos_json FROM producao_planos WHERE hash_versao=?', (version,)).fetchone()
    if plan:
        return evaluate(db, recipe, json.loads(plan[0]))
    facts = {(r['grupo'], r['chave']): r for r in db.execute('SELECT * FROM fatos')}
    checks = []
    aliases = {'forno': ['forno', 'forno_eletrico'], 'air_fryer': ['air_fryer', 'airfryer']}
    for group, key in required_hints(recipe):
        rows = [facts.get((group, name)) for name in aliases.get(key, [key])]
        row = next((r for r in rows if r and r['status'] == 'confirmado'), None)
        value = json.loads(row['valor_json']) if row else None
        state = 'desconhecido' if value is None else 'impedimento' if value is False else 'presenca_declarada'
        checks.append({'grupo': group, 'chave': key, 'estado': state, 'valor_confirmado': value})
    return {'sucesso': True, 'producao_validada': False, 'requer_plano_lote': True,
            'verificacoes': checks, 'pendencias': [c for c in checks if c['estado'] != 'presenca_declarada'],
            'perguntas': discovery(db, recipe),
            'acao': 'A memória foi consultada automaticamente. Pergunte somente os requisitos pendentes e derive o plano do lote com receita_verificar_producao, sem esperar pedido de conferência. Não infira capacidades quantitativas de presença declarada.'}
