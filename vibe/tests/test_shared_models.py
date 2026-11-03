from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from workbench.shared_models import planning_profile


class SharedModelTests(unittest.TestCase):
    def test_reads_bound_profile_with_local_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); vibe = root / 'vibe'; cutroom = root / 'cutroom'
            binding = vibe / 'work/director/shared-profile.json'; binding.parent.mkdir(parents=True)
            binding.write_text(json.dumps({'profileId':'p'}),encoding='utf-8')
            database = cutroom / 'runtime/workbench/catalog.sqlite'; database.parent.mkdir(parents=True)
            with closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE model_profiles (profile_id,display_name,model,enabled,capability,settings_json,account_id)')
                db.execute('CREATE TABLE model_provider_accounts (account_id,provider,base_url,credential_ref)')
                db.execute('INSERT INTO model_provider_accounts VALUES (?,?,?,?)', ('a','openai_compatible','https://example.test/v1','ref'))
                db.execute('INSERT INTO model_profiles VALUES (?,?,?,?,?,?,?)', ('p','test','model',1,'planning','{"protocol":"openai"}','a'))
                db.commit()
            credentials = cutroom / 'media_catalog/credentials.py'; credentials.parent.mkdir()
            credentials.write_text('def read_provider_secret(ref):\n    return "fake-key"\n',encoding='utf-8')
            with patch.dict(os.environ, {'SUOJIAN_CUTROOM_ROOT':str(cutroom)}):
                profile = planning_profile(vibe)
            self.assertEqual(profile['model'],'model')
            self.assertEqual(profile['key'],'fake-key')
            self.assertEqual(json.loads(binding.read_text(encoding='utf-8')), {'profileId':'p'})


if __name__ == '__main__':
    unittest.main()
