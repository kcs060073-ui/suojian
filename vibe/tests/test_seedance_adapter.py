import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import seedance


class SeedanceAdapterTests(unittest.TestCase):
    def test_reference_is_sent_as_first_frame_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'reference.png'
            image.write_bytes(b'example-image')
            body = seedance.build_request(image)
        reference = body['content'][1]
        self.assertEqual(reference['role'], 'first_frame')
        self.assertEqual(base64.b64decode(reference['image_url']['url'].split(',', 1)[1]), b'example-image')
        self.assertNotIn(base64.b64encode(b'example-image').decode(), str(seedance.safe(body)))

    def test_no_credentials_means_no_submission(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, '未提交任务'):
                seedance.credentials()

    def test_secret_is_read_from_environment_without_copy(self):
        with patch.dict(os.environ, {'ARK_API_KEY': 'test-secret'}, clear=True):
            self.assertEqual(seedance.credentials(), 'test-secret')
            self.assertEqual(seedance.safe('error: test-secret', 'test-secret'), 'error: [redacted]')


if __name__ == '__main__':
    unittest.main()
