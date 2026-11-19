"""Recupera comprovantes da sessão do Hermes, sem promover rascunhos a memória aprovada."""
import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

SESSION = ContextVar('dona_maria_session', default=None)
HISTORY = Path(str(Path.home() / 'agente-de-atendimento' / '.hermes' / 'state.db'))
ROOT = Path(str(Path.home() / 'agente-de-atendimento' / 'dona-maria'))

def fold(text):
    return ''.join(c for c in unicodedata.normalize('NFD', str(text).lower()) if not unicodedata.combining(c))


def decode(value):
    for _ in range(6):
        if not isinstance(value,str): return value
        try: value=json.loads(value)
        except ValueError:
            if '\\"' in value: value=value.replace('\\"','"')
            else: return None
    return value


def result(content):
    for line in (content or '').splitlines():
        value=decode(line)
        if isinstance(value,dict):
            if 'result' in value: value=decode(value['result'])
            if isinstance(value,dict):return value
    return {}


def calls(value):
    value=decode(value)
    if isinstance(value,list):
        for x in value: yield from calls(x)
    elif isinstance(value,dict):
        if 'function' in value: yield from calls(value['function'])
        elif value.get('name'):
            args=decode(value.get('arguments',{}))
            if value['name']=='tool_call':yield from calls(args)
            elif isinstance(args,dict):yield value['name'],args
        elif 'calls' in value:yield from calls(value['calls'])


