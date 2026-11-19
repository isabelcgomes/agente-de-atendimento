"""Teste real de uma sugestão pela interface, sem autorizar compras ou cardápio.

Cria uma conversa de avaliação no histórico e salva seu resultado para inspeção.
Uso: python3 avaliacao-mcp/verificar_interface.py
"""
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen
import uuid


def request(path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = Request('http://127.0.0.1:8080' + path, data=body,
                  headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=20) as response:
        return json.load(response)


def main():
    conversation = str(uuid.uuid4())
    created = request('/api/chat', {
        'conversation': conversation,
        'text': 'Quais receitas posso preparar com os ingredientes da minha despensa?',
    })
    print(json.dumps({'conversation': conversation, 'job': created['id']}), flush=True)
    deadline = time.monotonic() + 620
    previous = None
    while time.monotonic() < deadline:
        job = request('/api/jobs/' + created['id'])
        current = (job['status'], job.get('activity'))
        if current != previous:
            print(json.dumps({'status': current[0], 'atividade': current[1]}, ensure_ascii=False), flush=True)
            previous = current
        if job['status'] != 'running':
            result = {key: job.get(key) for key in ['status', 'text', 'error', 'session', 'progress']}
            result['conversation'] = conversation
            output = Path(__file__).with_name('resultado-interface-real.json')
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps({'status': job['status'], 'resultado': str(output),
                              'resposta': job.get('error') or job.get('text')}, ensure_ascii=False), flush=True)
            return 0 if job['status'] == 'done' and '**Ingredientes e quantidades**' in job.get('text', '') else 1
        time.sleep(2)
    raise TimeoutError('A interface não concluiu o atendimento dentro de 620 segundos.')


if __name__ == '__main__':
    raise SystemExit(main())
