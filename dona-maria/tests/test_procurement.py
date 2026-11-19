"""Regressões da despensa e do documento de conversões, sem rede ou dados reais alterados."""
import json
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
import despensa_compras as pantry
import memoria_agente as backend
import precos_ingredientes as prices
import precificacao_receitas as costing


class PantryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db=backend.connect(Path(self.folder.name)/'state.sqlite3')
        self.addCleanup(self.db.close)

    def test_screenshot_uses_existing_meat_and_chicken_and_never_buys_cooking_water(self):
        result=pantry.verificar(self.db,[
            {'ingrediente':'carne moída','quantidade':230,'unidade':'g'},
            {'ingrediente':'filés de peito de frango','quantidade':230,'unidade':'g'},
            {'ingrediente':'água quente','quantidade':650,'unidade':'ml'},
            {'ingrediente':'arroz basmati cru','quantidade':185,'unidade':'g'},
            {'ingrediente':'Canela em pó','quantidade':'1/2','unidade':'colher de chá'},
            {'ingrediente':'pimenta-da-jamaica','quantidade':1,'unidade':'colher de chá'}])
        rows={r['ingrediente']:r for r in result['ingredientes']}
        self.assertEqual(rows['Carne moída (patinho)']['faltante'],'0')
        self.assertEqual(rows['Peito de frango']['faltante'],'0')
        self.assertEqual(rows['Água']['faltante'],'0')
        self.assertTrue(rows['Água']['recurso_operacional'])
        self.assertGreater(Decimal(rows['arroz basmati cru']['faltante']),0)
        self.assertEqual(rows['arroz basmati cru']['substituicoes_na_despensa'][0]['ingrediente'],'Arroz branco tipo 1')
        targets={r['ingrediente'] for r in backend.alvos_precos(result)}
        self.assertEqual(targets,{'arroz basmati cru','pimenta-da-jamaica'})
        self.assertTrue(next(r for r in result['pendencias'] if r['ingrediente']=='Canela em pó')['em_estoque'])

    def test_document_fractions_conventions_and_dimensions(self):
        for value,expected in [('1/2','.5'),('1 1/2','1.5'),('½','.5'),('1½','1.5'),('0','0')]:
            self.assertEqual(pantry.number(value),Decimal(expected))
        self.assertEqual(pantry.normalize('Alho','1/2','dente')[0],Decimal('1'))
        self.assertEqual(pantry.normalize('Canela em pó','','a gosto')[0],Decimal('2'))
        self.assertEqual(pantry.normalize('Sal',1,'pitada')[0],Decimal('2'))
        self.assertEqual(pantry.normalize('Cebola','½','unidade')[0],Decimal('50'))
        self.assertEqual(pantry.normalize('Água',2,'xícara de chá')[0],Decimal('.48'))
        for value in ['1/0','-1','NaN']:
            with self.assertRaises(ValueError):pantry.number(value)
        with self.assertRaises(ValueError):pantry.normalize('Óleo de soja',1,'a gosto')
        with self.assertRaises(ValueError):pantry.normalize('Canela em pó',1,'colher de chá')

    def test_every_specific_convention_uses_json_without_manual_measure_checks(self):
        data=pantry.conversion_data()
        for row in data['convencoes_usuario']:
            if row['ingrediente']=='*':continue
            for amount in ['1','2','0.5']:
                for measure in [row['medida_origem'],amount+' '+row['medida_origem']]:
                    with self.subTest(ingredient=row['ingrediente'],quantity=amount,measure=measure):
                        value,unit,_=pantry.normalize(row['ingrediente'],amount,measure)
                        self.assertEqual(value,Decimal(amount)*Decimal(str(row['fator'])))
                        self.assertEqual(unit,row['unidade_destino'])

    def test_every_documented_liquid_measure_is_automatic(self):
        for row in pantry.conversion_data()['conversoes_liquidos']:
            for name in row['ingredientes_permitidos']:
                with self.subTest(ingredient=name,measure=row['medida_origem']):
                    amount,unit,_=pantry.normalize(name,'2','2 '+row['medida_origem'])
                    self.assertEqual(amount,Decimal(2)*Decimal(str(row['fator'])))
                    self.assertEqual(unit,'L')

    def test_metric_quantities_and_embedded_package_contents_are_not_multiplied_twice(self):
        cases=[('Ingrediente novo',500,'500g',Decimal(500),'g'),
               ('Ingrediente novo',1,'500 gramas',Decimal(500),'g'),
               ('Ingrediente novo',1,'0,5 KG',Decimal(500),'g'),
               ('Água',1,'1 litro',Decimal(1),'L'),
               ('Creme de leite',1,'1 caixinha (200g)',Decimal(200),'g'),
               ('Creme de leite',2,'caixinha de 200 gramas',Decimal(400),'g'),
               ('Batatas médias',4,'4 unidades',Decimal(800),'g'),
               ('Cebola média',1,'1 unidade',Decimal(100),'g'),
               ('Alho',2,'2 dentes',Decimal(4),'g')]
        for name,qty,measure,expected,unit in cases:
            with self.subTest(name=name,measure=measure):
                value,destination,_=pantry.normalize(name,qty,measure)
                self.assertEqual((value,destination),(expected,unit))

    def test_source_recipe_weights_override_incomplete_model_transcription(self):
        import fluxo_receitas as flow
        lines=['500g de peito de frango','4 batatas médias','1 caixinha de creme de leite (200g)',
               '1 cebola média','2 dentes de alho','Sal a gosto']
        names=['Peito de frango','Batatas médias','Creme de leite','Cebola média','Alho','Sal']
        model=[{'indice_receita':i,'ingrediente':name,'quantidade':0,'unidade':'medida desconhecida','estado':'cru'}
               for i,name in enumerate(names)]
        mapping=flow.ingredient_mapping({'ingredientes':lines},model)
        self.assertTrue(mapping['sucesso'],mapping)
        result=pantry.verificar(self.db,mapping['ingredientes'])
        self.assertEqual(result['pendencias'],[])
        values={row['ingrediente']:Decimal(row['necessario']) for row in result['ingredientes']}
        self.assertEqual(values,{'Peito de frango':Decimal(500),'Batata':Decimal(800),
                                'Creme de leite':Decimal(200),'Cebola':Decimal(100),'Alho':Decimal(4),'Sal':Decimal(2)})

    def test_generic_cheese_is_stock_but_basmati_is_not_an_automatic_white_rice_substitution(self):
        import fluxo_receitas as flow
        self.assertEqual(pantry.canonical('queijo ralado'),'Queijo parmesão ralado')
        self.assertEqual(pantry.canonical('cebola picada'),'Cebola')
        mapping=flow.ingredient_mapping({'ingredientes':['185g de arroz basmati cru']},[
            {'indice_receita':0,'ingrediente':'Arroz branco tipo 1','quantidade':185,'unidade':'g'}])
        self.assertFalse(mapping['sucesso'])

    def test_imported_ingredient_costs_use_current_sheet_and_normalized_units(self):
        def rows(file,sheet):
            return [['Pimentão verde','500','g']] if sheet==1 else [['pimentão verde','1','kg','10.00']]
        with patch.object(pantry,'rows_xlsx',side_effect=rows):
            value=costing.cost_basis(self.db)['pimentao verde']
        self.assertEqual(value['quantidade'],Decimal('500'))
        self.assertEqual(value['custo'],Decimal('5'))
        self.assertEqual(value['unidade'],'g')