def messages(session=None):
    session=session or SESSION.get()
    if not session:return []
    try:
        with closing(sqlite3.connect(HISTORY.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT cwd FROM sessions WHERE id=?',(session,)).fetchone()
            if not row or not row['cwd'] or not Path(row['cwd']).resolve().is_relative_to(ROOT):return []
            rows = list(reversed([dict(x) for x in db.execute('SELECT * FROM messages WHERE session_id=? AND active=1 ORDER BY id DESC LIMIT 1500',(session,))]))
            expanded = []
            for row in rows:
                if row['role'] == 'tool' and row['tool_name'] == 'dona_maria_agente_resultado':
                    # Recibos MCP transportados pelo plugin: o resumo livre do filho
                    # não participa de aceite, fonte, recuperação ou precificação.
                    value = result(row['content'])
                    for i, event in enumerate(value.get('eventos', [])[:32]):
                        if not isinstance(event, dict) or not str(event.get('tool_name', '')).startswith('mcp__dona_maria_memoria__'):
                            continue
                        call = {'name': event['tool_name'], 'arguments': event.get('args', {})}
                        expanded.append(dict(row, role='assistant', content='', tool_name=None,
                                             tool_calls=json.dumps([call]), indice_evento=i*2))
                        expanded.append(dict(row, content=json.dumps(event.get('result', {})),
                                             tool_name=event['tool_name'], indice_evento=i*2+1))
                expanded.append(row)
            return expanded
    except (OSError,sqlite3.Error,ValueError):return []


def recent(row):
    return 0 <= datetime.now(timezone.utc).timestamp()-row['timestamp'] <= 86400


def search_for(url, session=None, before=None):
    rows=messages(session)
    for row in reversed(rows):
        if before and _order(row) >= (before if isinstance(before, tuple) else (before, 1000)):continue
        if row['role']!='tool' or row['tool_name'] not in ['web_search','mcp__dona_maria_memoria__web_search'] or not recent(row):continue
        value=result(row['content']); web=value.get('resultado_web',value)
        hits=web.get('data',{}).get('web',[])
        if not any(hit.get('url')==url for hit in hits if isinstance(hit,dict)):continue
        if not (value.get('sucesso') is True or web.get('success') is True):continue
        query=None
        for previous in reversed(rows):
            if _order(previous)>=_order(row) or previous['role']!='assistant':continue
            for name,args in calls(previous.get('tool_calls')):
                if name in ['web_search','mcp__dona_maria_memoria__web_search']:
                    query=args.get('query');break
            if query:break
        return {'criado_em':datetime.fromtimestamp(row['timestamp'],timezone.utc).isoformat(),
                'query':query or 'Consulta comprovada pelo recibo de busca na sessão',
                'urls':[hit['url'] for hit in hits if isinstance(hit,dict) and hit.get('url')],
                'origem':'recibo_web_da_sessao','mensagem_id':row['id']}
    return None


def presentation(identifier=None,session=None):
    rows = messages(session)
    for row in reversed(rows):
        if row['role']!='tool' or row['tool_name']!='mcp__dona_maria_memoria__receita_apresentar' or not recent(row):continue
        value=result(row['content'])
        if value.get('sucesso') is True and (not identifier or value.get('receita_id')==identifier):
            recipe=value['receita']; canonical=json.dumps(recipe,ensure_ascii=False,sort_keys=True,allow_nan=False)
            search=search_for(recipe['fonte_url'],session,before=_order(row))
            if not search:return None
            audit = next((result(r['content']) for r in reversed(rows)
                          if r['role'] == 'tool' and r['tool_name'] == 'mcp__dona_maria_memoria__receita_conferir_execucao'
                          and result(r['content']).get('receita_id') == value['receita_id']), {})
            return {'criado_em':datetime.fromtimestamp(row['timestamp'],timezone.utc).isoformat(),
                    'receita':recipe,'receita_id':value['receita_id'],
                    'hash_versao':hashlib.sha256(canonical.encode()).hexdigest(),
                    'pesquisa':search,'resultado_aprovacao':None,'mensagem_id':row['id'],
                    'fonte_conferida':value.get('fonte_conferida') is True and (not audit or audit.get('sucesso') is True),
                    'execucao_conferida':audit.get('conferencia_execucao') if audit.get('hash_versao') == hashlib.sha256(canonical.encode()).hexdigest() else None}
    return None


def accepted(identifier,session=None):
    active=[];new_group=True;acceptance=None;target_name='';question=''
    for row in messages(session):
        if row['role']=='assistant' and row.get('content'):
            question=fold(row['content'])
        if row['role']=='user':
            text=fold(row['content']);new_group=True
            selected=(active==[identifier] or (identifier in active and target_name and target_name in text))
            if not selected:continue
            if re.search(r'\b(nao gostei|nao gosto|nao aceito|desisti|recuso|rejeito)\b|\bnao quero (?:esse|este|essa|esta) (?:prato|receita)\b',text): acceptance=None
            elif (re.search(r'\b(gostei|gosto|adorei|amei|curti|aceito|aprovo)\b|\bquero esse prato\b',text)
                  or (text.strip(' .!') in ['sim','sim gosto','pode ser','quero sim']
                      and re.search(r'gosta de cozinhar|gostou (?:desse|deste|do) prato|quer (?:preparar|seguir com) (?:essa|esta) receita',question))) and not re.search(r'\b(talvez|se|quando)\b',text):
                # Compra e autorização para buscar não atestam interesse no prato.
                if not re.search(r'\b(compra|comprar|pesquise|busque)\b',text):
                    acceptance={'evidencia':row['content'],'mensagem_id':row['id']}
            question=''
        elif row['role']=='tool' and row['tool_name']=='mcp__dona_maria_memoria__receita_apresentar':
            value=result(row['content'])
            if value.get('sucesso') is not True:continue
            if new_group:active=[];new_group=False
            if value['receita_id'] not in active:active.append(value['receita_id'])
            if value['receita_id']==identifier:target_name=fold(value['receita']['nome'])
    return acceptance


def latest_arguments(identifier,names,session=None):
    for row in reversed(messages(session)):
        if row['role']!='assistant':continue
        for name,args in calls(row.get('tool_calls')):
            if name in names and args.get('receita_id')==identifier:return args
    return None


def interest_target(text, session=None):
    """Resolve a versão escolhida, sem transferir interesse entre candidatas."""
    active = {}; fresh = True
    for row in messages(session):
        if row['role'] == 'user':
            fresh = True
        elif row['role'] == 'tool' and row['tool_name'] == 'mcp__dona_maria_memoria__receita_apresentar' and recent(row):
            value = result(row['content'])
            if value.get('sucesso'):
                if fresh:
                    active = {}; fresh = False
                active[value['receita_id']] = value
    normalized = fold(text)
    urls = re.findall(r'https?://[^\s<>]+', str(text))
    selected = [v for v in active.values() if (v['receita']['fonte_url'] in urls if urls else fold(v['receita']['nome']) in normalized)]
    if not selected and not urls and len(active) == 1:
        selected = list(active.values())
    if len(selected) == 1:
        return {'sucesso': True, 'receita_id': selected[0]['receita_id'], 'receita': selected[0]['receita']}
    return {'sucesso': False, 'erro': 'receita_ambigua' if active else 'sem_receita_identificada',
            'acao': 'De qual receita você gostou? Informe o nome ou o link para eu conferir o preparo e os requisitos.'}


def ingredient_mapping(recipe, ingredients):
    """Valida referências por ingrediente; um índice existente não prova cobertura."""
    import despensa_compras as pantry
    if not isinstance(ingredients,list) or not ingredients:raise ValueError('Informe os ingredientes da receita')
    def words(text):
        return set(re.findall(r'[a-z]+',fold(text)))
    lines=recipe['ingredientes'];mapped=[];pending=[];adjustments=[]
    for item in ingredients:
        name=pantry.canonical(item['ingrediente']);label=fold(name)
        anchor = ('creme de leite' if 'creme de leite' in label else
                  'leite' if label.startswith('leite') else
                  'queijo' if label.startswith('queijo') else
                  'salsinha' if 'salsinha' in label else label.split()[0])
        if anchor=='peito':anchor='frango'
        matches=[]
        for index,line in enumerate(lines):
            core=fold(re.sub(r'\([^)]*\)','',line))
            tokens=words(core)
            alternatives={'salsinha':['salsinha','cheiro-verde','cheiro verde'],
                          'frango':['frango'],'batata':['batata','batatas']}.get(anchor,
                          [anchor,anchor[:-1] if anchor.endswith('s') else anchor+'s'])
            if any(re.search(r'\b'+re.escape(word)+r'\b',core) for word in alternatives):
                if anchor=='queijo':
                    kind=next((x for x in ['mussarela','parmesao','ricota'] if x in label),None)
                    if kind and kind not in core:continue
                matches.append(index)
        if not matches:
            pending.append({'ingrediente':name,'erro':'Não corresponde ao ingrediente principal de nenhuma linha da versão apresentada. Substituições exigem discussão e nova versão; chantilly não substitui automaticamente creme de leite ou requeijão.'})
            continue
        original=item.get('indice_receita')
        index=original if original in matches else matches[0] if len(matches)==1 else None
        if index is None:
            pending.append({'ingrediente':name,'erro':'Indique qual linha usa este ingrediente repetido.','indices_possiveis':matches});continue
        row=dict(item,ingrediente=name,indice_receita=index)
        if original!=index:adjustments.append({'ingrediente':name,'indice_receita_anterior':original,'indice_receita':index})
        # Só conferir medidas explícitas e convenções inequívocas. Não extrair
        # quantidades de parênteses/adaptações nem inventar equivalências.
        core=fold(re.sub(r'\([^)]*\)','',lines[index])).strip()
        expected=None
        head=re.match(r'^(\d+(?:[.,]\d+)?(?:/\d+)?)\s*(kg|mg|g|ml|l|dentes?|ovos?|cebolas?|tomates?|batatas?)\b',core)
        try:
            if head:
                from decimal import Decimal
                parts=head.group(1).replace(',','.').split('/')
                amount=Decimal(parts[0])/(Decimal(parts[1]) if len(parts)>1 else 1)
                measure=head.group(2)
                if measure in ['dente','dentes']:measure='dente'
                elif measure not in ['kg','mg','g','ml','l']:measure='un'
                if measure=='l':measure='L'
                expected=pantry.normalize(name,amount,measure)[:2]
            elif 'a gosto' in core:
                expected=pantry.normalize(name,1,'a gosto')[:2]
            if expected:
                actual=pantry.normalize(name,item['quantidade'],item['unidade'],item.get('estado'))[:2]
                if actual!=expected:
                    pending.append({'ingrediente':name,'indice_receita':index,'erro':'Quantidade difere da receita/convenção do projeto. Corrija esta linha sem pedir novo aceite.',
                                    'quantidade_esperada':str(expected[0]),'unidade_esperada':expected[1]})
        except (ValueError,ArithmeticError):
            pass
        mapped.append(row)
    covered={x['indice_receita'] for x in mapped}
    for index,line in enumerate(lines):
        if index not in covered:
            pending.append({'indice_receita':index,'linha_original':line,'erro':'Linha ainda não contemplada; resolva ingrediente/quantidade ou adaptação.'})
            continue
        text=fold(line);rows=[x for x in mapped if x['indice_receita']==index]
        if re.search(r'\bsal\s+e\s+pimenta',text):
            for required in ['sal','pimenta']:
                if not any(re.search(r'\b'+required,fold(x['ingrediente'])) for x in rows):
                    pending.append({'indice_receita':index,'erro':'Inclua separadamente '+required+' desta linha composta, usando a convenção de a gosto.'})
        if re.search(r'\bou\b',re.sub(r'\([^)]*\)','',text)) and len({x['ingrediente'] for x in rows})>1:
            pending.append({'indice_receita':index,'erro':'A linha contém alternativas (OU). Escolha a que será usada; não compre/cobre ambas automaticamente.'})
    return {'sucesso':not pending,'ingredientes':mapped,'pendencias':pending,'ajustes_indices':adjustments,
            'linhas_originais':[{'indice_receita':i,'texto':line} for i,line in enumerate(lines)]}


def save_consent(identifier,evidence,session=None):
    if not isinstance(evidence,str) or not evidence.strip():return False
    selected=[];fresh=True;authorized=False
    for row in messages(session):
        if row['role']=='user':
            fresh=True;text=fold(row['content'])
            if selected==[identifier]:
                if re.search(r'\b(nao|desisti|revogo)\b.*\b(salvar|salve|guardar|guarde|memorizar|registrar)\b',text):authorized=False
                elif evidence.strip() in row['content'] and re.search(r'\b(salvar|salve|guardar|guarde|memorizar|registrar)\b',text) and not re.search(r'\b(nao|talvez|se|quando)\b',text):authorized=True
        elif row['role']=='tool' and row['tool_name']=='mcp__dona_maria_memoria__receita_apresentar':
            value=result(row['content'])
            if value.get('sucesso'):
                if fresh:selected=[];fresh=False
                selected.append(value['receita_id'])
    return authorized


def purchase_confirmation(quote, evidence, session=None):
    if fold(evidence).strip(' .!') not in ['sim','sim confirmo','confirmo','sim autorizo','pode ser','atuorizo a compra','autorizo a compra']:return None
    rows=messages(session)
    users=[i for i,row in enumerate(rows) if row['role']=='user']
    if not users or evidence.strip() not in rows[users[-1]]['content']:return None
    boundary=users[-2] if len(users)>1 else -1
    question=next((row for row in reversed(rows[boundary+1:users[-1]]) if row['role']=='assistant' and row['content'] and '?' in row['content']),None)
    if not question:return None
    text=fold(question['content'])
    if not re.search(r'autoriza|aceita comprar|aceita a compra|confirmar a compra',text):return None
    if quote['id'] not in question['content']:
        items=json.loads(quote['itens_json'])
        normalized=re.sub(r'[^a-z0-9]','',text)
        if not all(re.sub(r'[^a-z0-9]','',fold(item['ingrediente'])) in normalized for item in items):return None
        amount=format(quote['total_centavos']/100,'.2f').replace('.',',')
        if amount not in text:return None
    return {'resposta_mensagem_id':rows[users[-1]]['id'],'pergunta_mensagem_id':question['id'],'cotacao_id':quote['id']}


def price_consent(pricing, scenario, evidence, session=None):
    """Escolha vinculada ao recibo de CMV e à pergunta realmente mostrada."""
    if not isinstance(evidence, str) or not evidence.strip():
        return None
    rows = messages(session)
    users = [i for i, row in enumerate(rows) if row['role'] == 'user']
    if not users or rows[users[-1]]['content'].strip() != evidence.strip():
        return None
    boundary = users[-2] if len(users) > 1 else -1
    previous = rows[boundary+1:users[-1]]
    receipts = [(i, result(row['content'])) for i, row in enumerate(previous)
                if row['role'] == 'tool' and row['tool_name'] == 'mcp__dona_maria_memoria__receita_precificar']
    if not receipts:
        # A retomada também pode mostrar a cotação persistida após reiniciar o MCP.
        receipts = [(i, result(row['content']).get('precificacao', {})) for i, row in enumerate(previous)
                    if row['role'] == 'tool' and row['tool_name'] == 'mcp__dona_maria_memoria__receita_consultar_fluxo']
    if not receipts:
        return None
    index, receipt = receipts[-1]
    if not receipt.get('sucesso') or receipt.get('precificacao_id') != pricing['precificacao_id']:
        return None
    if any(row['role'] == 'tool' and row['tool_name'] == 'mcp__dona_maria_memoria__receita_apresentar'
           for row in previous[index+1:]):
        return None
    question = next((r for r in reversed(previous[index+1:]) if r['role'] == 'assistant' and r['content']), None)
    if not question:
        return None
    shown = fold(question['content'])
    if fold(pricing['nome_receita']) not in shown or not re.search(r'qual preco|qual cenario|escolh|adotar', shown):
        return None
    if not all(s['preco_por_porcao_BRL'].replace('.', ',') in shown for s in pricing['cenarios']):
        return None
    text = fold(evidence).strip(' .!')
    if re.search(r'\b(nao|talvez|se|quando|mas|porem|comprar|compra|ou)\b', text) or '?' in text:
        return None
    numbers = re.findall(r'\b(?:cenario|opcao)\s*([123])\b', text)
    ordinals = [i for i, word in enumerate(['primeir', 'segund', 'terceir'], 1)
                if re.search(r'\b'+word+r'[oa]\b', text)]
    selected = {int(x) for x in numbers} | set(ordinals)
    if text in ['1', '2', '3']:
        selected.add(int(text))
    amounts = re.findall(r'\b\d+[.,]\d{2}\b', text)
    if amounts:
        selected |= {i for i, s in enumerate(pricing['cenarios'], 1)
                     if s['preco_por_porcao_BRL'] in [a.replace(',', '.') for a in amounts]}
        if any(a.replace(',', '.') != pricing['cenarios'][scenario-1]['preco_por_porcao_BRL'] for a in amounts):
            return None
    if selected != {scenario}:
        return None
    return {'evidencia': evidence, 'resposta_mensagem_id': rows[users[-1]]['id'],
            'pergunta_mensagem_id': question['id'], 'precificacao_id': pricing['precificacao_id']}


def user_evidence(evidence, session=None):
    import re, unicodedata
    def normalize(s):
        return re.sub(r'[\u0300-\u036f]', '', unicodedata.normalize('NFKD', s)).casefold()
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    e = normalize(evidence.strip())
    return any(
        row['role'] == 'user' and e in normalize(row['content'])
        for row in messages(session))


def _order(row):
    return row['id'], row.get('indice_evento', 1000)
