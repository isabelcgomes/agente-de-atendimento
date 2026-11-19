#!/usr/bin/env python3
"""Verifica busca, leitura e apresentação reais em uma cópia temporária da base.

Execute com o Python do runtime Hermes. Não usa mocks nem altera a cozinha.
"""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

SOURCE = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SOURCE))
import memoria_agente as memory
from agentes_receitas import Coordinator, TaskStore
from plugin_receitas import complete_candidate


def main():
    with tempfile.TemporaryDirectory(prefix='dona-maria-rede-') as directory:
        root = Path(directory)
        with sqlite3.connect(memory.DEFAULT_DB.resolve().as_uri() + '?mode=ro', uri=True) as original:
            db = sqlite3.connect(root / 'memory.sqlite3')
            original.backup(db)
        db.close()
        db = memory.connect(root / 'memory.sqlite3')
        try:
            def backend(operation, args):
                value = memory.dispatch(db, operation, args)
                print(json.dumps({'operacao':operation, 'url':args.get('url'),
                                  'query':args.get('query'), 'sucesso':value.get('sucesso'),
                                  'erro':value.get('erro'), 'detalhe':value.get('detalhe')}, ensure_ascii=False), flush=True)
                return value
            coordinator = Coordinator(None, backend, TaskStore(root / 'tasks.sqlite3'))
            coordinator.store.put({'id':'rede', 'dono':'verificacao-rede', 'chave':'rede',
                'estado':'RUNNING', 'agente':'pesquisa', 'finalidade':'receita',
                'prazo':time.time()+480, 'chamadas':0, 'buscas':0, 'falhas':{}, 'eventos':[]})
            coordinator.children['verificacao']='rede'
            query = ' '.join(sys.argv[1:]) or 'receita caseira com peito de frango e arroz ingredientes modo de preparo'
            result = coordinator.execute('verificacao', 'rede', 'web_search', {'query':query, 'limit':5})
            recipe = result.get('receita_validada', {})
            if not complete_candidate(recipe):
                print(json.dumps(result, ensure_ascii=False))
                return 1
            print(json.dumps({'validada':True, 'receita':recipe['receita'],
                              'fontes_tentadas':coordinator.store.get('rede').get('fontes_tentadas', {})},
                             ensure_ascii=False, indent=2))
            return 0
        finally:
            db.close()


if __name__ == '__main__':
    raise SystemExit(main())
