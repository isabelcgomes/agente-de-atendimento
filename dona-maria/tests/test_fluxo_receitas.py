"""Regressões do atendimento; SQLite temporário e rede/especialistas simulados."""
import json
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import fluxo_receitas as flow
import memoria_agente as backend
import plugin_receitas as plugin


class RecipeFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        history = Path(self.tmp.name) / 'history.db'
        self.history = sqlite3.connect(history)
        self.addCleanup(self.history.close)
        self.history.executescript('''CREATE TABLE sessions(id TEXT, cwd TEXT);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
              content TEXT, tool_name TEXT, tool_calls TEXT, timestamp REAL, active INTEGER);''')
        self.history.execute('INSERT INTO sessions VALUES(?,?)', ('test', str(flow.ROOT)))
        self.history.commit()
        self.patcher = patch.object(flow, 'HISTORY', history)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.db = backend.connect(Path(self.tmp.name) / 'memory.db')
        self.addCleanup(self.db.close)
        backend._SEARCHES.clear(); backend._CANDIDATES.clear(); backend._SOURCES.clear()
        self.recipe = {'nome': 'Tomate cortado', 'fonte_url': 'https://example.org/tomate',
                       'ingredientes': ['100g de tomate'],
                       'modo_preparo': ['Corte o tomate com uma faca.'], 'rendimento': '1 porção'}
        self.search = {'success': True, 'data': {'web': [{'url': self.recipe['fonte_url']}]}}
        self.network = patch.object(backend, '_live_search', return_value=self.search)
        self.network.start(); self.addCleanup(self.network.stop)
        self.source = patch.object(backend.fontes, 'read', return_value={'receitas': [self.recipe]})
        self.source.start(); self.addCleanup(self.source.stop)
        self.spec = {'lote': '1 porção',
                     'equipamentos': [{'chave': 'faca', 'igual': True, 'pergunta': 'Tem faca?'}],
                     'habilidades': [{'chave': 'cortar', 'igual': True, 'pergunta': 'Sabe cortar?'}],
                     'operacao': {'energia': False, 'gas': False, 'tempo_minutos': 5,
                                  'geladeira_litros': 1, 'bancada_livre': True}}

    def message(self, role, content='', tool=None, calls=None):
        self.history.execute('INSERT INTO messages VALUES(NULL,?,?,?,?,?,?,1)',
                             ('test', role, content, tool, json.dumps(calls) if calls else None, time.time()))
        self.history.commit()

    def api(self, name, **args):
        self.message('assistant', calls=[{'name': 'mcp__dona_maria_memoria__'+name, 'arguments': args}])
        result = backend.dispatch(self.db, name, dict(args, sessao='test'))
        self.message('tool', json.dumps(result), 'mcp__dona_maria_memoria__'+name)
        return result

    def mcp(self, method, params=None):
        request = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}
        outgoing = io.StringIO()
        backend.serve(self.db, io.StringIO(json.dumps(request) + '\n'), outgoing)
        response = json.loads(outgoing.getvalue())
        self.assertNotIn('error', response, response)
        return response['result']

    def mcp_api(self, name, **args):
        self.message('assistant', calls=[{'name': 'mcp__dona_maria_memoria__'+name, 'arguments': args}])
        result = self.mcp('tools/call', {'name': name, 'arguments': dict(args, sessao='test')})
        self.assertFalse(result['isError'], result)
        value = json.loads(result['content'][0]['text'])
        self.message('tool', json.dumps(value), 'mcp__dona_maria_memoria__'+name)
        return value

    def test_mcp_manifest_exposes_source_execution_and_approval_with_expiry(self):
        result = self.mcp('tools/list')
        names = {tool['name'] for tool in result['tools']}
        self.assertTrue({'receita_ler_fonte', 'receita_conferir_execucao', 'receita_aprovar',
                         'cardapio_adicionar', 'cardapio_consultar'}.issubset(names))
        self.assertGreater(result['ttlMs'], 0)
        self.assertLessEqual(result['ttlMs'], 300_000)

    def test_mcp_validates_source_and_production_without_plugin(self):
        # Todas as etapas atravessam JSON-RPC; apenas rede é simulada.
        search = self.mcp_api('web_search', query='tomate')
        source = self.mcp_api('receita_ler_fonte', pesquisa_id=search['pesquisa_id'],
                              url=self.recipe['fonte_url'])
        self.assertTrue(source['sucesso'])
        presented = self.mcp_api('receita_apresentar', pesquisa_id=search['pesquisa_id'], receita=self.recipe)
        self.assertTrue(presented['fonte_conferida'])
        self.facts()
        checked = self.mcp_api('receita_conferir_execucao', receita_id=presented['receita_id'])
        self.assertTrue(checked['sucesso'])
        self.assertTrue(checked['fonte_conferida'])
        capacity = self.mcp_api('receita_verificar_producao', receita_id=presented['receita_id'], requisitos=self.spec)
        self.assertTrue(capacity['producao_validada'], capacity)

    def test_mcp_unread_source_still_blocks_production(self):
        search = self.mcp_api('web_search', query='tomate')
        presented = self.mcp_api('receita_apresentar', pesquisa_id=search['pesquisa_id'], receita=self.recipe)
        self.assertFalse(presented['fonte_conferida'])
        self.facts()
        capacity = self.mcp_api('receita_verificar_producao', receita_id=presented['receita_id'], requisitos=self.spec)
        self.assertFalse(capacity['producao_validada'])
        self.assertEqual(capacity['erro'], 'preparo_nao_conferido')

    def test_mcp_changed_source_revokes_validation(self):
        self.present()
        self.facts()
        self.source.stop()
        with patch.object(backend.fontes, 'read', return_value={'receitas': []}):
            checked = self.mcp_api('receita_conferir_execucao', receita_id=self.identifier)
        self.assertFalse(checked['sucesso'])
        capacity = self.mcp_api('receita_verificar_producao', receita_id=self.identifier, requisitos=self.spec)
        self.assertFalse(capacity['producao_validada'])
        self.assertEqual(capacity['erro'], 'preparo_nao_conferido')

    def present(self, answer='gosto'):
        search = self.api('web_search', query='tomate')
        self.api('receita_ler_fonte', pesquisa_id=search['pesquisa_id'], url=self.recipe['fonte_url'])
        presented = self.api('receita_apresentar', pesquisa_id=search['pesquisa_id'], receita=self.recipe)
        self.assertTrue(presented['fonte_conferida'])
        self.identifier = presented['receita_id']
        self.message('assistant', 'Você gosta de cozinhar Tomate cortado?')
        if answer is not None: self.message('user', answer)
        return presented

    def facts(self, missing=None, negative=None):
        for group, key, value in [('equipamentos', 'faca', True), ('habilidades', 'cortar', True),
                                  ('operacao', 'tempo_por_cozinhada_minutos', 30),
                                  ('operacao', 'espaco_geladeira_litros', 10),
                                  ('operacao', 'bancada_livre', True)]:
            if key == missing: continue
            backend.registrar(self.db, group, key, False if key == negative else value,
                              'confirmado', 'dona_maria', 'Declaração da fixture', backend.revision(self.db))

    def price(self):
        self.api('receita_verificar_producao', receita_id=self.identifier, requisitos=self.spec)
        return self.api('receita_precificar', receita_id=self.identifier,
                        ingredientes=[{'indice_receita': 0, 'ingrediente': 'Tomate', 'quantidade': 100, 'unidade': 'g'}])

    def test_sim_answers_initial_recipe_question(self):
        self.present('sim')
        self.assertEqual(flow.accepted(self.identifier, 'test')['evidencia'], 'sim')

    def test_equipment_confirmation_does_not_accept_recipe(self):
        self.present(None)
        self.message('assistant', 'Você tem uma faca?')
        self.message('user', 'sim')
        self.assertIsNone(flow.accepted(self.identifier, 'test'))

    def test_rejection_revokes_interest(self):
        self.present()
        self.message('user', 'não gosto')
        self.assertIsNone(flow.accepted(self.identifier, 'test'))

    def test_technical_representation_keeps_id_and_acceptance(self):
        self.present('gosto')
        first = self.identifier
        search = self.api('web_search', query='tomate')
        self.api('receita_ler_fonte', pesquisa_id=search['pesquisa_id'], url=self.recipe['fonte_url'])
        again = self.api('receita_apresentar', pesquisa_id=search['pesquisa_id'], receita=self.recipe)
        self.assertEqual(again['receita_id'], first)
        self.assertTrue(again['aceite'])
        self.assertEqual(again['perguntas_feedback'], [])

    def test_other_recipe_does_not_inherit_acceptance(self):
        self.present()
        search = self.api('web_search', query='tomate')
        other = dict(self.recipe, nome='Outra receita', ingredientes=['200g de tomate'])
        again = self.api('receita_apresentar', pesquisa_id=search['pesquisa_id'], receita=other)
        self.assertNotEqual(again['receita_id'], self.identifier)
        self.assertIsNone(again['aceite'])

    def test_capacity_precedes_ingredient_normalization(self):
        self.present()
        progress = self.api('receita_consultar_fluxo')
        self.assertEqual(progress['etapa'], 'verificar_capacidade')

    def test_known_capabilities_need_no_questions(self):
        self.present(); self.facts()
        capacity = self.api('receita_verificar_producao', receita_id=self.identifier, requisitos=self.spec)
        self.assertTrue(capacity['producao_validada'])
        self.assertEqual(capacity['perguntas'], [])
        self.assertEqual(self.api('receita_consultar_fluxo')['etapa'], 'normalizar_ingredientes')

    def test_only_unknown_skill_is_asked_then_revalidated(self):
        self.present(); self.facts(missing='cortar')
        capacity = self.api('receita_verificar_producao', receita_id=self.identifier, requisitos=self.spec)
        self.assertEqual(capacity['perguntas'], ['Sabe cortar?'])
        self.assertFalse(capacity['producao_validada'])
        self.message('user', 'Sim, sei cortar os ingredientes')
        saved = self.api('memoria_registrar', grupo='habilidades', chave='cortar', valor_json=True,
                         status='confirmado', fonte='dona_maria', evidencia='Sim, sei cortar os ingredientes',
                         revisao_esperada=backend.revision(self.db))
        self.assertTrue(saved['sucesso'])
        capacity = self.api('receita_verificar_producao', receita_id=self.identifier, requisitos=self.spec)
        self.assertTrue(capacity['producao_validada'])
        self.assertEqual(flow.accepted(self.identifier, 'test')['evidencia'], 'gosto')

    def test_negative_skill_blocks_pricing(self):
        self.present(); self.facts(negative='cortar')
        result = self.price()
        self.assertFalse(result['sucesso'])
        self.assertEqual(result['erro'], 'producao_nao_validada')

    def test_restart_preserves_recipe_acceptance_and_plan(self):
        self.present('sim'); self.facts()
        self.api('receita_verificar_producao', receita_id=self.identifier, requisitos=self.spec)
        backend._CANDIDATES.clear(); backend._SOURCES.clear(); backend._SEARCHES.clear()
        progress = self.api('receita_consultar_fluxo')
        self.assertEqual(progress['receita_id'], self.identifier)
        self.assertEqual(progress['aceite']['evidencia'], 'sim')
        self.assertEqual(progress['etapa'], 'normalizar_ingredientes')
        self.assertTrue(self.price()['sucesso'])

    def test_price_choice_saves_menu_once_without_save_confirmation(self):
        self.present(); self.facts()
        pricing = self.price()
        self.assertTrue(pricing['sucesso'])
        self.assertEqual(self.api('cardapio_consultar')['pratos'], [])
        self.message('assistant', plugin.render_pricing(pricing))
        self.message('user', 'Quero o cenário 2')
        args = dict(precificacao_id=pricing['precificacao_id'], cenario=2, evidencia_usuario='Quero o cenário 2')
        saved = self.api('cardapio_adicionar', **args)
        self.assertTrue(saved['adicionado'])
        self.assertTrue(saved['alterado'])
        self.assertFalse(self.api('cardapio_adicionar', **args)['alterado'])
        self.assertEqual(len(self.api('cardapio_consultar')['pratos']), 1)

    def test_liking_recipe_does_not_choose_price(self):
        self.present(); self.facts()
        pricing = self.price()
        self.message('assistant', plugin.render_pricing(pricing))
        self.message('user', 'gostei')
        result = self.api('cardapio_adicionar', precificacao_id=pricing['precificacao_id'],
                          cenario=1, evidencia_usuario='gostei')
        self.assertEqual(result['erro'], 'escolha_de_preco_necessaria')

    def test_automatic_costs_start_once_only_after_capacity(self):
        self.present(); self.facts()
        state = {'message': 'gosto', 'delegations': []}
        coordinator = Mock()
        coordinator.start.return_value = {'sucesso': True, 'tarefa_id': 'costs'}
        with patch.object(plugin, '_coordinator', coordinator):
            plugin.advance_automatically(state, 'test', 'turn', self.api('receita_consultar_fluxo'))
            coordinator.start.assert_not_called()
            self.api('receita_verificar_producao', receita_id=self.identifier, requisitos=self.spec)
            progress = self.api('receita_consultar_fluxo')
            plugin.advance_automatically(state, 'test', 'turn', progress)
            plugin.advance_automatically(state, 'test', 'turn', progress)
            coordinator.start.assert_called_once_with('custos', 'test', 'turn', 'gosto', self.identifier)

    def test_automatic_menu_uses_real_price_consent(self):
        self.present(); self.facts()
        pricing = self.price()
        self.message('assistant', plugin.render_pricing(pricing))
        self.message('user', 'cenário 3')
        state = {'message': 'cenário 3', 'delegations': []}
        def handle(name, args, **kwargs):
            return self.api(name.removeprefix('mcp__dona_maria_memoria__'), **args)
        module = type(sys)('model_tools'); module.handle_function_call = Mock(side_effect=handle)
        progress = self.api('receita_consultar_fluxo')
        with patch.object(plugin, '_coordinator', Mock()), patch.dict(sys.modules, model_tools=module):
            plugin.advance_automatically(state, 'test', 'turn', progress)
        self.assertTrue(state['menu_result']['adicionado'])
        module.handle_function_call.assert_called_once()

    def test_initial_sim_triggers_execution_hook_without_extra_question(self):
        self.present('sim')
        coordinator = Mock()
        coordinator.pending.return_value = []
        with patch.object(plugin, 'project_session', return_value=True), \
             patch.object(plugin, 'call_flow', side_effect=lambda *a: self.api('receita_consultar_fluxo')), \
             patch.object(plugin, 'call_execution_check', return_value={'sucesso': True, 'tarefa_id': 'execution'}) as check, \
             patch.object(plugin, '_coordinator', coordinator):
            plugin.before_turn(session_id='test', turn_id='initial-sim', user_message='sim')
            check.assert_called_once_with(self.identifier, 'sim', 'test', 'initial-sim')
            self.assertTrue(plugin._turns[('test', 'initial-sim')]['automatic_interest'])
        plugin._turns.clear()

    def test_capacity_hook_launches_costs_in_same_turn(self):
        self.present(); self.facts()
        coordinator = Mock()
        coordinator.pending.return_value = []
        coordinator.start.return_value = {'sucesso': True, 'tarefa_id': 'costs'}
        with patch.object(plugin, 'project_session', return_value=True), \
             patch.object(plugin, 'call_flow', side_effect=lambda *a: self.api('receita_consultar_fluxo')), \
             patch.object(plugin, 'call_execution_check', return_value={'sucesso': True, 'tarefa_id': 'execution'}), \
             patch.object(plugin, '_coordinator', coordinator):
            plugin.before_turn(session_id='test', turn_id='capacity', user_message='gosto')
            capacity = self.api('receita_verificar_producao', receita_id=self.identifier, requisitos=self.spec)
            plugin.after_tool(tool_name='mcp__dona_maria_memoria__receita_verificar_producao',
                              result=capacity, session_id='test', turn_id='capacity')
            coordinator.start.assert_called_once_with('custos', 'test', 'capacity', 'gosto', self.identifier)
            request = plugin.limit_next_request(request={'messages': []}, session_id='test', turn_id='capacity')
            self.assertEqual(request['reason'], 'custos_automaticos')
            self.assertIn('costs', request['request']['messages'][-1]['content'])
        plugin._turns.clear()

    def test_memory_answer_hook_resumes_costs_without_new_acceptance(self):
        self.present(); self.facts(missing='cortar')
        self.api('receita_verificar_producao', receita_id=self.identifier, requisitos=self.spec)
        self.message('user', 'Sim, sei cortar os ingredientes')
        coordinator = Mock(); coordinator.pending.return_value = []
        coordinator.start.return_value = {'sucesso': True, 'tarefa_id': 'costs'}
        with patch.object(plugin, 'project_session', return_value=True), \
             patch.object(plugin, 'call_flow', side_effect=lambda *a: self.api('receita_consultar_fluxo')), \
             patch.object(plugin, 'call_execution_check', return_value={'sucesso': True, 'tarefa_id': 'execution'}), \
             patch.object(plugin, '_coordinator', coordinator):
            plugin.before_turn(session_id='test', turn_id='answer', user_message='Sim, sei cortar os ingredientes')
            saved = self.api('memoria_registrar', grupo='habilidades', chave='cortar', valor_json=True,
                             status='confirmado', fonte='dona_maria', evidencia='Sim, sei cortar os ingredientes',
                             revisao_esperada=backend.revision(self.db))
            plugin.after_tool(tool_name='mcp__dona_maria_memoria__memoria_registrar',
                              result=saved, session_id='test', turn_id='answer')
            coordinator.start.assert_called_once_with('custos', 'test', 'answer',
                                                       'Sim, sei cortar os ingredientes', self.identifier)
        plugin._turns.clear()

    def proactive_hooks(self, pantry=None):
        from contextlib import ExitStack
        stack = ExitStack()
        coordinator = Mock(); coordinator.pending.return_value = []
        stack.enter_context(patch.object(plugin, '_coordinator', coordinator))
        stack.enter_context(patch.object(plugin, 'project_session', return_value=True))
        stack.enter_context(patch.object(plugin, 'call_flow', side_effect=lambda *a: self.api('receita_consultar_fluxo')))
        stack.enter_context(patch.object(plugin, 'call_pantry', side_effect=lambda *a: pantry if pantry is not None else self.api('despensa_consultar')))
        research = stack.enter_context(patch.object(plugin, 'call_research', return_value={'sucesso': True, 'tarefa_id': 'research'}))
        self.addCleanup(plugin._turns.clear)
        self.addCleanup(stack.close)
        return research, coordinator

    def test_greeting_starts_stock_research_without_recipe_request(self):
        research, _ = self.proactive_hooks()
        for index, greeting in enumerate(['Oi', 'Olá, Hermes! Tudo bem?', 'Vamos começar', 'Estou pronta', 'Não sei']):
            with self.subTest(greeting=greeting):
                result = plugin.before_turn(session_id='test', turn_id='hello-'+str(index), user_message=greeting)
                self.assertIn('Não pergunte o que a usuária', result['context'])
                self.assertTrue(plugin._turns[('test', 'hello-'+str(index))]['required'])
        self.assertEqual(research.call_count, 5)
        self.assertIn('receita caseira com ', research.call_args.args[0])

    def test_stock_ready_starts_recipe_research_without_preference(self):
        research, _ = self.proactive_hooks()
        plugin.before_turn(session_id='test', turn_id='stock-ready', user_message='Meu estoque está abastecido')
        research.assert_called_once()
        self.assertIn('ingredientes modo de preparo', research.call_args.args[0])

    def test_specific_tasks_and_opt_out_do_not_start_suggestions(self):
        research, _ = self.proactive_hooks()
        for index, message in enumerate(['Mostre apenas meu estoque', 'Não quero sugestões',
                                         'Não quero sugestões de receita', 'Não quero outra receita',
                                         'Consulte o cardápio', 'Limpe a despensa', 'Qual é meu orçamento?']):
            with self.subTest(message=message):
                plugin.before_turn(session_id='test', turn_id='specific-'+str(index), user_message=message)
        research.assert_not_called()

    def test_greeting_preserves_active_recipe(self):
        self.present(None)
        research, _ = self.proactive_hooks()
        plugin.before_turn(session_id='test', turn_id='active-greeting', user_message='Oi')
        research.assert_not_called()
        self.assertFalse(plugin._turns[('test', 'active-greeting')]['required'])

    def test_rejected_suggestion_starts_alternative_automatically(self):
        self.present(None)
        self.message('user', 'Não quero esse prato')
        research, _ = self.proactive_hooks()
        plugin.before_turn(session_id='test', turn_id='rejected', user_message='Não quero esse prato')
        research.assert_called_once()

    def test_proactive_research_reuses_existing_task(self):
        research, coordinator = self.proactive_hooks()
        coordinator.pending.return_value = [{'tarefa_id': 'existing', 'agente': 'pesquisa', 'estado': 'RUNNING'}]
        result = plugin.before_turn(session_id='test', turn_id='resume-search', user_message='Oi')
        research.assert_not_called()
        self.assertIn('existing', result['context'])

    def test_stock_failure_blocks_proposal_without_asking_for_dish(self):
        research, _ = self.proactive_hooks({'sucesso': False})
        with self.assertLogs(plugin.logger, level='ERROR'):
            result = plugin.before_turn(session_id='test', turn_id='stock-failed', user_message='Olá')
        self.assertEqual(result['action'], 'block')
        research.assert_not_called()

    def test_proactive_response_presents_recipe_instead_of_asking_for_ideas(self):
        self.proactive_hooks()
        plugin.before_turn(session_id='test', turn_id='proposal', user_message='Olá')
        search = self.api('web_search', query='tomate')
        self.api('receita_ler_fonte', pesquisa_id=search['pesquisa_id'], url=self.recipe['fonte_url'])
        candidate = self.api('receita_apresentar', pesquisa_id=search['pesquisa_id'], receita=self.recipe)
        plugin.after_tool(tool_name=plugin.SEARCH_TOOL, result=search, session_id='test', turn_id='proposal')
        plugin.after_tool(tool_name='mcp__dona_maria_memoria__receita_apresentar', result=candidate,
                          session_id='test', turn_id='proposal')
        text = plugin.finish(response_text='O que você está com vontade de cozinhar?', session_id='test', turn_id='proposal')
        self.assertIn('Tomate cortado', text)
        self.assertIn('100g de tomate', text)
        self.assertNotIn('vontade de cozinhar', text)
        self.assertNotIn('tipo de prato', text)

    def test_stock_tool_result_starts_proposal_without_reasking_user(self):
        research, _ = self.proactive_hooks()
        plugin.before_turn(session_id='test', turn_id='stock-tool', user_message='Tudo bem?')
        research.assert_not_called()
        pantry = self.api('despensa_consultar')
        plugin.after_tool(tool_name='mcp__dona_maria_memoria__despensa_consultar', result=pantry,
                          session_id='test', turn_id='stock-tool')
        research.assert_called_once()
        self.assertTrue(plugin._turns[('test', 'stock-tool')]['required'])
        request = plugin.limit_next_request(request={'messages': []}, session_id='test', turn_id='stock-tool')
        self.assertEqual(request['reason'], 'sugestao_proativa_por_estoque')
        plugin.after_tool(tool_name='mcp__dona_maria_memoria__despensa_consultar', result=pantry,
                          session_id='test', turn_id='stock-tool')
        research.assert_called_once()

    def test_stock_tool_respects_explicit_inventory_only_request(self):
        research, _ = self.proactive_hooks()
        plugin.before_turn(session_id='test', turn_id='stock-only', user_message='Mostre apenas o estoque')
        plugin.after_tool(tool_name='mcp__dona_maria_memoria__despensa_consultar', result=self.api('despensa_consultar'),
                          session_id='test', turn_id='stock-only')
        research.assert_not_called()


if __name__ == '__main__':
    unittest.main()
