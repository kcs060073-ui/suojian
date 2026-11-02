import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from workbench.publishing import publish

class PublishingTests(unittest.TestCase):
    def context(self, root):
        return {'root':root,'output':root/'outputs','vendor':root/'vendor'}

    def test_no_credentials_never_submits(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ',{},clear=True), patch('workbench.publishing.subprocess.run') as run:
            result=publish({'confirmed':True},self.context(Path(folder)))
            self.assertEqual(result['status'],'unavailable')
            run.assert_not_called()

    def test_unconfirmed_content_never_submits(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ',{'UPLOAD_POST_API_KEY':'test','UPLOAD_POST_USER':'test'}), patch('workbench.publishing.subprocess.run') as run:
            with self.assertRaises(ValueError):publish({},self.context(Path(folder)))
            run.assert_not_called()

    def test_unknown_submission_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ',{'UPLOAD_POST_API_KEY':'test','UPLOAD_POST_USER':'test'}):
            root=Path(folder);video=root/'fixture.mp4';video.write_bytes(b'protocol-fixture')
            response=type('Reply',(),{'returncode':0,'stdout':json.dumps({'authOk':True,'missingPlatforms':[]})})
            unknown=type('Reply',(),{'returncode':2,'stdout':json.dumps({'status':'unknown','requestId':'fixture-request','doNotRerun':True})})
            data={'confirmed':True,'video':str(video),'platforms':['youtube'],'caption':'Protocol fixture, never transmitted'}
            with patch('workbench.publishing.subprocess.run',side_effect=[response,unknown]) as run:
                first=publish(data,self.context(root));second=publish(data,self.context(root))
                self.assertEqual(first['status'],'unknown')
                self.assertEqual(second['status'],'needs_review')
                self.assertEqual(run.call_count,2)

if __name__=='__main__':unittest.main()
