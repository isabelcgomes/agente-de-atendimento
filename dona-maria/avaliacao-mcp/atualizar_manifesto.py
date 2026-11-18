"""Redescubra o MCP pelo cliente Hermes e atualize seu cache, sem carregar plugins.

Uso: python3 /home/opc/dona-maria/avaliacao-mcp/atualizar_manifesto.py
Não reinicia o gateway nem substitui as ferramentas de uma conversa já aberta.
"""
import json
from pathlib import Path
import subprocess
import sys


def main():
    repo = Path.home() / '.hermes' / 'hermes-agent'
    if '--runtime' not in sys.argv:
        launcher = repo / '.hermes' / 'bin' / 'hermes'
        command = json.loads(subprocess.check_output(
            [str(launcher), '--print-runtime-command'], text=True, timeout=60))
        bootstrap = (
            'import os,sys,runpy; '
            "os.environ.pop('PYTHONHOME',None); os.environ.pop('PYTHONPATH',None); "
            "os.environ.pop('VIRTUAL_ENV',None); "
            'sys.path.insert(0,' + repr(str(repo)) + '); '
            'sys._hermes_pin_default_home=True; import hermes_bootstrap; '
            'runpy.run_path(' + repr(str(Path(__file__).resolve())) + ",run_name='__main__')"
        )
        return subprocess.run([command[0], '-I', '-c', bootstrap, '--runtime'], timeout=90).returncode

    from hermes_constants import get_hermes_home
    import hermes_yaml as yaml
    # Importe a fachada antes dos módulos auxiliares (que voltam à fachada).
    from tools import mcp_tool
    from tools.mcp_tool_registration import _register_from_cache_sync
    from tools.mcp_schema_cache import config_fingerprint, get_cached_entry, write_cache_entry
    from unittest.mock import patch
    import time

    home = get_hermes_home()
    config = yaml.safe_load((home / 'config.yaml').read_text())['mcp_servers']['dona_maria_memoria']
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2024-11-05', 'capabilities': {},
            'clientInfo': {'name': 'avaliacao-dona-maria', 'version': '1'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
    ]
    process = subprocess.run([config['command'], *config.get('args', [])],
        input=''.join(json.dumps(request)+'\n' for request in requests),
        text=True, capture_output=True, check=True, timeout=30)
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    manifest = next(response['result'] for response in responses if response['id'] == 2)
    required = ['receita_ler_fonte', 'receita_conferir_execucao', 'receita_aprovar',
                'cardapio_adicionar', 'cardapio_consultar']
    assert set(required).issubset({tool['name'] for tool in manifest['tools']})
    assert manifest['ttlMs'] == 300_000
    fingerprint = config_fingerprint(config)
    write_cache_entry('dona_maria_memoria', fingerprint, tools=manifest['tools'], ttl_ms=manifest['ttlMs'])
    cached = get_cached_entry('dona_maria_memoria', fingerprint)
    names = _register_from_cache_sync('dona_maria_memoria', config, cached)
    assert all('mcp__dona_maria_memoria__' + name in names for name in required), names
    assert len(names) == len(manifest['tools'])
    with patch('tools.mcp_schema_cache.time.time', return_value=time.time() + 301):
        assert get_cached_entry('dona_maria_memoria', fingerprint) is None
    print(json.dumps({'ferramentas_registradas': names, 'cache_atualizado': True,
                      'ttl_ms': cached['ttl_ms'], 'expiracao_verificada': True,
                      'plugin_carregado': False}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
