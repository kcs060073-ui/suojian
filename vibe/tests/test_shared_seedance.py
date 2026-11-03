import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from workbench import seedance


class SharedVideoProfileTests(unittest.TestCase):
    def test_reads_selected_video_model_without_submitting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / 'runtime/workbench/catalog.sqlite'
            database.parent.mkdir(parents=True)
            with closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE model_profiles (profile_id,model,enabled,account_id,is_default,capability)')
                db.execute('CREATE TABLE model_provider_accounts (account_id,base_url,credential_ref)')
                db.execute('INSERT INTO model_provider_accounts VALUES (?,?,?)', ('a','https://ark.cn-beijing.volces.com/api/v3','test-reference'))
                db.execute('INSERT INTO model_profiles VALUES (?,?,?,?,?,?)', ('p','test-video-model',1,'a',1,'broll'))
                db.commit()
            image = root / 'image.png'; image.write_bytes(b'test-image')
            with patch.dict(os.environ, {'SUOJIAN_CUTROOM_ROOT':str(root)}, clear=True):
                body = seedance.build_request(image)
            self.assertEqual(body['model'], 'test-video-model')
            self.assertEqual(body['content'][1]['role'], 'first_frame')


if __name__ == '__main__':
    unittest.main()
