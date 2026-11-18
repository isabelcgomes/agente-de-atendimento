#!/usr/bin/env python3
"""Inicia o MCP no runtime atual do Hermes, incluindo extras como ddgs."""
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
repo = Path.home() / '.hermes' / 'hermes-agent'
launcher = repo / '.hermes' / 'bin' / 'hermes'
try:
    command = json.loads(subprocess.run(
        [str(launcher), '--print-runtime-command'], check=True,
        capture_output=True, text=True, timeout=60).stdout)
    python = command[0]
except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
    print('Não foi possível resolver o runtime atual do Hermes: ' + str(exc), file=sys.stderr)
    raise SystemExit(1)

# Mesmo bootstrap do launcher oficial, executando o servidor do projeto. Não
# fixa o caminho de um venv antigo: extras instalados depois continuam visíveis.
bootstrap = (
    'import os,sys,runpy; '
    "os.environ.pop('PYTHONHOME',None); os.environ.pop('PYTHONPATH',None); "
    "os.environ.pop('VIRTUAL_ENV',None); "
    'sys.path.insert(0,' + repr(str(repo)) + '); '
    'sys._hermes_pin_default_home=True; import hermes_bootstrap; '
    "os.environ.setdefault('HERMES_HOME',str(__import__('hermes_constants').get_default_hermes_root())); "
    'runpy.run_path(' + repr(str(root / 'memoria_agente.py')) + ",run_name='__main__')"
)
os.execv(python, [python, '-I', '-c', bootstrap, *sys.argv[1:]])
