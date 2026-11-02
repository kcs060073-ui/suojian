import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
from workbench.production import Store, Conflict, save, read
from workbench.vibe_storyboard import Storyboards
from workbench.storyboard_video import Videos


class VideoWorkflowTests(unittest.TestCase):
    def test_mock_provider_output_is_registered_for_the_pinned_plan(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'run';folder.mkdir()
            image=folder/'ref.png';Image.new('RGB',(8,8),'white').save(image)
            job={'id':'a'*32,'taskId':'V20261029-12345678','revision':1,'inputHash':'input',
                 'shotId':'01','boardSha256':'board','planSha256':'plan','reference':str(image),
                 'prompt':'保持建筑稳定','duration':7,'status':'queued'}
            save(folder/'job.json',job)
            class FakeHTTP:
                def __init__(self,token):pass
                def request(self,method,path,body=None):
                    return ({'id':'provider-1'} if method=='POST' else {'status':'succeeded','content':{'video_url':'https://example.test/video'}}), 'request-1'
                def download(self,url,target):
                    target.write_bytes(b'mock');return {'sha256':'fake-output'}
            module=SimpleNamespace(API='https://example.test',MODEL='test-model',credentials=lambda:'secret',HTTP=FakeHTTP,
                                   build_request=lambda p:{'content':[{'type':'text','text':''},{'image_url':{'url':'image'}}]},
                                   safe=lambda value,secret='':str(value).replace(secret,'[redacted]') if secret else value)
            spec=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda m:None))
            video=Videos(root);state={'serial':1,'shots':[{'id':'01','selectedBoard':1,'boards':[{'version':1,'sha256':'board'}],
                                                         'selectedPlan':1,'plans':[{'version':1,'sha256':'plan'}]}]}
            captured=[]
            with patch('workbench.storyboard_video.importlib.util.spec_from_file_location',return_value=spec),\
                 patch('workbench.storyboard_video.importlib.util.module_from_spec',return_value=module),\
                 patch.object(video.boards,'get',return_value=state),\
                 patch.object(video.boards,'mutate',side_effect=lambda *args:captured.append(args)):
                video._run(folder)
            self.assertEqual(read(folder/'job.json')['status'],'pending_review')
            self.assertEqual(captured[0][2]['externalTaskId'],'provider-1')
            self.assertEqual(read(folder/'request.redacted.json')['body']['content'][1]['image_url']['url'],'[image omitted]')

    def test_confirmed_input_is_pinned_and_duplicate_submission_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=Store(root); boards=Storyboards(root)
            task=store.create({'title':'测试','script':'原文','materials':[],'creativeRoute':'storyboard'})
            key={'expectedRevision':1,'inputHash':task['inputHash']}
            state=boards.create(task['id'],{**key,'artDirection':'手绘','shots':[{'id':'01','start':0,'end':7}]})
            img=root/'outputs'/'input.png';img.parent.mkdir();img.write_bytes(b'\x89PNG\r\n\x1a\nexample')
            def change(op,**args):
                nonlocal state
                state=boards.mutate(task['id'],op,{**key,'expectedSerial':state['serial'],**args})
            change('board',shotId='01',path=str(img));change('confirm_all')
            element={'element_id':'a','action_type':'move','start_time':0,'duration':7,'motion_description':'缓慢前进',
                     'start_state':{},'end_state':{},'execution_method':'seedance','prompt':'建筑保持稳定',
                     'required_assets':[],'overlay_requirements':[],'validation_rules':[]}
            change('plan',shotId='01',elements=[element]);plan=state['shots'][0]['plans'][0]
            change('confirm_plan',shotId='01',planSha256=plan['sha256'])
            event=threading.Event();video=Videos(root)
            job=video.start(task['id'],{**key,'expectedSerial':state['serial'],'shotId':'01'},worker=lambda folder:event.set())
            self.assertTrue(event.wait(2))
            self.assertEqual(job['boardSha256'],state['shots'][0]['boards'][0]['sha256'])
            self.assertEqual(job['planSha256'],plan['sha256'])
            self.assertEqual(video.get(task['id'],job['id'])['status'],'queued')
            with self.assertRaises(Conflict):video.start(task['id'],{**key,'expectedSerial':state['serial'],'shotId':'01'},worker=lambda folder:None)


if __name__=='__main__':unittest.main()
