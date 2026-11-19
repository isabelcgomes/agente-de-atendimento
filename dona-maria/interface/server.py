#!/usr/bin/env python3
"""Interface local do Hermes. Python 3.9+, sem dependências adicionais."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOURCE = Path(__file__).resolve().parents[1] / 'src'
sys.dont_write_bytecode = True
sys.path.insert(0, str(SOURCE))
import memoria_agente as memory
import despensa_compras as pantry
STATIC = Path(__file__).parent / 'static'
LIMIT = 8 * 1024 * 1024
MUTATION = threading.Lock()
JOBS = {}
STATE_LOCK = threading.RLock()
RUNTIME = SOURCE / 'dados' / 'interface'
HERMES_HOME = SOURCE.parents[1] / '.hermes'


def stamp():
    return datetime.now(timezone.utc).isoformat()


def storage():
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(str(RUNTIME / 'chat.sqlite3'), timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, session TEXT);
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, conversation TEXT, role TEXT, text TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY, name TEXT, created TEXT, items INTEGER, backup TEXT);''')
    return db


def history(conversation):
    with closing(storage()) as db:
        return [dict(r) for r in db.execute('SELECT role,text,created FROM messages WHERE conversation=? ORDER BY id', (conversation,))]


def append(conversation, role, text):
    with closing(storage()) as db, db:
        db.execute('INSERT INTO messages(conversation,role,text,created) VALUES(?,?,?,?)', (conversation, role, text, stamp()))


def parse_workbook(content):
    """Valida as duas abas originais, preservando posições de células vazias."""
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if len(archive.infolist()) > 2000 or sum(x.file_size for x in archive.infolist()) > 32 * 1024 * 1024:
            raise ValueError('Planilha muito grande após descompactação.')
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(x.itertext()) for x in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('m:si', ns)]
        def rows(sheet, columns):
            result = []
            for row in ET.fromstring(archive.read('xl/worksheets/sheet%d.xml' % sheet)).findall('.//m:sheetData/m:row', ns)[1:]:
                values = [''] * columns
                for cell in row.findall('m:c', ns):
                    if cell.find('m:f', ns) is not None:
                        raise ValueError('Use valores nas células de dados, sem fórmulas.')
                    letters = re.match('[A-Z]+', cell.get('r', 'A1'))[0]
                    index = 0
                    for char in letters:
                        index = index * 26 + ord(char) - 64
                    if index > columns:
                        continue
                    value = cell.find('m:v', ns)
                    text = value.text if value is not None else ''.join(cell.find('m:is', ns).itertext()) if cell.find('m:is', ns) is not None else ''
                    values[index-1] = strings[int(text)] if cell.get('t') == 's' else text
                if any(values):
                    result.append(values)
            return result
        stock, prices = rows(1, 3), rows(2, 4)
    if not stock or len(stock) > 5000:
        raise ValueError('A primeira aba deve conter de 1 a 5.000 ingredientes.')
    costs = {}
    for name, quantity, unit, price in prices:
        name = pantry.canonical(name)
        ident = pantry.key(name)
        if ident in costs:
            raise ValueError('Preço duplicado: ' + name)
        qty, normalized, _ = pantry.normalize(name, quantity, unit)
        paid = pantry.number(price).quantize(Decimal('.01'))
        costs[ident] = (qty, normalized, paid)
    result, seen = [], set()
    for name, quantity, unit in stock:
        name = pantry.canonical(name)
        ident = pantry.key(name)
        if ident in seen:
            raise ValueError('Ingrediente duplicado: ' + name)
        seen.add(ident)
        # normalize exige quantidade positiva; zero é estoque válido.
        factor, normalized, _ = pantry.normalize(name, '1', unit)
        qty = pantry.number(quantity) * factor
        if ident not in costs or costs[ident][1] != normalized:
            raise ValueError('Falta preço ou as unidades divergem nas abas: ' + name)
        bought, _, paid = costs[ident]
        cost = int((qty / bought * paid * 100).quantize(Decimal('1')))
        result.append((ident, name, normalized, str(qty), cost))
    return result


def import_workbook(content, name):
    if not name.lower().endswith('.xlsx'):
        raise ValueError('Selecione uma planilha .xlsx.')
    rows = parse_workbook(content)
    if not MUTATION.acquire(blocking=False):
        raise ValueError('Aguarde o Hermes terminar a resposta antes de trocar a despensa.')
    try:
        identifier, created = uuid.uuid4().hex, stamp()
        backups = RUNTIME / 'backups'
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backups / (identifier + '.sqlite3')
        target = SOURCE / 'despensa_dona_maria.xlsx'
        previous = target.read_bytes()
        (backups / (identifier + '.xlsx')).write_bytes(previous)
        with closing(memory.connect(memory.DEFAULT_DB)) as db:
            with closing(sqlite3.connect(str(backup))) as copy:
                db.backup(copy)
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('DELETE FROM despensa_estoque')
                db.executemany('INSERT INTO despensa_estoque VALUES(?,?,?,?,?)', rows)
                db.execute('UPDATE despensa_controle SET importado_em=? WHERE id=1', (created,))
                db.execute("UPDATE despensa_cotacoes SET estado='substituida' WHERE estado='pendente'")
                db.execute('UPDATE metadata SET revisao=revisao+1 WHERE id=1')
                with tempfile.NamedTemporaryFile(dir=str(SOURCE), suffix='.xlsx', delete=False) as f:
                    f.write(content)
                    temporary = Path(f.name)
                os.replace(str(temporary), str(target))
                db.commit()
            except Exception:
                db.rollback()
                target.write_bytes(previous)
                raise
        with closing(storage()) as db, db:
            db.execute('INSERT INTO imports VALUES(?,?,?,?,?)', (identifier, name, created, len(rows), str(backup)))
        return {'sucesso': True, 'itens': len(rows), 'arquivo': name}
    finally:
        MUTATION.release()


def snapshot():
    with closing(sqlite3.connect(memory.DEFAULT_DB.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        state = pantry.consultar(db)
        state['cardapio'] = memory.consultar_cardapio(db)['pratos']
        state['importado_em'] = db.execute('SELECT importado_em FROM despensa_controle WHERE id=1').fetchone()[0]
        state['cardapio_atualizado_em'] = db.execute('SELECT MAX(atualizado_em) FROM cardapio').fetchone()[0]
    with closing(storage()) as db:
        last = db.execute('SELECT name,created FROM imports ORDER BY created DESC LIMIT 1').fetchone()
        state['arquivo'] = last['name'] if last else 'despensa_dona_maria.xlsx'
        state['estoque_substituido'] = bool(last)
    state['consultado_em'] = stamp()
    state['hermes_disponivel'] = bool(shutil.which('hermes'))
    return state


def chat_worker(job_id, conversation, text):
    job = JOBS[job_id]
    process = None
    try:
        append(conversation, 'user', text)
        with closing(storage()) as db:
            row = db.execute('SELECT session FROM conversations WHERE id=?', (conversation,)).fetchone()
        command = ['hermes', 'chat', '-q', text, '--format', 'stream-json']
        if row and row['session']:
            command += ['--resume', row['session']]
        env = dict(os.environ, HERMES_HOME=str(HERMES_HOME), NO_COLOR='1')
        with tempfile.TemporaryFile(mode='w+') as errors:
            process = subprocess.Popen(command, cwd=str(SOURCE), env=env, stdout=subprocess.PIPE,
                                       stderr=errors, text=True, bufsize=1, start_new_session=True)
            def stop():
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            watchdog = threading.Timer(600, stop)
            watchdog.start()
            terminal = None
            try:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    with STATE_LOCK:
                        if event.get('session_id'):
                            with closing(storage()) as db, db:
                                db.execute('INSERT OR REPLACE INTO conversations VALUES(?,?)', (conversation, event['session_id']))
                        if event.get('type') == 'text':
                            job['text'] += event.get('text', '')
                        elif event.get('type') == 'tool_use':
                            job['activity'] = 'Consultando ' + event.get('name', 'ferramenta').split('__')[-1]
                        elif event.get('type') == 'result':
                            terminal = event
                code = process.wait()
            finally:
                watchdog.cancel()
            if code or not terminal or terminal.get('exit_code'):
                raise RuntimeError('O Hermes não concluiu a resposta. Confira a configuração e os logs locais do agente. A conversa foi preservada; consulte o estado antes de repetir uma ação.')
            answer = terminal.get('text') or job['text']
            if not answer:
                raise RuntimeError('O Hermes encerrou sem uma resposta de texto.')
            append(conversation, 'assistant', answer)
            with STATE_LOCK:
                job.update(status='done', text=answer, activity='Concluído')
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else 'Não foi possível iniciar o Hermes. Verifique a instalação local.'
        append(conversation, 'error', message)
        with STATE_LOCK:
            job.update(status='error', error=message)
    finally:
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        MUTATION.release()


class Handler(BaseHTTPRequestHandler):
    def send(self, value, status=200, content_type='application/json; charset=utf-8'):
        content = json.dumps(value, ensure_ascii=False).encode() if content_type.startswith('application/json') else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.headers.get('Host', '') not in self.server.allowed_hosts:
            return self.send({'erro': 'Origem não permitida.'}, 403)
        from urllib.parse import urlsplit, parse_qs
        path = urlsplit(self.path).path
        try:
            if path == '/api/state':
                return self.send(snapshot())
            if path == '/api/history':
                conversation = parse_qs(urlsplit(self.path).query).get('conversation', [''])[0]
                with STATE_LOCK:
                    active = next((dict(j, id=k) for k,j in JOBS.items() if j['conversation']==conversation and j['status']=='running'), None)
                return self.send({'messages': history(conversation), 'active': active})
            if path.startswith('/api/jobs/'):
                with STATE_LOCK:
                    job = JOBS.get(path.rsplit('/', 1)[-1])
                    return self.send(dict(job), 200) if job else self.send({'erro':'Resposta não encontrada.'},404)
            names = {'/': 'index.html', '/app.js':'app.js', '/style.css':'style.css'}
            if path not in names:
                return self.send({'erro':'Página não encontrada.'},404)
            file = STATIC / names[path]
            kind = {'.html':'text/html', '.js':'text/javascript', '.css':'text/css'}[file.suffix]
            self.send(file.read_bytes(), content_type=kind+'; charset=utf-8')
        except Exception:
            self.send({'erro': 'Não foi possível consultar os dados locais.'}, 500)

    def do_POST(self):
        # Bloqueia requisições de outros sites ao serviço local, inclusive DNS rebinding.
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        if host not in self.server.allowed_hosts or (origin and origin != 'http://' + host):
            return self.send({'erro':'Origem não permitida.'},403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > LIMIT:
                return self.send({'erro':'Envie um arquivo ou mensagem de até 8 MB.'},413)
            data = self.rfile.read(length)
            if self.path == '/api/pantry':
                from urllib.parse import unquote
                return self.send(import_workbook(data, Path(unquote(self.headers.get('X-Filename', ''))).name))
            if self.path == '/api/chat':
                payload = json.loads(data)
                text, conversation = payload.get('text', ''), payload.get('conversation', '')
                if not isinstance(text,str) or not text.strip() or len(text)>12000 or not isinstance(conversation,str) or not re.fullmatch('[a-f0-9-]{36}',conversation):
                    raise ValueError('Mensagem ou conversa inválida.')
                if not MUTATION.acquire(blocking=False):
                    return self.send({'erro':'O Hermes está trabalhando. Aguarde a conclusão.'},409)
                with STATE_LOCK:
                    # Mantém apenas trabalhos recentes; histórico completo está no SQLite.
                    for key in list(JOBS):
                        if JOBS[key]['status'] != 'running' and len(JOBS)>100:
                            del JOBS[key]
                    identifier = uuid.uuid4().hex
                    JOBS[identifier] = dict(status='running', text='', activity='Iniciando Hermes…', conversation=conversation)
                threading.Thread(target=chat_worker, args=(identifier,conversation,text.strip()),daemon=True).start()
                return self.send({'id':identifier},202)
            self.send({'erro':'Rota não encontrada.'},404)
        except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError, IndexError) as exc:
            self.send({'erro':str(exc) or 'Planilha inválida. Confira o modelo.'},400)
        except Exception:
            self.send({'erro':'Não foi possível salvar os dados.'},500)

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    server.allowed_hosts = {'127.0.0.1:'+str(args.port),'localhost:'+str(args.port)}
    print('Hermes: http://localhost:%d — Ctrl+C para encerrar' % args.port,flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