class PriceEvidenceTests(unittest.TestCase):
    def test_package_price_is_not_confused_with_unit_price_or_drained_weight(self):
        value=prices.snippet_offer({'title':'Milho Verde 200 g','description':'R$ 4,99. Peso drenado 170 g.',
                                  'url':'https://example.org/milho'},'milho verde')
        self.assertEqual(value['quantidade'],'200')
        self.assertEqual(value['preco_embalagem_BRL'],'4.99')
        value=prices.snippet_offer({'title':'Pimentão Verde 500 g','description':'R$ 8,99 R$ 17,98/kg',
                                  'url':'https://example.org/pimentao'},'pimentão verde')
        self.assertEqual(value['tipo_venda'],'embalagem')
        self.assertEqual(value['preco_embalagem_BRL'],'8.99')
    def test_supermarket_price_per_kg_and_plural_product_names(self):
        value=prices.snippet_offer({'title':'Pimentão Verde Fresco por Kg','description':'R$ 11, 99 / kg',
                                  'url':'https://example.org/pimentao'},'pimentão verde')
        covered=prices.with_coverage(value,{'faltante':'250','unidade':'g'})
        self.assertEqual(covered['preco_embalagem_BRL'],'11.99')
        self.assertEqual(covered['quantidade_compra'],'0.25')
        self.assertEqual(covered['custo_itens_BRL'],'3.00')
        value=prices.snippet_offer({'title':'Azeitona Verde 500g','description':'R$ 13.90',
                                  'url':'https://example.org/azeitona'},'azeitonas')
        self.assertEqual(value['preco_embalagem_BRL'],'13.90')

    def test_per_kg_price_is_distinguished_from_partial_package_price(self):
        value=prices.snippet_offer({'title':'Pimentão Verde','description':'R$ 8,99 R$ 17,98/ kg R$ 8,99 R$ 17,98/ kg',
                                  'url':'https://example.org/pimentao'},'pimentão verde')
        self.assertEqual(value['preco_embalagem_BRL'],'17.98')
        self.assertEqual(value['tipo_venda'],'por_peso')
    def test_product_page_preserves_currency_package_and_reference(self):
        html='<script type="application/ld+json">'+json.dumps({'@type':'Product','name':'Pinoli 100 g',
             'offers':{'@type':'Offer','price':'39.90','priceCurrency':'BRL'}})+'</script>'
        values=prices.structured_offers(html,'Pinoli','https://example.org/pinoli')
        self.assertEqual(values[0]['preco_embalagem_BRL'],'39.90')
        value=prices.with_coverage(values[0],{'faltante':'170','unidade':'g'})
        self.assertEqual(value['embalagens'],2)
        self.assertEqual(value['custo_itens_BRL'],'79.80')
        self.assertIsNone(value['frete_BRL'])

    def test_search_snippet_is_labelled_and_ambiguous_prices_are_rejected(self):
        row={'title':'Pimenta Jamaica 50 g','description':'R$ 12,90','url':'https://example.org/pimenta'}
        value=prices.snippet_offer(row,'pimenta-da-jamaica')
        self.assertEqual(value['origem_evidencia'],'preco_anunciado_na_busca')
        self.assertIsNone(prices.snippet_offer(dict(row,description='De R$ 15,00 por R$ 12,90'),'pimenta-da-jamaica'))
        self.assertIsNone(prices.snippet_offer(dict(row,title='Pimenta Jamaica sem embalagem'),'pimenta-da-jamaica'))
        self.assertIsNone(prices.snippet_offer(dict(row,title='Canela em pó 50 g'),'pimenta-da-jamaica'))
