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


if __name__=='__main__':unittest.main()
