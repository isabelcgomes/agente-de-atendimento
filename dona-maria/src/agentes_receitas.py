"""Coordenação de especialistas Hermes pela API pública subagent_lifecycle.

O resumo do LLM nunca comprova uma receita ou um preço: só recibos MCP podem
avançar o fluxo. Nenhum especialista pode gravar fatos, comprar ou salvar menu.
"""
import dataclasses
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PREFIX = 'mcp__dona_maria_memoria__'
TOOLSETS = {'pesquisa': 'dona_maria_pesquisa', 'custos': 'dona_maria_custos'}
WORK_TOOLS = {'pesquisa': 'dona_maria_pesquisar', 'custos': 'dona_maria_calcular'}
COST_TOOLS = {'dona_maria_custos_'+op:op for op in
              ['despensa_verificar_receita', 'receita_precificar']}
OPERATIONS = {
    'pesquisa': frozenset(['web_search', 'receita_ler_fonte', 'receita_apresentar', 'receita_conferir_execucao', 'preco_pesquisar']),
    'custos': frozenset(['medidas_consultar', 'despensa_verificar_receita', 'receita_precificar']),
}
ROUTED = frozenset(PREFIX + op for ops in OPERATIONS.values() for op in ops)
CONTROL_TOOLS = frozenset(['dona_maria_delegar', 'dona_maria_agente_resultado'])
TERMINAL = frozenset(['SUCCEEDED', 'FAILED', 'INTERRUPTED', 'CANCELLED', 'UNKNOWN'])


def cost_pending_receipts(events):
    """Somente retornos de negócio comprovados; erros de chamada não são pendências da usuária."""
    result = {}
    for event in events:
        name = event.get('tool_name', '').split('__')[-1]
        value = event.get('result', {})
        identity = (name, str(value.get('ingrediente', event.get('args', {}).get('ingrediente', ''))).casefold(),
                    str(event.get('args', {}).get('medida', '')).casefold())
        if value.get('sucesso') is True:
            result.pop(identity, None)
        if ((name == 'medidas_consultar' and value.get('erro') == 'conversao_indefinida')
                or (name in ['despensa_verificar_receita', 'receita_precificar']
                    and value.get('erro') != 'argumentos_invalidos'
                    and (value.get('pendencias') or value.get('faltantes') or value.get('erro') == 'rendimento_pendente'))):
            result[identity] = event
    return list(result.values())


class LifecycleProxy:
    """Resolve a fachada somente no turno, sem iniciar runtime durante register/doctor."""
    def __init__(self, context):
        self.context = context

    def __getattr__(self, name):
        return getattr(self.context.subagent_lifecycle, name)


def unpack(value):
    for _ in range(8):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return {}
        elif isinstance(value, dict) and (value.get('truncated') or value.get('ok') is False or value.get('isError')):
            return {'sucesso': False, 'erro': 'mcp_indisponivel_ou_truncado'}
        elif isinstance(value, dict) and 'result' in value and 'sucesso' not in value:
            value = value['result']
        elif isinstance(value, dict) and isinstance(value.get('content'), list):
            value = next((x.get('text') for x in value['content'] if x.get('type') == 'text'), '')
        else:
            return value if isinstance(value, dict) else {}
    return {}


def record(value):
    return dataclasses.asdict(value) if dataclasses.is_dataclass(value) else dict(value)


def state_name(value):
    return getattr(value, 'value', value)


class TaskStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS tarefas(
                id TEXT PRIMARY KEY, dono TEXT NOT NULL, chave TEXT NOT NULL,
                dados TEXT NOT NULL, UNIQUE(dono,chave));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, job):
        with self.connect() as db:
            db.execute('INSERT INTO tarefas VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET dados=excluded.dados',
                       (job['id'], job['dono'], job['chave'], json.dumps(job, ensure_ascii=False)))

    def get(self, identifier, owner=None):
        with self.connect() as db:
            row = db.execute('SELECT dados FROM tarefas WHERE id=?', (identifier,)).fetchone()
        job = json.loads(row[0]) if row else None
        return job if job and (owner is None or job['dono'] == owner) else None

    def jobs(self, owner):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT dados FROM tarefas WHERE dono=? ORDER BY rowid', (owner,))]


