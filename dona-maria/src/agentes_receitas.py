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
OPERATIONS = {
    'pesquisa': frozenset(['web_search', 'receita_ler_fonte', 'receita_apresentar', 'receita_conferir_execucao']),
    'custos': frozenset(['medidas_consultar', 'despensa_verificar_receita', 'receita_precificar']),
}
ROUTED = frozenset(PREFIX + op for ops in OPERATIONS.values() for op in ops)
CONTROL_TOOLS = frozenset(['dona_maria_delegar', 'dona_maria_agente_resultado'])
TERMINAL = frozenset(['SUCCEEDED', 'FAILED', 'INTERRUPTED', 'CANCELLED', 'UNKNOWN'])


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
        return [{'tarefa_id': j['id'], 'agente': j['agente'], 'estado': j['estado']}
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
            else:
                progress = self.call(owner, 'receita_consultar_fluxo', {'receita_id': recipe_id})
                if not progress.get('sucesso'):
                    return progress
                context['fluxo'] = progress
            job = {'id': secrets.token_urlsafe(18), 'token': secrets.token_urlsafe(24),
                   'dono': owner, 'turno': turn, 'chave': key, 'agente': agent,
                   'receita_id': recipe_id, 'estado': 'PENDING', 'eventos': [],
                   'finalidade': purpose,
                   'criada_em': time.time(), 'prazo': time.time() + self.timeout,
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

    def child_exhausted(self, session):
        with self.lock:
            job = self.store.get(self.children.get(session, ''))
        return not job or (job['estado'] in TERMINAL or job['estado'] == 'CANCEL_REQUESTED'
                           or time.time() > job['prazo'] or job['chamadas'] >= self.max_calls
                           or any(count >= 2 for count in job['falhas'].values()))

    def execute(self, session, identifier, operation, arguments):
        with self.lock:
            job = self.store.get(identifier)
            if not job or self.children.get(session) != identifier:
                return {'sucesso': False, 'erro': 'especialista_nao_autorizado'}
            if operation not in OPERATIONS[job['agente']]:
                return {'sucesso': False, 'erro': 'operacao_fora_do_papel'}
            if job.get('finalidade') == 'precos' and operation != 'web_search':
                return {'sucesso': False, 'erro': 'cotacao_so_pesquisa_precos'}
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
            if job['chamadas'] >= self.max_calls or operation in ['web_search', 'receita_conferir_execucao'] and job['buscas'] >= 4:
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
            # Só estes recibos são úteis ao coordenador; páginas completas ficam no MCP.
            if operation not in ['receita_ler_fonte', 'medidas_consultar']:
                job['eventos'].append({'tool_name': PREFIX + operation, 'args': arguments, 'result': result})
            self.store.put(job)
        return result

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
                expected = 'web_search'
            elif job.get('finalidade') == 'execucao':
                expected = 'receita_conferir_execucao'
            proved = any(e['tool_name'] == PREFIX + expected and e['result'].get('sucesso')
                         and (expected != 'receita_apresentar' or e['result'].get('fonte_conferida')) for e in receipts)
            success = status == 'SUCCEEDED' and valid and proved
            answer = {'sucesso': success, 'tarefa_id': identifier, 'agente': job['agente'],
                      'estado': status, 'concluida': True, 'eventos': receipts,
                      'resultado_validado': success, 'uso': result.get('usage_metadata', {})}
            if not success:
                answer['erro'] = 'especialista_sem_resultado_validado'
                answer['acao'] = 'Use somente as pendências dos recibos MCP. Sem comprovante, não apresente receita/preço como concluídos.'
            job.update(estado=status, entregue=True, resultado=answer)
            self.store.put(job)
            return answer
