import importlib.util
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('interface_server',Path(__file__).parents[1]/'server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root/'src'
        self.source.mkdir()
        self.original = (server.SOURCE/'despensa_dona_maria.xlsx').read_bytes()
        (self.source/'despensa_dona_maria.xlsx').write_bytes(self.original)
        self.dbpath = self.root/'state.sqlite3'
        with sqlite3.connect(server.memory.DEFAULT_DB.resolve().as_uri()+'?mode=ro',uri=True) as source, sqlite3.connect(self.dbpath) as dest:
            source.backup(dest)
        self.patch = patch.multiple(server, SOURCE=self.source,RUNTIME=self.root/'interface')
        self.patch.start()
        self.dbpatch = patch.object(server.memory,'DEFAULT_DB',self.dbpath)
        self.dbpatch.start()

    def tearDown(self):
        self.dbpatch.stop(); self.patch.stop(); self.temp.cleanup()

    def values(self):
        with sqlite3.connect(self.dbpath) as db:
            return (db.execute('select saldo_centavos from despensa_controle').fetchone()[0],db.execute('select count(*) from despensa_compras').fetchone()[0],db.execute('select revisao from metadata').fetchone()[0])

    def test_real_workbook_import_preserves_budget_and_purchase_history(self):
        before=self.values()
        # Inicializar tabelas depende do arquivo de conversões original.
        result=server.import_workbook(self.original,'nova-despensa.xlsx')
        after=self.values()
        self.assertEqual(result['itens'],37)
        self.assertEqual(before[:2],after[:2])
        self.assertEqual(after[2],before[2]+1)
        self.assertEqual(server.snapshot()['arquivo'],'nova-despensa.xlsx')
        self.assertEqual(len(list((server.RUNTIME/'backups').glob('*.sqlite3'))),1)
        with sqlite3.connect(self.dbpath) as db:
            self.assertEqual(db.execute("select count(*) from despensa_cotacoes where estado='pendente'").fetchone()[0],0)

    def test_invalid_upload_keeps_existing_data(self):
        before=self.values()
        with self.assertRaises(Exception): server.import_workbook(b'invalid','bad.xlsx')
        self.assertEqual(before,self.values())
        self.assertEqual((self.source/'despensa_dona_maria.xlsx').read_bytes(),self.original)

    def test_upload_blocked_during_agent_execution(self):
        server.MUTATION.acquire()
        try:
            with self.assertRaisesRegex(ValueError,'Aguarde'):
                server.import_workbook(self.original,'nova.xlsx')
        finally: server.MUTATION.release()

    def test_snapshot_does_not_write_operational_database(self):
        before=self.dbpath.read_bytes()
        server.snapshot()
        self.assertEqual(before,self.dbpath.read_bytes())

    def test_unknown_extension_is_rejected(self):
        with self.assertRaises(ValueError):server.import_workbook(self.original,'arquivo.xls')


class ChatProgressTests(unittest.TestCase):
    setUp = ImportTests.setUp
    tearDown = ImportTests.tearDown

    def job(self):
        import uuid
        ident = uuid.uuid4().hex
        conversation = str(uuid.uuid4())
        server.JOBS[ident] = dict(status='running', text='', activity='', conversation=conversation)
        return ident, conversation, server.JOBS[ident]

    def test_history_hides_old_execution_logs_and_new_status_is_temporary(self):
        ident,conversation,job=self.job()
        server.append(conversation,'user','Uma receita')
        server.append(conversation,'progress','Registro de execução antigo')
        server.append(conversation,'assistant','Receita completa')
        server.progress(job,'Consultando a despensa')
        self.assertEqual([(m['role'],m['text']) for m in server.history(conversation)],
                         [('user','Uma receita'),('assistant','Receita completa')])
        self.assertEqual(job['activity'],'Consultando a despensa')
        with sqlite3.connect(server.RUNTIME/'chat.sqlite3') as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE conversation=? AND role='progress'",(conversation,)).fetchone()[0],1)

    def test_steps_are_available_before_the_agent_finishes(self):
        import threading
        ident, conversation, job = self.job()
        entered, release = threading.Event(), threading.Event()
        def turn(current, text, deadline):
            server.progress(current, 'Pesquisa — conferindo a receita na fonte')
            entered.set()
            release.wait(5)
            return 'Receita conferida.'
        server.MUTATION.acquire()
        with patch.object(server, 'run_chat_turn', side_effect=turn), patch.object(server, 'task_snapshot', return_value=[]):
            worker = threading.Thread(target=server.chat_worker,args=(ident,conversation,'Uma ideia de prato'))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(job['status'],'running')
                self.assertTrue(any('conferindo' in p['text'] for p in job['progress']))
                self.assertFalse(any(m['role']=='progress' for m in server.history(conversation)))
            finally:
                release.set();worker.join(5)
            self.assertEqual(job['status'],'done')

    def test_pending_specialist_is_collected_without_repeating_user_request(self):
        ident, conversation, job = self.job()
        task={'id':'existing-task','agente':'pesquisa','estado':'RUNNING','entregue':False,'criada_em':server.time.time()+1}
        turns=[]
        def turn(current,text,deadline):
            turns.append(text)
            if len(turns)==1:
                return 'Estou aguardando o especialista.'
            self.assertEqual(current['status'],'running')
            self.assertIn('existing-task',text)
            self.assertIn('Não relance especialistas',text)
            task.update(estado='SUCCEEDED',entregue=True,resultado={'resultado_validado':True})
            return 'Aqui está a receita validada.'
        server.MUTATION.acquire()
        with patch.object(server,'run_chat_turn',side_effect=turn),patch.object(server,'task_snapshot',side_effect=lambda session:[task]):
            server.chat_worker(ident,conversation,'Uma receita, por favor')
        self.assertEqual(len(turns),2)
        self.assertEqual(job['status'],'done')
        self.assertIn('validada',job['text'])
        messages=server.history(conversation)
        self.assertEqual(len([m for m in messages if m['role']=='user']),1)
        self.assertEqual([m['text'] for m in messages if m['role']=='assistant'],['Aqui está a receita validada.'])

    def test_unvalidated_specialist_does_not_look_like_work_in_progress(self):
        ident, conversation, job = self.job()
        task={'id':'bad-task','agente':'pesquisa','estado':'SUCCEEDED','entregue':True,'criada_em':server.time.time()+1,
              'resultado':{'resultado_validado':False}}
        server.MUTATION.acquire()
        with patch.object(server,'run_chat_turn',return_value='Estou aguardando o especialista.'),patch.object(server,'task_snapshot',return_value=[task]):
            server.chat_worker(ident,conversation,'Uma receita')
        self.assertEqual(job['status'],'error')
        self.assertIn('Não foi possível obter uma receita completa',job['error'])
        self.assertNotIn('especialista',job['error'])
        self.assertFalse(any(m['role']=='assistant' for m in server.history(conversation)))

    def test_proved_cost_pending_finishes_with_question_instead_of_generic_error(self):
        ident,conversation,job=self.job()
        task={'id':'pending-cost','agente':'custos','estado':'SUCCEEDED','entregue':True,
              'criada_em':server.time.time()+1,
              'resultado':{'resultado_validado':False,'pendencia_validada':True}}
        text='Para calcular o preço, qual a quantidade em gramas de molho de tomate?'
        server.MUTATION.acquire()
        with patch.object(server,'run_chat_turn',return_value=text),patch.object(server,'task_snapshot',return_value=[task]):
            server.chat_worker(ident,conversation,'Pode calcular o preço')
        self.assertEqual(job['status'],'done')
        self.assertEqual(job['text'],text)
        self.assertNotIn('error',job)
        self.assertTrue(any('aguardando dados para precificar' in p['text'] for p in job['progress']))

    def test_verified_recipe_survives_failed_attempt_and_replaces_free_answer(self):
        ident, conversation, job = self.job()
        recipe = {'nome':'Arroz', 'ingredientes':['1 xícara de arroz', '2 xícaras de água'],
                  'modo_preparo':['Cozinhe o arroz na água.'], 'fonte_url':'https://example.org/arroz', 'rendimento':'2 porções'}
        tasks = [{'id':'failed', 'agente':'pesquisa', 'estado':'SUCCEEDED', 'entregue':True,
                  'criada_em':server.time.time()+1, 'resultado':{'resultado_validado':False}},
                 {'id':'valid', 'agente':'pesquisa', 'estado':'SUCCEEDED', 'entregue':True,
                  'criada_em':server.time.time()+2, 'resultado':{'resultado_validado':True},
                  'eventos':[{'tool_name':'mcp__dona_maria_memoria__receita_apresentar',
                              'result':{'sucesso':True,'fonte_conferida':True,'receita':recipe}}]}]
        server.MUTATION.acquire()
        with patch.object(server,'run_chat_turn',return_value='Aqui estão três opções. Quer calcular preços?'), patch.object(server,'task_snapshot',return_value=tasks):
            server.chat_worker(ident,conversation,'Uma receita')
        self.assertEqual(job['status'],'done')
        self.assertTrue(job['text'].startswith('**Arroz**'))
        self.assertIn('1 xícara de arroz',job['text'])
        self.assertIn('**Modo de preparo**',job['text'])
        self.assertEqual(job['text'].count('**Referência:**'),1)
        self.assertNotIn('opções',job['text'])
        self.assertNotIn('preços',job['text'])

    def test_task_receipts_are_reported_once_and_filtered_by_session(self):
        import json
        ident, conversation, job = self.job()
        job.update(session='my-session',started_at=server.time.time(),progress=[])
        path=self.source/'dados';path.mkdir()
        with sqlite3.connect(path/'tarefas_agentes.sqlite3') as db:
            db.execute('CREATE TABLE tarefas(dono TEXT,dados TEXT)')
            for owner in ['my-session','other-session']:
                task={'id':owner,'agente':'pesquisa','estado':'RUNNING','eventos':[{'tool_name':'web_search','result':{'sucesso':True}}]}
                db.execute('INSERT INTO tarefas VALUES(?,?)',(owner,json.dumps(task)))
        server.observe_tasks(job);server.observe_tasks(job)
        self.assertEqual(len(job['progress']),2)
        self.assertEqual(len(server.history(conversation)),0)
        self.assertTrue(any('Pesquisando fontes' in e['text'] for e in job['progress']))

    def test_cli_session_and_tool_events_are_captured_before_terminal_result(self):
        import io,json
        from unittest.mock import Mock
        ident,conversation,job=self.job()
        job.update(started_at=server.time.time(),progress=[])
        process=Mock()
        process.stderr=io.StringIO('Session: local-live-session\n')
        process.stdout=io.StringIO('\n'.join(json.dumps(e) for e in [
            {'type':'tool_use','name':'dona_maria_agente_resultado'},
            {'type':'tool_result','name':'dona_maria_agente_resultado','is_error':False},
            {'type':'result','exit_code':0,'text':'Recebido.'}]))
        process.wait.return_value=0;process.poll.return_value=0
        with patch.object(server.subprocess,'Popen',return_value=process):
            answer=server.run_chat_turn(job,'Mensagem real',server.time.monotonic()+30)
        self.assertEqual(answer,'Recebido.')
        self.assertEqual(job['session'],'local-live-session')
        self.assertEqual(len(job['progress']),2)
        self.assertIn('retorno recebido',job['progress'][1]['text'])


if __name__=='__main__':unittest.main()
