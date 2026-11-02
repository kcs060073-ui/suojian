"""Meaningful director tests with mocked providers; never consumes real credentials."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from workbench.director import Director,ProviderError,apply_operations,context_for_model,digest,provider_call,seal,SCHEMA
from workbench.server import validate_config

def operation(path,value): return {'op':'set','path':path,'value_json':json.dumps(value,ensure_ascii=False)}
def reply(ops=None): return {'summary':'修改测试','warnings':[],'operations':ops or []}

class DirectorTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.state={'name':'所选半身版','format':'portrait','style':'fusion','background':'footage','audioProfile':'selected-reference','showSelectedVideo':True,'narrationMode':'existing','musicGain':1,'voiceGain':1,
            'shots':[{'id':str(i),'from':i*150,'duration':150,'title':'原标题'+str(i),'recipe':'original-'+str(i),'sourceOffset':i*150,'speed':1} for i in range(3)],'subtitles':[{'from':0,'to':70,'text':'原字幕'}],'presenter':{'enabled':True,'placement':'side'}}
        self.d=Director(self.root,lambda:copy.deepcopy(self.state),self.save,validate_config)
    def save(self,c): self.state=copy.deepcopy(c)
    def settings(self,**overrides):
        data={'profiles':[{'id':p,'name':p,'provider':'custom','protocol':'chat','base_url':'https://example.test/v1','model':'test-model','enabled':True,'apiKey':'test-credential-never-sent-'+p} for p in ('first','backup')],
            'routes':{role:['first','backup'] for role in ('edit','script','review')},'maxConcurrency':1,'allowFallback':False,'timeoutSeconds':15,'maxOutputTokens':1024}
        data.update(overrides);self.d.save_settings(data);return data
    def wait_job(self,jid):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            j=next(j for j in self.d.job_list() if j['id']==jid)
            if j['status'] not in ('queued','running'):return j
            time.sleep(.01)
        self.fail('job did not finish')
    def test_apply_undo_preserves_entire_original_and_conflicts(self):
        before=copy.deepcopy(self.state)
        p=self.d.propose('修改第二镜标题',reply([operation('shots.1.title','设备先行')]),self.state)
        self.assertEqual(self.state,before)
        applied=self.d.apply(p['id']);self.assertEqual(applied['config']['shots'][1]['title'],'设备先行')
        self.assertFalse(self.state['showSelectedVideo'])
        self.state['name']='后续修改'
        with self.assertRaisesRegex(ValueError,'又有变化'):self.d.apply(p['id'],True)
        self.state=applied['config'];self.d.apply(p['id'],True);self.assertEqual(self.state,before)
        stale=self.d.propose('换字',reply([operation('name','片名')]),self.state)
        self.state['name']='手动改名'
        with self.assertRaisesRegex(ValueError,'工程已变化'):self.d.apply(stale['id'])
    def test_retime_preserves_source_mapping_and_no_dual_audio(self):
        c,_,warnings=apply_operations(self.state,[operation('shots.1.duration',90),operation('musicGain',.6)])
        self.assertEqual(c['shots'][2]['from'],240);self.assertEqual(c['shots'][2]['sourceOffset'],300)
        self.assertEqual(c['audioProfile'],'workbench');self.assertTrue(warnings)
    def test_bounded_edits_and_code_execution_rejected(self):
        for op in [operation('__class__','bad'),operation('subtitleMode','bilingual'),operation('shots.0.duration',0),operation('shots.0.source','https://evil.test/file.mp4'),operation('shots.0.textColor','url(x)'),operation('musicGain',float('nan')),{'op':'shell','path':'','value_json':'"echo bad"'}]:
            with self.subTest(op=op),self.assertRaises(ValueError):apply_operations(self.state,[op])
        with self.assertRaises(ValueError):apply_operations(self.state,[{'op':'reorder_shots','path':'','value_json':'[0,0,1]'}])
    def test_storyboard_never_reuses_old_speech(self):
        body={'title':'新主题','narration':'新口播','shots':[{'title':'设备','narration':'请先核对设备与入口条件。','durationSeconds':5,'recipe':'measure'}]}
        c,changes,warnings=apply_operations(self.state,[{'op':'storyboard','path':'','value_json':json.dumps(body)}])
        self.assertEqual(c['narrationMode'],'silent');self.assertEqual(c['voiceGain'],0);self.assertEqual(c['audioProfile'],'workbench')
        self.assertFalse(c['subtitlesFollowShots']);self.assertEqual(c['shots'][0]['duration'],150);self.assertTrue(warnings);self.assertEqual(c['scriptDraft']['narration'],'新口播')
    def test_style_roundtrip_restores_original_shots(self):
        before=copy.deepcopy(self.state)
        c,_,_=apply_operations(before,[operation('style','fable')]);self.assertEqual(c['shots'][0]['recipe'],'fable')
        restored,_,_=apply_operations(c,[operation('style','fusion')]);self.assertEqual(restored['shots'],before['shots'])
    def test_review_cannot_apply(self):
        with self.assertRaisesRegex(ValueError,'不允许修改'):self.d.propose('审查',reply([operation('name','修改')]),self.state,role='review')
    def test_no_key_fails_before_queue_and_settings_are_redacted(self):
        with self.assertRaisesRegex(ValueError,'先在API'):self.d.enqueue({'instruction':'变蓝'})
        self.settings();safe=self.d.settings();disk=self.d.secret_path.read_text()
        self.assertNotIn('test-credential',disk);self.assertNotIn('secret',json.dumps(safe));self.assertNotIn('apiKey',json.dumps(safe))
        self.assertTrue(safe['profiles'][0]['keyConfigured'])
        self.d.save_settings(safe);self.assertTrue(self.d.settings()['profiles'][0]['keyConfigured'])
        safe=self.d.settings();safe['profiles'][0]['clearKey']=True;self.d.save_settings(safe)
        self.assertFalse(self.d.settings()['profiles'][0]['keyConfigured'])
    def test_provider_protocols_and_refusal_incomplete(self):
        self.settings();p=self.d.profile('first');record=[]
        def chat(url,key,body,timeout):record.append((url,body));return {'choices':[{'message':{'content':json.dumps(reply())},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':20,'raw':'discard'}}
        _,usage=provider_call(p,[],1024,15,chat);self.assertTrue(record[0][0].endswith('/chat/completions'));self.assertEqual(usage,{'prompt_tokens':10,'completion_tokens':20})
        p['protocol']='responses'
        def responses(url,key,body,timeout):record.append((url,body));return {'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps(reply())}]}]}
        provider_call(p,[],1024,15,responses);self.assertEqual(record[-1][1]['text']['format']['schema'],SCHEMA);self.assertFalse(record[-1][1]['store'])
        with self.assertRaises(ProviderError):provider_call(p,[],1024,15,lambda *a:{'status':'incomplete'})
    def test_model_context_does_not_include_filesystem_or_credentials(self):
        self.state.update(apiKey='never-send',internalPath='C:/secret',assets=[{'name':'厂房','url':'/media/uploads/test.png','path':'C:/private/file.png'}])
        data=json.dumps(context_for_model(self.state));self.assertNotIn('never-send',data);self.assertNotIn('C:/',data)
    def test_fallback_only_when_opted_in_and_transient(self):
        self.settings(allowFallback=True);calls=[]
        def transport(url,key,body,timeout):
            calls.append(key)
            if key.endswith('first'):raise ProviderError('限流测试',True)
            return {'choices':[{'message':{'content':json.dumps(reply([operation('name','备用成功')]))},'finish_reason':'stop'}]}
        self.d.transport=transport
        job=self.wait_job(self.d.enqueue({'instruction':'改名'})['id']);self.assertEqual(job['status'],'completed');self.assertEqual(len(calls),2)
        self.assertEqual(self.state['name'],'所选半身版') # A model response alone never mutates the project.
        self.assertEqual(len(job['attempts']),2)
        self.settings(allowFallback=False);calls.clear()
        job=self.wait_job(self.d.enqueue({'instruction':'改名'})['id']);self.assertEqual(job['status'],'failed');self.assertEqual(len(calls),1)
    def test_queue_concurrency_cancel_and_stale_plan(self):
        self.settings();started=threading.Event();release=threading.Event();calls=[]
        def transport(*args):
            calls.append(1);started.set();release.wait(3)
            return {'choices':[{'message':{'content':json.dumps(reply([operation('name','排队结果')]))},'finish_reason':'stop'}]}
        self.d.transport=transport
        one=self.d.enqueue({'instruction':'任务1'});self.assertTrue(started.wait(1))
        two=self.d.enqueue({'instruction':'任务2'});self.d.cancel(two['id'])
        self.assertEqual(len(calls),1)
        self.state['name']='运行时手动修改';release.set()
        one=self.wait_job(one['id']);self.assertEqual(one['status'],'completed')
        with self.assertRaisesRegex(ValueError,'工程已变化'):self.d.apply(one['planId'])
        self.assertEqual(self.wait_job(two['id'])['status'],'cancelled')
    def test_key_roundtrip_and_url_validation(self):
        s=seal('test-key-not-an-api-secret');self.assertEqual(seal(s,True),'test-key-not-an-api-secret')
        data=self.settings()
        for base in ['https://user:pass@example.test/v1','https://example.test/?api_key=bad','http://example.test/v1']:
            data['profiles'][0]['base_url']=base
            with self.assertRaises(ValueError):self.d.save_settings(data)

if __name__=='__main__':unittest.main()