class Coordinator:
    def __init__(self, service, backend, store, timeout=180, max_calls=16):
        self.service, self.backend, self.store = service, backend, store
        self.timeout, self.max_calls = timeout, max_calls
        self.lock = threading.RLock()
        self.children = {}
        self.call_lock = threading.RLock()

    def call(self, owner, name, args=None):
        args = dict(args or {}, sessao=owner)
        # O servidor MCP mantém seu próprio estado; não abra outra conexão SQLite.
        with self.call_lock:
            return unpack(self.backend(name, args))

    def pending(self, owner):
        return [{'tarefa_id': j['id'], 'agente': j['agente'], 'estado': j['estado'], 'receita_id':j.get('receita_id')}
                for j in self.store.jobs(owner) if not j.get('entregue')]

    def start(self, agent, owner, turn, message, recipe_id=None, purpose='receita'):
        if agent not in TOOLSETS or not owner or not turn or not isinstance(message, str):
            return {'sucesso': False, 'erro': 'delegacao_invalida'}
        if agent == 'custos' and not recipe_id:
            return {'sucesso': False, 'erro': 'receita_id_obrigatorio'}
        if purpose not in ['receita', 'precos', 'execucao'] or purpose in ['precos', 'execucao'] and (agent != 'pesquisa' or not recipe_id):
            return {'sucesso': False, 'erro': 'finalidade_invalida'}
        key = hashlib.sha256(json.dumps([turn, agent, recipe_id, purpose], ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            jobs = self.store.jobs(owner)
            existing = next((j for j in jobs if j['chave'] == key), None)
            if existing:
                return {'sucesso': True, 'tarefa_id': existing['id'], 'estado': existing['estado'], 'reutilizada': True}
            active = [j for j in jobs if j['estado'] not in TERMINAL]
            if len(active) >= 2:
                return {'sucesso': False, 'erro': 'limite_de_tarefas', 'acao': 'Consulte ou cancele as tarefas em andamento.'}
            if any(j['agente'] == agent and j.get('receita_id') == recipe_id for j in active):
                return {'sucesso': False, 'erro': 'especialista_em_andamento', 'acao': 'Consulte a tarefa existente antes de iniciar outra.'}
            context = {'pedido_usuario': message[:4000], 'finalidade': purpose}
            price_targets = []
            if agent == 'pesquisa':
                pantry = self.call(owner, 'despensa_consultar', {'resumido': True})
                if not pantry.get('sucesso') or not isinstance(pantry.get('estoque'), list):
                    return {'sucesso': False, 'erro': 'despensa_indisponivel'}
                context['despensa'] = pantry
                if purpose in ['precos', 'execucao']:
                    progress = self.call(owner, 'receita_consultar_fluxo', {'receita_id': recipe_id})
                    if not progress.get('sucesso'):
                        return progress
                    context['fluxo'] = progress
                    if purpose == 'precos':
                        done = {r['ingrediente'].casefold() for r in progress.get('pesquisas_precos', [])}
                        price_targets = [r['ingrediente'] for r in progress.get('faltantes_precos', [])
                                         if r['ingrediente'].casefold() not in done]
                        context['faltantes_para_pesquisar'] = price_targets
                        # A próxima consulta precisa só dos faltantes. Recibos de
                        # preços já concluídos não devem estourar o contexto do filho.
                        context['despensa'] = {'orcamento':pantry.get('orcamento', {})}
                        context['fluxo'] = {'receita_id':recipe_id,
                                            'receita':{'nome':progress.get('receita', {}).get('nome', '')},
                                            'aceite':bool(progress.get('aceite'))}
                    elif purpose == 'execucao':
                        context['fluxo'] = {'receita_id':recipe_id,
                            'receita':{key:progress.get('receita', {}).get(key) for key in ['nome','fonte_url']}}
            else:
                progress = self.call(owner, 'receita_consultar_fluxo', {'receita_id': recipe_id})
                if not progress.get('sucesso'):
                    return progress
                context['fluxo'] = progress
                context['fluxo'] = {key:value for key,value in progress.items() if key not in
                                    ['pesquisas_precos','conferencia_execucao','precificacao','prato_cardapio']}
                context['fluxo']['receita'] = {key:value for key,value in progress.get('receita', {}).items()
                                               if key != 'modo_preparo'}
            job = {'id': secrets.token_urlsafe(18), 'token': secrets.token_urlsafe(24),
                   'dono': owner, 'turno': turn, 'chave': key, 'agente': agent,
                   'receita_id': recipe_id, 'estado': 'PENDING', 'eventos': [],
                   'finalidade': purpose,
                   'alvos_precos':price_targets,
                   'criada_em': time.time(), 'prazo': time.time() + (max(self.timeout, 480) if agent == 'pesquisa' and purpose == 'receita' else max(self.timeout, 420) if purpose == 'precos' else self.timeout),
                   'chamadas': 0, 'buscas': 0, 'falhas': {}, 'entregue': False}
            self.store.put(job)
        try:
            prompt = (ROOT / 'agentes' / (agent + '.md')).read_text()
            goal = 'TAREFA_DONA_MARIA=' + job['id'] + ' TOKEN=' + job['token'] + '\n' + prompt
            data = json.dumps(context, ensure_ascii=False)
            if len(data) > 30000:
                raise ValueError('Contexto do especialista excedeu o limite')
            handle = self.service.launch({'goal': goal, 'context': data, 'role': 'leaf',
                'allowed_toolsets': (TOOLSETS[agent],), 'parent_session_id': owner,
                'correlation_id': 'dona-maria-' + job['id']})
            with self.lock:
                job = self.store.get(job['id'])
                job['handle'] = handle.to_dict() if hasattr(handle, 'to_dict') else dict(handle)
                job['estado'] = 'RUNNING'
                self.store.put(job)
            return {'sucesso': True, 'tarefa_id': job['id'], 'estado': 'RUNNING', 'agente': agent,
                    'acao': 'Use dona_maria_agente_resultado para aguardar e receber os recibos. Não invente o resultado do especialista.'}
        except Exception as exc:
            with self.lock:
                job = self.store.get(job['id'])
                job.update(estado='FAILED', erro=str(exc), entregue=True)
                self.store.put(job)
            return {'sucesso': False, 'tarefa_id': job['id'], 'erro': 'especialista_indisponivel', 'detalhe': str(exc)}

    def bind_child(self, session, parent, message):
        match = re.search(r'TAREFA_DONA_MARIA=([\w-]+) TOKEN=([\w-]+)', str(message))
        if not match:
            return None
        with self.lock:
            job = self.store.get(match[1], parent)
            if not job or not secrets.compare_digest(job['token'], match[2]) or job['estado'] in TERMINAL:
                return None
            # Um contrato pertence a uma única sessão filha.
            if job.get('filho') and job['filho'] != session:
                return None
            job['filho'] = session
            self.children[session] = job['id']
            self.store.put(job)
            return job['agente']

    def child_role(self, session):
        with self.lock:
            job = self.store.get(self.children.get(session, ''))
        return job['agente'] if job else None

    def child_contract(self, session):
        with self.lock:
            job = self.store.get(self.children.get(session, ''))
        if not job:
            return None
        done = {e['result'].get('ingrediente', '').casefold() for e in job['eventos']
                if e['tool_name'] == PREFIX+'preco_pesquisar' and e['result'].get('pesquisa_realizada')}
        return {'id':job['id'], 'finalidade':job.get('finalidade'), 'receita_id':job.get('receita_id'),
                'comparacao_feita':any(e['tool_name'] == PREFIX+'despensa_verificar_receita'
                                      and e['result'].get('sucesso') for e in job['eventos']),
                'custos_pendentes':bool(cost_pending_receipts(job['eventos'])) if job['agente']=='custos' else False,
                'precificacao_feita':any(e['tool_name']==PREFIX+'receita_precificar' and e['result'].get('sucesso') for e in job['eventos']),
                'alvos_precos':[n for n in job.get('alvos_precos', []) if n.casefold() not in done]}

    def execute_cost(self, session, operation, arguments):
        with self.lock:
            job = self.store.get(self.children.get(session, ''))
        if not job or job['agente'] != 'custos':
            return {'sucesso':False, 'erro':'especialista_nao_autorizado'}
        arguments = dict(arguments)
        if operation != 'medidas_consultar':
            arguments['receita_id'] = job['receita_id']
        return self.execute(session, job['id'], operation, arguments)

    def bind_request_child(self, session, messages):
        """Filhos efêmeros não passam pelo hook pre_llm_call do Hermes."""
        for message in messages:
            content = message.get('content', '')
            if isinstance(content, list):
                content = '\n'.join(part.get('text', '') for part in content if isinstance(part, dict))
            match = re.search(r'TAREFA_DONA_MARIA=([\w-]+) TOKEN=([\w-]+)', str(content))
            if match:
                job = self.store.get(match[1])
                if job:
                    role = self.bind_child(session, job['dono'], content)
                    if role:
                        return role
        return None

    def call_limit(self, job):
        return max(self.max_calls, 96) if job['agente'] == 'pesquisa' and job.get('finalidade') == 'receita' else self.max_calls

    def child_exhausted(self, session):
        with self.lock:
            job = self.store.get(self.children.get(session, ''))
        return not job or (job['estado'] in TERMINAL or job['estado'] == 'CANCEL_REQUESTED'
                           or time.time() > job['prazo'] or job['chamadas'] >= self.call_limit(job))

    def child_needs_work(self, session):
        """Um resumo livre não substitui a busca, leitura e apresentação comprovadas."""
        if self.child_exhausted(session):
            return False
        with self.lock:
            job = self.store.get(self.children.get(session, ''))
        if job['agente'] == 'custos' and cost_pending_receipts(job['eventos']):
            return False
        if job.get('finalidade') == 'precos':
            done = {e.get('result', {}).get('ingrediente', '').casefold() for e in job['eventos']
                    if e.get('tool_name') == PREFIX+'preco_pesquisar' and e.get('result', {}).get('pesquisa_realizada')}
            return any(name.casefold() not in done for name in job.get('alvos_precos', []))
        expected = ('receita_conferir_execucao' if job.get('finalidade') == 'execucao' else
                    'web_search' if job.get('finalidade') == 'precos' else
                    'receita_apresentar' if job['agente'] == 'pesquisa' else 'receita_precificar')
        return not any(event.get('tool_name') == PREFIX+expected and event.get('result', {}).get('sucesso')
                       and (expected != 'receita_apresentar' or event['result'].get('fonte_conferida') is True)
                       for event in job['eventos'])

    def execute(self, session, identifier, operation, arguments, _recover=True):
        with self.lock:
            job = self.store.get(identifier)
            if not job or self.children.get(session) != identifier:
                return {'sucesso': False, 'erro': 'especialista_nao_autorizado'}
            if operation not in OPERATIONS[job['agente']]:
                return {'sucesso': False, 'erro': 'operacao_fora_do_papel'}
            if job.get('finalidade') == 'precos' and operation not in ['web_search', 'preco_pesquisar']:
                return {'sucesso': False, 'erro': 'cotacao_so_pesquisa_precos'}
            if operation == 'preco_pesquisar' and (not isinstance(arguments, dict) or job.get('finalidade') != 'precos'
                    or arguments.get('receita_id') != job.get('receita_id')
                    or arguments.get('ingrediente', '').casefold() not in [n.casefold() for n in job.get('alvos_precos', [])]):
                return {'sucesso':False,'erro':'preco_fora_do_contrato'}
            if operation == 'preco_pesquisar':
                prior = next((e['result'] for e in job['eventos'] if e['tool_name'] == PREFIX+operation
                              and e['result'].get('ingrediente', '').casefold() == arguments.get('ingrediente', '').casefold()
                              and e['result'].get('pesquisa_realizada')), None)
                if prior:
                    return prior
            if job.get('finalidade') == 'execucao' and (operation != 'receita_conferir_execucao' or not isinstance(arguments, dict) or arguments.get('receita_id') != job['receita_id']):
                return {'sucesso': False, 'erro': 'conferencia_exige_receita_escolhida'}
            if operation == 'receita_conferir_execucao' and job.get('finalidade') != 'execucao':
                return {'sucesso': False, 'erro': 'conferencia_exige_contrato_de_execucao'}
            if job['estado'] in TERMINAL or job['estado'] == 'CANCEL_REQUESTED' or time.time() > job['prazo']:
                return {'sucesso': False, 'erro': 'tarefa_encerrada_ou_expirada'}
            if not isinstance(arguments, dict) or len(json.dumps(arguments)) > 32000 or 'sessao' in arguments:
                return {'sucesso': False, 'erro': 'argumentos_invalidos'}
            if job['agente'] == 'custos' and operation != 'medidas_consultar' and arguments.get('receita_id') != job['receita_id']:
                return {'sucesso': False, 'erro': 'receita_de_outra_tarefa'}
            if 'evidencia_aceite' in arguments:
                return {'sucesso': False, 'erro': 'aceite_deve_vir_do_historico_real'}
            search_limit = 12 if job['agente'] == 'pesquisa' and job.get('finalidade') == 'receita' else 4
            if job['chamadas'] >= self.call_limit(job) or operation in ['web_search', 'receita_conferir_execucao'] and job['buscas'] >= search_limit:
                return {'sucesso': False, 'erro': 'limite_do_especialista'}
            signature = json.dumps([operation, arguments], sort_keys=True)
            if job['falhas'].get(signature, 0) >= 2:
                return {'sucesso': False, 'erro': 'falha_repetida'}
            job['chamadas'] += 1
            job['buscas'] += int(operation in ['web_search', 'receita_conferir_execucao'])
            self.store.put(job)
        result = self.call(job['dono'], operation, arguments)
        with self.lock:
            job = self.store.get(identifier)
            if result.get('sucesso') is not True:
                job['falhas'][signature] = job['falhas'].get(signature, 0) + 1
            # Guarde o diagnóstico das fontes, sem carregar páginas inteiras no contexto.
            if operation == 'receita_ler_fonte':
                job.setdefault('fontes_tentadas', {})[arguments['url']] = {
                    key:result[key] for key in ['sucesso', 'erro', 'detalhe'] if key in result}
            else:
                job['eventos'].append({'tool_name': PREFIX + operation, 'args': arguments, 'result': result})
            self.store.put(job)
        if (operation == 'web_search' and _recover
                and job['agente'] == 'pesquisa' and job.get('finalidade', 'receita') == 'receita'):
            return self.recover_recipe_search(session, identifier, arguments, result)
        return result

    def recover_recipe_search(self, session, identifier, arguments, result):
        """Uma fonte inválida descarta o link, nunca toda a pesquisa."""
        query = arguments.get('query', '')
        sites = ['tudogostoso.com.br', 'receiteria.com.br', 'receitasnestle.com.br',
                 'panelinha.com.br', 'receitas.globo.com', 'tudoreceitas.com',
                 'anamariabraga.globo.com', 'guiadacozinha.com.br',
                 'comidinhasdochef.com', 'acozinhalegal.com.br', 'terra.com.br']
        while not self.child_exhausted(session):
            if result.get('sucesso'):
                candidate = self.read_search_recipe(session, identifier, result)
                if candidate:
                    return dict(result, receita_validada=candidate,
                                proximo_passo='Uma receita completa já foi lida e validada. Encerre a tarefa sem pesquisar outras opções.')
            job = self.store.get(identifier)
            if job['buscas'] >= 12 or self.child_exhausted(session):
                break
            site = sites[min(max(job['buscas'] - 1, 0), len(sites) - 1)]
            refined = dict(arguments, query=query + ' ingredientes modo de preparo site:' + site, limit=5)
            result = self.execute(session, identifier, 'web_search', refined, _recover=False)
        return dict(result, pesquisa_esgotada=True,
                    acao='As fontes consultadas não forneceram uma receita validada dentro do prazo de pesquisa. Não invente ingredientes ou preparo.',
                    fontes_tentadas=self.store.get(identifier).get('fontes_tentadas', {}))

    def read_search_recipe(self, session, identifier, search):
        """Transcreve o Recipe estruturado diretamente, sem reconstrução pelo LLM."""
        payload = search.get('resultado_web', {})
        rows = payload.get('data', {}).get('web', [])
        seen = set()
        restrictions = re.findall(r'^NUNCA APRESENTE UMA RECEITA DE (.+)$',
                                  (ROOT / 'agentes' / 'pesquisa.md').read_text(), re.M | re.I)
        for row in rows:
            url = row.get('url')
            tried = self.store.get(identifier).get('fontes_tentadas', {})
            if not url or url in seen or url in tried or self.child_exhausted(session):
                continue
            seen.add(url)
            source = self.execute(session, identifier, 'receita_ler_fonte',
                                  {'pesquisa_id':search['pesquisa_id'], 'url':url})
            if source.get('sucesso') is not True:
                continue
            for recipe in source.get('conteudo', {}).get('receitas', []):
                description = ' '.join([recipe.get('nome', '')] + recipe.get('ingredientes', [])).casefold()
                if any(term.strip().rstrip('.').casefold() in description for term in restrictions):
                    continue
                if re.search(r'\bou\b|\b(?:opções|opcoes|versões|versoes)\b', str(recipe.get('nome', '')), re.I):
                    # Uma página com versões alternativas de pratos não é uma única sugestão.
                    continue
                # Use as listas integrais retornadas pela fonte já lida no MCP.
                value = {key:recipe.get(key) for key in ['nome', 'ingredientes', 'modo_preparo']}
                value.update(fonte_url=url, rendimento=recipe.get('rendimento') or 'não informado')
                receipt = self.execute(session, identifier, 'receita_apresentar',
                                       {'pesquisa_id':search['pesquisa_id'], 'receita':value})
                if receipt.get('sucesso') is True and receipt.get('fonte_conferida') is True:
                    return receipt
        return None

    def collect(self, owner, identifier, wait=0, cancel=False):
        job = self.store.get(identifier, owner)
        if not job:
            return {'sucesso': False, 'erro': 'tarefa_inexistente_nesta_sessao'}
        if 'resultado' in job:
            return job['resultado']
        if not job.get('handle'):
            answer = {'sucesso': False, 'tarefa_id': identifier, 'estado': 'UNKNOWN',
                      'erro': job.get('erro', 'lancamento_incompleto'),
                      'acao': 'Lançamento interrompido. Inicie outra tarefa explicitamente em um novo turno.'}
            with self.lock:
                job.update(estado='UNKNOWN', entregue=True, resultado=answer)
                self.store.put(job)
            return answer
        handle = job['handle']
        try:
            # Handles de outro processo não podem retomar nem relançar trabalho.
            connection = record(self.service.reconnect(handle))
            if not connection.get('connected'):
                raise ValueError('Execução não pode ser reconectada após reinício; solicite nova tarefa explicitamente')
            if cancel or time.time() > job['prazo']:
                self.service.cancel(handle, reason='Cancelada pelo coordenador ou prazo excedido')
                with self.lock:
                    job = self.store.get(identifier, owner)
                    job['estado'] = 'CANCEL_REQUESTED'
                    self.store.put(job)
            self.service.wait(handle, timeout_seconds=min(max(float(wait), 0), 10))
            result = record(self.service.result(handle))
        except Exception as exc:
            answer = {'sucesso': False, 'tarefa_id': identifier, 'estado': 'UNKNOWN', 'erro': str(exc),
                      'acao': 'A execução foi perdida. Inicie nova tarefa explicitamente; não suponha que o trabalho terminou.'}
            with self.lock:
                job = self.store.get(identifier, owner)
                job.update(estado='UNKNOWN', erro=str(exc), entregue=True, resultado=answer)
                self.store.put(job)
            return answer
        if not result.get('ready'):
            return {'sucesso': True, 'tarefa_id': identifier, 'estado': job['estado'], 'concluida': False,
                    'acao': 'O especialista ainda está executando. Aguarde até 10 segundos por consulta; não crie tarefa duplicada.'}
        with self.lock:
            job = self.store.get(identifier, owner)
            status = state_name(result.get('terminal_state', 'FAILED'))
            try:
                report = json.loads(result.get('summary') or '')
                valid = (isinstance(report, dict) and set(report) == {'estado', 'resumo', 'pendencias'}
                         and report['estado'] in ['concluido', 'pendente'] and isinstance(report['resumo'], str)
                         and isinstance(report['pendencias'], list) and all(isinstance(x, str) for x in report['pendencias']))
            except (ValueError, TypeError):
                valid = False
            receipts = job['eventos']
            expected = 'receita_apresentar' if job['agente'] == 'pesquisa' else 'receita_precificar'
            if job.get('finalidade') == 'precos':
                expected = 'preco_pesquisar'
            elif job.get('finalidade') == 'execucao':
                expected = 'receita_conferir_execucao'
            proved = any(e['tool_name'] == PREFIX + expected and e['result'].get('sucesso')
                         and (expected != 'receita_apresentar' or e['result'].get('fonte_conferida')) for e in receipts)
            if job.get('finalidade') == 'precos':
                names = {e['result'].get('ingrediente', '').casefold() for e in receipts
                         if e['tool_name'] == PREFIX+expected and e['result'].get('pesquisa_realizada')}
                proved = bool(names) and all(n.casefold() in names for n in job.get('alvos_precos', []))
            # O recibo determinístico comprova o trabalho; a formatação do resumo
            # livre não pode invalidar uma receita já conferida na fonte.
            success = status == 'SUCCEEDED' and proved
            pending = status == 'SUCCEEDED' and job['agente'] == 'custos' and bool(cost_pending_receipts(receipts)) and not success
            answer = {'sucesso': success or pending, 'tarefa_id': identifier, 'agente': job['agente'],
                      'estado': status, 'concluida': True, 'eventos': receipts,
                      'resultado_validado': success, 'uso': result.get('usage_metadata', {})}
            answer['pendencia_validada'] = pending
            answer['resumo_estruturado'] = valid
            if result.get('error_message'):
                answer['diagnostico_execucao'] = result['error_message']
            if not success and not pending:
                answer['erro'] = 'especialista_sem_resultado_validado'
                answer['acao'] = 'Use somente as pendências dos recibos MCP. Sem comprovante, não apresente receita/preço como concluídos.'
            job.update(estado=status, entregue=True, resultado=answer)
            self.store.put(job)
            return answer
