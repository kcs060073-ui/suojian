import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC=importlib.util.spec_from_file_location('trace_maintenance',Path(__file__).resolve().parents[1]/'tools/trace_maintenance.py')
m=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(m)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.src=root/'source.py';self.dst=root/'deployed.py';self.backup=root/'before.py'
        self.src.write_text('after');self.dst.write_text('before');self.backup.write_text('before')
        self.previous=(m.ROOT,m.OUT,m.MAPPING);m.ROOT=root;m.OUT=root/'records';m.MAPPING=[(self.src,self.dst)]
        m.write(m.OUT/'baseline.json',{'code':[{'path':str(self.dst),'sha256':m.sha(self.dst),'backup':str(self.backup)}]})
        self.addCleanup(self.restore)

    def restore(self):m.ROOT,m.OUT,m.MAPPING=self.previous

    def test_scoped_deploy_and_rollback_restore_exact_original(self):
        m.deploy();self.assertEqual(self.dst.read_text(),'after')
        m.deploy(rollback=True);self.assertEqual(self.dst.read_text(),'before')

    def test_later_changes_block_deploy_and_rollback_without_clobbering(self):
        self.dst.write_text('someone else')
        with self.assertRaises(ValueError):m.deploy()
        self.assertEqual(self.dst.read_text(),'someone else')
        self.dst.write_text('before');m.deploy();self.dst.write_text('later edit')
        with self.assertRaises(ValueError):m.deploy(rollback=True)
        self.assertEqual(self.dst.read_text(),'later edit')
