import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from media_catalog import credentials
from media_catalog.database import CatalogDatabase
from media_catalog.packaging_plan import create_blank_plan, get_plan
from workbench.api import ApiError, WorkbenchAPI


class MemoryCredentials:
    def __init__(self):
        self.values = {}

    def write(self, ref, secret):
        self.values[ref] = secret

    def read(self, ref):
        return self.values.get(ref)

    def delete(self, ref):
        self.values.pop(ref, None)


class ProviderCreationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.db = CatalogDatabase.open(Path(':memory:'))
        self.db.migrate()
        self.addCleanup(self.db.close)
        self.api = WorkbenchAPI(self.db, Path.cwd())
        self.backend = MemoryCredentials()
        self.patch = patch('media_catalog.credentials._backend', return_value=self.backend)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.body = {'accountId': 'existing', 'name': 'fixture',
                     'preset': 'openai_compatible', 'baseUrl': 'https://example.invalid/v1',
                     'secret': 'original-secret'}

    def test_duplicate_creation_keeps_original_secret_and_account(self):
        self.api._create_provider_account(self.body)
        before = dict(self.backend.values)
        with self.assertRaises((ApiError, sqlite3.IntegrityError)):
            self.api._create_provider_account({**self.body, 'secret': 'replacement'})
        self.assertEqual(self.backend.values, before)
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM model_provider_accounts').fetchone()[0], 1)

    def test_invalid_duplicate_cannot_erase_original_secret(self):
        self.api._create_provider_account(self.body)
        before = dict(self.backend.values)
        with self.assertRaises(ApiError):
            self.api._create_provider_account({**self.body, 'channelId': True})
        self.assertEqual(self.backend.values, before)

    def test_failed_new_insert_cleans_only_its_new_secret(self):
        self.api._create_provider_account(self.body)
        before = dict(self.backend.values)
        self.db.connection.execute("CREATE TRIGGER reject_new BEFORE INSERT ON model_provider_accounts BEGIN SELECT RAISE(ABORT, 'forced'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.api._create_provider_account({**self.body, 'accountId': 'new-account'})
        self.assertEqual(self.backend.values, before)

    def test_unknown_packaging_reference_is_rejected_before_draft_mutation(self):
        self.db.connection.execute("INSERT INTO tasks(task_id,title,task_type,status) VALUES('t','task','数字人混剪','draft')")
        self.db.connection.commit()
        plan_id = create_blank_plan(self.db, 't', '数字人混剪', '自选组合')
        with self.assertRaises((ApiError, RuntimeError)):
            self.api._compose_packaging({'task_id':'t'},
                {'planId':plan_id,'slot':'subtitle','assetRef':'undefined'})
        self.assertEqual(get_plan(self.db, plan_id)['items'], [])
