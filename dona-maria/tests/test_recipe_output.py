"""Contrato de saída: somente receita completa e comprovada, nunca uma lista de links."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import fonte_receitas as sources
import plugin_receitas as plugin


class RecipeOutputTests(unittest.TestCase):
    def setUp(self):
        self.recipe={'nome':'Arroz com tomate','ingredientes':['1 xícara de arroz','2 tomates picados','Sal a gosto'],
                     'modo_preparo':['Lave e corte os tomates.','Cozinhe o arroz e misture os tomates.'],
                     'rendimento':'2 porções','fonte_url':'https://example.org/arroz'}
        self.candidate={'receita_id':'recipe','fonte_conferida':True,'receita':self.recipe}
        self.state={'message':'Me dê uma receita','required':True,'pricing':[], 'candidates':[self.candidate],
                    'pending':False,'searches':[], 'analyses':{},'quotes':[],'delegations':[],'tool_count':0}
        self.key=('output-test','turn')
        plugin._turns[self.key]=self.state
        self.addCleanup(plugin._turns.pop,self.key,None)

    def finish(self):
        return plugin.finish(session_id=self.key[0],turn_id=self.key[1],response_text='Acesse estes links: https://unverified.example')

    def test_exact_recipe_without_search_summary_feedback_or_budget(self):
        text=self.finish()
        self.assertEqual(text,'**Arroz com tomate**\n\n**Ingredientes e quantidades**\n- 1 xícara de arroz\n- 2 tomates picados\n- Sal a gosto\n\n**Modo de preparo**\n1. Lave e corte os tomates.\n2. Cozinhe o arroz e misture os tomates.\n\n**Referência:** https://example.org/arroz')

    def test_collected_recipe_is_rendered_on_automatic_continuation(self):
        self.state['required']=False
        self.assertIn('1 xícara de arroz',self.finish())
        self.assertNotIn('Acesse estes links',self.finish())

    def test_only_one_recipe_even_when_user_asks_for_options(self):
        other = copy.deepcopy(self.candidate)
        other['receita']['nome'] = 'Outra receita'
        self.state['candidates'].append(other)
        for message in ['Me dê três receitas', 'Quero 2 opções de receitas', 'Sugira receitas']:
            with self.subTest(message=message):
                self.state['message'] = message
                self.assertNotIn('Outra receita', self.finish())
                self.assertEqual(self.finish().count('**Referência:**'), 1)

    def test_unverified_candidate_is_not_displayed(self):
        self.candidate['fonte_conferida']=False
        self.state['searches']=[{'resultado_web':{'data':{'web':[{'title':'Link sem receita','url':'https://example.org/other'}]}}}]
        text=self.finish()
        self.assertNotIn('https://',text)
        self.assertNotIn('Arroz com tomate',text)
        with self.assertRaises(ValueError):plugin.render_candidate(self.candidate)

    def test_missing_quantities_block_even_a_receipt_marked_as_checked(self):
        self.recipe['ingredientes']=['Arroz','Tomate']
        self.assertNotIn('**Ingredientes',self.finish())
        checked,issues=sources.check(self.recipe,{'receitas':[copy.deepcopy(self.recipe)]})
        self.assertFalse(checked)
        self.assertTrue(any('sem quantidade' in issue for issue in issues))

    def test_missing_steps_block_recipe(self):
        self.recipe['modo_preparo']=[]
        self.assertNotIn('https://',self.finish())

    def test_source_excerpt_cannot_replace_name_or_preparation(self):
        text=plugin.render_candidate(self.candidate,source={'title':'Título do resultado','description':'Trecho incompleto'})
        self.assertIn('Arroz com tomate',text)
        self.assertNotIn('Título do resultado',text)
        self.assertNotIn('Trecho incompleto',text)

    def test_original_quantities_are_preserved_including_fractions_and_to_taste(self):
        self.recipe['ingredientes']=['½ xícara de arroz','Duas unidades de tomate','Sal a gosto']
        self.assertFalse(sources.presentation_issues(self.recipe))
        self.assertIn('½ xícara',self.finish())

    def test_recipe_request_with_no_menu_permission_is_still_a_recipe_request(self):
        self.assertTrue(plugin.wants_recipe('Me dê uma receita. Não adicione nada ao cardápio.'))

    def test_cost_request_does_not_start_another_recipe_search(self):
        self.assertFalse(plugin.wants_recipe('Retome os custos da receita aceita'))
        self.assertFalse(plugin.wants_recipe('Confira o preço da receita'))
        self.assertTrue(plugin.wants_recipe('Me dê uma receita de baixo custo'))

    def test_mcp_envelope_and_runtime_wrapper_preserve_validation_result(self):
        import json
        result={'sucesso':True,'producao_validada':True}
        envelope={'content':[{'type':'text','text':json.dumps(result)}]}
        self.assertEqual(plugin.unpack(envelope),result)
        self.assertEqual(plugin.unpack('<untrusted_tool_result>\n'+json.dumps(envelope)+'\n</untrusted_tool_result>'),result)

    def test_source_without_quantities_is_rejected_before_presentation(self):
        import memoria_agente as backend
        self.recipe['ingredientes']=['Arroz','Tomate']
        with patch.object(backend,'_temporary_get',return_value={'urls':[self.recipe['fonte_url']]}), patch.object(
                backend.fontes,'read',return_value={'receitas':[self.recipe]}):
            result=backend.ler_fonte_receita(None,'search',self.recipe['fonte_url'])
        self.assertFalse(result['sucesso'])
        self.assertEqual(result['erro'],'fonte_indisponivel')

    def test_historical_recipe_without_body_does_not_crash_memory(self):
        import memoria_agente as backend
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            db=backend.connect(Path(folder)/'memory.sqlite3')
            try:
                db.execute('INSERT INTO fatos VALUES(?,?,?,?,?,?,?,?,?)',
                           ('receitas','old','{"receita":null}','revogado','test','test','test',backend.now(),0))
                db.commit()
                self.assertTrue(backend.consultar(db)['sucesso'])
            finally:
                db.close()

    def test_lazy_discovery_keeps_the_registered_specialist_tool_available(self):
        coordinator=Mock()
        coordinator.child_role.return_value='pesquisa'
        coordinator.child_exhausted.return_value=False
        coordinator.child_needs_work.return_value=True
        session='lazy-specialist-test'
        plugin.register_agent_tools(Mock())
        plugin._specialists.add(session)
        self.addCleanup(plugin._specialists.discard,session)
        with patch.object(plugin,'_coordinator',coordinator):
            request=plugin.limit_next_request(request={'tools':[{'function':{'name':n}} for n in ['tool_search','tool_describe','tool_call']]},session_id=session)['request']
        function=request['tools'][0]['function']
        self.assertEqual(function['name'],'tool_call')
        self.assertEqual(request['tool_choice']['function']['name'],'tool_call')
        entry=function['parameters']['properties']['calls']['items']
        self.assertEqual(entry['properties']['name']['enum'],['dona_maria_pesquisar'])
        arguments=entry['properties']['arguments']['properties']['argumentos']['properties']
        self.assertIn('query',arguments)
        self.assertIn('receita',arguments)
        self.assertIn('ingredientes',arguments['receita']['properties'])
        self.assertEqual(len(request['tools']),1)

    def test_specialist_cannot_finish_before_validated_tool_receipt(self):
        name='specialist-output-test'
        coordinator=Mock()
        coordinator.child_role.return_value='pesquisa'
        coordinator.child_exhausted.return_value=False
        coordinator.child_needs_work.return_value=True
        plugin._specialists.add(name)
        self.addCleanup(plugin._specialists.discard,name)
        tools=[{'function':{'name':'dona_maria_pesquisar'}},{'function':{'name':'unrelated'}}]
        with patch.object(plugin,'_coordinator',coordinator):
            request=plugin.limit_next_request(request={'tools':tools},session_id=name)['request']
        self.assertEqual(request['tool_choice'],{'type':'function','function':{'name':'dona_maria_pesquisar'}})
        self.assertEqual(len(request['tools']),1)


class SpecialistWorkTests(unittest.TestCase):
    def test_cost_runtime_only_allows_batch_comparison_before_calculation(self):
        coordinator=Mock();coordinator.child_role.return_value='custos'
        coordinator.child_exhausted.return_value=False;coordinator.child_needs_work.return_value=True
        plugin.register_agent_tools(Mock())
        plugin._specialists.add('batch-only');self.addCleanup(plugin._specialists.discard,'batch-only')
        with patch.object(plugin,'_coordinator',coordinator):
            coordinator.child_contract.return_value={'finalidade':'receita','comparacao_feita':False}
            result=plugin.limit_next_request(session_id='batch-only',request={'tools':[{'function':{'name':'tool_call'}}]})['request']
            item=result['tools'][0]['function']['parameters']['properties']['calls']['items']
            self.assertEqual(item['properties']['name']['enum'],['dona_maria_custos_despensa_verificar_receita'])
            coordinator.child_contract.return_value={'finalidade':'receita','comparacao_feita':True,'custos_pendentes':True}
            result=plugin.limit_next_request(session_id='batch-only',request={'tools':[{'function':{'name':'tool_call'}}]})['request']
            self.assertNotIn('tools',result)

    def test_price_resume_does_not_send_completed_price_history_to_child(self):
        import tempfile,json
        from agentes_receitas import Coordinator,TaskStore
        def backend(name,args):
            if name=='despensa_consultar':return {'sucesso':True,'estoque':[],'orcamento':{}}
            return {'sucesso':True,'aceite':True,'receita':{'nome':'Prato','modo_preparo':['x'*40000]},
                    'faltantes_precos':[{'ingrediente':'Milho verde'}],
                    'pesquisas_precos':[{'ingrediente':'Outro','historico':'x'*40000}]}
        with tempfile.TemporaryDirectory() as folder:
            service=Mock();service.launch.return_value={'id':'h'}
            c=Coordinator(service,backend,TaskStore(Path(folder)/'tasks.sqlite3'))
            result=c.start('pesquisa','parent','turn','Retome os preços','recipe',purpose='precos')
            self.assertTrue(result['sucesso'])
            context=service.launch.call_args[0][0]['context']
            self.assertLess(len(context),30000)
            self.assertEqual(json.loads(context)['faltantes_para_pesquisar'],['Milho verde'])

    def test_price_contract_requires_each_target_and_deduplicates_queries(self):
        import tempfile,time
        from agentes_receitas import Coordinator,TaskStore
        backend=Mock(side_effect=lambda name,args:{'sucesso':True,'pesquisa_realizada':True,
                    'ingrediente':args['ingrediente'],'ofertas':[]})
        service=Mock()
        service.reconnect.return_value={'connected':True}
        service.result.return_value={'ready':True,'terminal_state':'SUCCEEDED','summary':'{}'}
        with tempfile.TemporaryDirectory() as folder:
            c=Coordinator(service,backend,TaskStore(Path(folder)/'tasks.sqlite3'))
            c.store.put({'id':'price','dono':'parent','chave':'k','agente':'pesquisa','finalidade':'precos',
                'alvos_precos':['Pinoli','Pimenta'], 'receita_id':'recipe','estado':'RUNNING','handle':{'id':'h'},
                'prazo':time.time()+60,'chamadas':0,'buscas':0,'falhas':{},'eventos':[]})
            c.children['child']='price'
            for _ in range(2):
                c.execute('child','price','preco_pesquisar',{'receita_id':'recipe','ingrediente':'Pinoli'})
            self.assertEqual(backend.call_count,1)
            self.assertTrue(c.child_needs_work('child'))
            c.execute('child','price','preco_pesquisar',{'receita_id':'recipe','ingrediente':'Pimenta'})
            self.assertFalse(c.child_needs_work('child'))
            self.assertTrue(c.collect('parent','price')['resultado_validado'])

    def test_resolved_conversion_is_removed_from_pending_questions(self):
        from agentes_receitas import cost_pending_receipts,PREFIX
        events=[{'tool_name':PREFIX+'medidas_consultar','args':{'medida':'colher de sopa'},
                 'result':{'sucesso':False,'erro':'conversao_indefinida','ingrediente':'Manteiga'}},
                {'tool_name':PREFIX+'medidas_consultar','args':{'medida':'colher de sopa'},
                 'result':{'sucesso':True,'ingrediente':'manteiga','quantidade_normalizada':'15'}}]
        self.assertEqual(cost_pending_receipts(events),[])

    def test_cost_tools_do_not_offer_individual_measure_checks(self):
        coordinator=Mock()
        coordinator.child_role.return_value='custos'
        coordinator.child_needs_work.return_value=True
        coordinator.child_exhausted.return_value=False
        plugin.register_agent_tools(Mock())
        plugin._specialists.add('cost-schema')
        self.addCleanup(plugin._specialists.discard,'cost-schema')
        with patch.object(plugin,'_coordinator',coordinator):
            request=plugin.limit_next_request(session_id='cost-schema',request={
                'tools':[{'function':{'name':'tool_call'}}]})['request']
        entries=request['tools'][0]['function']['parameters']['properties']['calls']['items']['oneOf']
        schemas={e['properties']['name']['enum'][0]:e['properties']['arguments'] for e in entries}
        self.assertNotIn('dona_maria_custos_medidas_consultar',schemas)
        self.assertEqual(set(schemas),{'dona_maria_custos_despensa_verificar_receita','dona_maria_custos_receita_precificar'})
        inventory=schemas['dona_maria_custos_despensa_verificar_receita']
        self.assertIn('ingredientes',inventory['required'])
        self.assertNotIn('receita_id',inventory['properties'])

    def test_cost_pending_is_proved_by_business_receipts_and_never_by_invalid_arguments(self):
        import tempfile,time
        from agentes_receitas import Coordinator,TaskStore,PREFIX
        for error, expected in [('conversao_indefinida',True),('argumentos_invalidos',False)]:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as folder:
                service=Mock()
                service.reconnect.return_value={'connected':True}
                service.result.return_value={'ready':True,'terminal_state':'SUCCEEDED','summary':'{}'}
                coordinator=Coordinator(service,Mock(),TaskStore(Path(folder)/'tasks.sqlite3'))
                events=[{'tool_name':PREFIX+'medidas_consultar','args':{'medida':'lata'},
                         'result':{'sucesso':False,'erro':error,'ingrediente':'Molho de tomate'}}]
                coordinator.store.put({'id':'cost','dono':'parent','chave':'key','agente':'custos',
                    'estado':'RUNNING','handle':{'id':'h'},'prazo':time.time()+60,'eventos':events})
                result=coordinator.collect('parent','cost')
                self.assertEqual(result['pendencia_validada'],expected)
                self.assertFalse(result['resultado_validado'])
                if expected:
                    text=plugin.render_cost_pending(events)
                    self.assertIn('Molho de tomate',text)
                    self.assertNotIn('habilidades',text)
                    self.assertNotIn('informe a quantidade',text)
                    self.assertNotIn('preciso definir',text)

    def test_search_reads_next_source_and_presents_exact_structured_recipe(self):
        import tempfile, time
        from agentes_receitas import Coordinator, TaskStore, PREFIX
        recipe = {'nome':'Arroz', 'ingredientes':['1 xícara de arroz','2 xícaras de água'],
                  'modo_preparo':['Cozinhe o arroz na água.'], 'rendimento':'2 porções'}
        calls = []
        def backend(operation, args):
            calls.append((operation, args))
            if operation == 'web_search':
                return {'sucesso':True, 'pesquisa_id':'search', 'resultado_web':{'data':{'web':[
                    {'url':'https://example.org/bad'}, {'url':'https://example.org/good'},
                    {'url':'https://example.org/unused'}]}}}
            if operation == 'receita_ler_fonte':
                self.assertEqual(args['pesquisa_id'],'search')
                return {'sucesso':False} if args['url'].endswith('/bad') else {'sucesso':True,'conteudo':{'receitas':[dict(recipe,nome='Arroz ou massa'),dict(recipe,nome='Peito de frango moído'),recipe]}}
            self.assertEqual(operation,'receita_apresentar')
            self.assertEqual(args['receita'],dict(recipe,fonte_url='https://example.org/good'))
            return {'sucesso':True,'fonte_conferida':True,'receita':args['receita']}
        with tempfile.TemporaryDirectory() as folder:
            coordinator = Coordinator(Mock(), backend, TaskStore(Path(folder)/'tasks.sqlite3'))
            coordinator.store.put({'id':'job','dono':'parent','chave':'key','estado':'RUNNING',
                'agente':'pesquisa','finalidade':'receita','prazo':time.time()+60,
                'chamadas':0,'buscas':0,'falhas':{},'eventos':[]})
            coordinator.children['child']='job'
            result=coordinator.execute('child','job','web_search',{'query':'receita arroz'})
            self.assertTrue(result['receita_validada']['fonte_conferida'])
            self.assertEqual(len(calls),4)
            self.assertFalse(coordinator.child_needs_work('child'))
            self.assertEqual(coordinator.store.get('job')['eventos'][-1]['tool_name'],PREFIX+'receita_apresentar')

    def test_failed_links_trigger_new_search_and_do_not_stop_other_sources(self):
        import tempfile, time
        from agentes_receitas import Coordinator, TaskStore
        recipe = {'nome':'Arroz', 'ingredientes':['1 xícara de arroz'],
                  'modo_preparo':['Cozinhe.'], 'rendimento':'2 porções'}
        calls = []
        searches = []
        def backend(operation, args):
            calls.append((operation, args))
            if operation == 'web_search':
                searches.append(args['query'])
                urls = ['bad', 'bad'] if len(searches) == 1 else ['bad', 'incomplete', 'good', 'unused']
                return {'sucesso':True, 'pesquisa_id':str(len(searches)),
                        'resultado_web':{'data':{'web':[{'url':'https://example.org/'+u} for u in urls]}}}
            if operation == 'receita_ler_fonte':
                if args['url'].endswith('bad'):
                    return {'sucesso':False, 'erro':'fonte_indisponivel', 'detalhe':'DNS failed'}
                return {'sucesso':True, 'conteudo':{'receitas':[] if args['url'].endswith('incomplete') else [recipe]}}
            return {'sucesso':True, 'fonte_conferida':True, 'receita':args['receita']}
        with tempfile.TemporaryDirectory() as folder:
            c = Coordinator(None, backend, TaskStore(Path(folder)/'tasks.sqlite3'))
            c.store.put({'id':'job','dono':'parent','chave':'key','estado':'RUNNING',
                'agente':'pesquisa','finalidade':'receita','prazo':time.time()+60,
                'chamadas':0,'buscas':0,'falhas':{'old failed link':2},'eventos':[]})
            c.children['child']='job'
            self.assertFalse(c.child_exhausted('child'))
            result = c.execute('child','job','web_search',{'query':'receita arroz'})
            self.assertTrue(result['receita_validada']['fonte_conferida'])
            self.assertEqual(len(searches),2)
            self.assertNotEqual(searches[0],searches[1])
            reads = [args['url'] for op,args in calls if op == 'receita_ler_fonte']
            self.assertEqual(reads,['https://example.org/bad','https://example.org/incomplete','https://example.org/good'])
            self.assertEqual(c.store.get('job')['fontes_tentadas']['https://example.org/bad']['detalhe'],'DNS failed')

    def test_unavailable_search_stops_at_budget_without_fabricating_recipe(self):
        import tempfile, time
        from agentes_receitas import Coordinator, TaskStore
        with tempfile.TemporaryDirectory() as folder:
            backend = Mock(return_value={'sucesso':False, 'erro':'busca_web_falhou'})
            c = Coordinator(None, backend, TaskStore(Path(folder)/'tasks.sqlite3'))
            c.store.put({'id':'job','dono':'parent','chave':'key','estado':'RUNNING',
                'agente':'pesquisa','finalidade':'receita','prazo':time.time()+60,
                'chamadas':0,'buscas':0,'falhas':{},'eventos':[]})
            c.children['child']='job'
            result = c.execute('child','job','web_search',{'query':'receita arroz'})
            self.assertTrue(result['pesquisa_esgotada'])
            self.assertNotIn('receita_validada',result)
            self.assertEqual(backend.call_count,12)

    def test_ephemeral_specialist_binds_without_pre_llm_hook(self):
        import tempfile, time
        from agentes_receitas import Coordinator, TaskStore
        with tempfile.TemporaryDirectory() as folder:
            coordinator = Coordinator(Mock(), Mock(), TaskStore(Path(folder)/'tasks.sqlite3'))
            coordinator.store.put({'id':'ephemeral', 'token':'secret', 'dono':'parent', 'chave':'key',
                'estado':'RUNNING', 'agente':'pesquisa', 'prazo':time.time()+60,
                'chamadas':0, 'falhas':{}, 'eventos':[]})
            plugin.register_agent_tools(Mock())
            self.addCleanup(plugin._specialists.discard, 'ephemeral-child')
            request = {'tools':[{'function':{'name':n}} for n in ['tool_search','tool_describe','tool_call']], 'messages':[
                {'role':'user', 'content':'TAREFA_DONA_MARIA=ephemeral TOKEN=secret'}]}
            with patch.object(plugin, '_coordinator', coordinator):
                payload = plugin.limit_next_request(request=request, session_id='ephemeral-child', platform='subagent')['request']
            self.assertEqual(coordinator.child_role('ephemeral-child'), 'pesquisa')
            self.assertEqual(payload['tool_choice']['function']['name'], 'tool_call')
            self.assertEqual(payload['tools'][0]['function']['name'], 'tool_call')
            self.assertIsNone(coordinator.bind_request_child('impostor', [
                {'content':'TAREFA_DONA_MARIA=ephemeral TOKEN=wrong'}]))

    def test_receipt_is_authoritative_when_summary_is_not_json(self):
        import tempfile, time
        from agentes_receitas import Coordinator, TaskStore, PREFIX
        for checked in [True, False]:
            with self.subTest(checked=checked), tempfile.TemporaryDirectory() as folder:
                service = Mock()
                service.reconnect.return_value = {'connected':True}
                service.result.return_value = {'ready':True, 'terminal_state':'SUCCEEDED', 'summary':'Receita pronta.'}
                coordinator = Coordinator(service, Mock(), TaskStore(Path(folder)/'tasks.sqlite3'))
                coordinator.store.put({'id':'job', 'dono':'parent', 'chave':'key', 'handle':{'id':'handle'},
                    'estado':'RUNNING', 'agente':'pesquisa', 'prazo':time.time()+60,
                    'eventos':[{'tool_name':PREFIX+'receita_apresentar',
                                'result':{'sucesso':True, 'fonte_conferida':checked}}]})
                result = coordinator.collect('parent', 'job')
                self.assertEqual(result['resultado_validado'], checked)

    def test_free_summary_cannot_replace_a_complete_recipe_receipt(self):
        import tempfile,time
        from agentes_receitas import Coordinator,TaskStore,PREFIX
        with tempfile.TemporaryDirectory() as folder:
            coordinator=Coordinator(Mock(),Mock(),TaskStore(Path(folder)/'tasks.sqlite3'))
            job={'id':'job','dono':'parent','chave':'key','estado':'RUNNING','agente':'pesquisa','finalidade':'receita',
                 'prazo':time.time()+60,'chamadas':0,'falhas':{},'eventos':[]}
            coordinator.children['child']='job'
            def needed():
                coordinator.store.put(job)
                return coordinator.child_needs_work('child')
            self.assertTrue(needed())
            job['eventos']=[{'tool_name':PREFIX+'web_search','result':{'sucesso':True}}]
            self.assertTrue(needed())
            job['eventos'].append({'tool_name':PREFIX+'receita_apresentar','result':{'sucesso':True,'fonte_conferida':False}})
            self.assertTrue(needed())
            job['eventos'][-1]['result']['fonte_conferida']=True
            self.assertFalse(needed())
            job['eventos']=[];job['chamadas']=96
            self.assertFalse(needed(), 'O limite de ferramentas continua sendo respeitado')


if __name__=='__main__':unittest.main()
