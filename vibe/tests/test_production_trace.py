"""Provenance regression: deliberately distinguish installed/output/used/accepted."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from workbench.production_trace import (STAGES, base_trace, capability, component, digest, file_record,
    safe_record, shot, trace_delivery, trace_feed, trace_vibe_run, validate, write_trace, version_video, observe_renderer_return)


class ProductionTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def put(self, rel, value):
        p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(value) if isinstance(value,(dict,list)) else value,encoding='utf-8');return p

    def test_source_or_output_does_not_promote_execution_reference_or_visual_quality(self):
        src=self.put('component.py','source');out=self.put('clip.mp4','exists-only')
        c=component('card','renderer','adapted',source=src,output=out)
        self.assertEqual(c['capabilities']['source_exists']['status'],'verified')
        for stage in STAGES[1:]:self.assertEqual(c['capabilities'][stage]['status'],'unverified')

    def test_trace_is_append_only_and_cannot_auto_grant_visual_approval(self):
        t=base_trace('task',1,{},None,None);p=write_trace(self.root,t)
        with self.assertRaises(FileExistsError):write_trace(self.root,t)
        self.assertEqual(json.loads(Path(p['path']).read_text(encoding='utf-8'))['delivery']['visualAcceptance'],'unverified')
        t['delivery']['visualAcceptance']='passed'
        with self.assertRaises(ValueError):validate(t)

    def test_verified_capability_requires_evidence(self):
        t=base_trace('task',1,{},None,None);c=component('card','engine','adapted')
        c['capabilities']['execution_succeeded']=capability('verified')
        t['shots']=[shot('S1',0,1,[c],t)]
        with self.assertRaises(ValueError):validate(t)

    def test_trace_failure_is_explicit_and_nonfatal_to_existing_output(self):
        video=self.put('final.mp4','keep')
        def fail(*args):raise ValueError('bad receipt')
        result=safe_record(self.root,fail)
        self.assertEqual(result['status'],'failed');self.assertEqual(video.read_text(),'keep')

    def test_private_inputs_are_not_read(self):
        private=self.put('work/secrets/personal.json',{'secret':'never'})
        self.assertIsNone(file_record(private)['sha256'])

    def test_migrated_video_url_is_bounded(self):
        video=self.put('outputs/moved.mp4','v')
        self.assertEqual(version_video(self.root,{'video':'missing','url':'/outputs/moved.mp4'}),video)
        with self.assertRaises(ValueError):version_video(self.root,{'url':'/outputs/../../outside.mp4'})

    def assembly_fixture(self):
        scene=self.put('scene.mp4','scene');video=self.put('outputs/final.mp4','final')
        assembly={'schema':'suojian.park-vibe-assembly/1','task':'V-test','revision':2,
            'sha256':file_record(video)['sha256'],'command':['ffmpeg','-i',str(scene)],
            'storyVideoScenes':[{'name':'arrival','start':0,'end':2,'video':str(scene),'sha256':file_record(scene)['sha256']}],
            'templateNativeMotionRanges':[[2,3]],'templateBase':str(self.root/'template.mp4'),'templateVersion':'2.7.11'}
        a=self.put('project/assembly.json',assembly)
        project=self.put('project/project.json',{'assembly':str(a)})
        task={'id':'V-test','revision':2,'input':{'script':'real script','reference':'ref'}}
        version={'id':'v2','revision':2,'sha256':file_record(video)['sha256'],'video':str(video),
                 'sourceProject':str(project),'editingMode':'native-project'}
        return task,version,scene,video

    def test_bound_assembly_proves_scene_reference_not_generator_execution(self):
        task,version,_,_=self.assembly_fixture();t=trace_delivery(self.root,task,version)
        c=t['shots'][0]['components'][0]
        self.assertEqual(c['capabilities']['final_referenced']['status'],'verified')
        self.assertEqual(c['capabilities']['execution_succeeded']['status'],'unverified')
        self.assertEqual(t['delivery']['editability']['level'],'unverified')
        self.assertEqual(t['shots'][1]['components'][0]['capabilities']['final_referenced']['status'],'unverified')

    def test_changed_video_scene_or_revision_cannot_reuse_old_binding(self):
        for change in ('video','scene','revision'):
            with self.subTest(change=change):
                task,v,scene,video=self.assembly_fixture()
                if change=='video':video.write_text('changed')
                elif change=='scene':scene.write_text('changed')
                else:v['revision']=3
                t=trace_delivery(self.root,task,v)
                self.assertEqual(t['shots'][0]['components'][0]['capabilities']['final_referenced']['status'],'unverified')

    def test_delivery_without_final_plan_does_not_invent_shots(self):
        video=self.put('outputs/v.mp4','v')
        t=trace_delivery(self.root,{'id':'T','input':{}},{'revision':1,'video':str(video),'editingMode':'native-project'})
        self.assertEqual(t['shots'],[]);self.assertEqual(t['delivery']['render'],'unverified')

    def test_actual_worker_success_does_not_verify_configured_card(self):
        self.put('outputs/runs/J/preview.mp4','v');self.put('outputs/runs/J/config.json',{})
        self.put('engine/bridge.cjs','renderer')
        config={'shots':[{'id':'S1','duration':30,'recipe':'spotlight-hero-card'}]}
        observation=observe_renderer_return(self.root,self.root/'outputs/runs/J','render',config,self.root/'outputs/runs/J/preview.mp4')
        t=trace_vibe_run(self.root,'J','render',config,{'status':'completed'},observation=observation)
        renderer,card=t['shots'][0]['components']
        self.assertEqual(renderer['capabilities']['execution_succeeded']['status'],'verified')
        self.assertEqual(card['capabilities']['execution_succeeded']['status'],'unverified')
        self.assertEqual(card['capabilities']['final_referenced']['status'],'unverified')
        self.assertEqual(t['delivery']['visualAcceptance'],'unverified')

    def test_export_failure_does_not_erase_successful_picture_render(self):
        video=self.put('outputs/runs/J/plate.mp4','v');config={'shots':[{'id':'S','duration':30}]}
        observation=observe_renderer_return(self.root,video.parent,'export_draft',{**config,'plate':True},video)
        t=trace_vibe_run(self.root,'J','export_draft',config,error='draft export failed',observation=observation)
        self.assertEqual(t['delivery']['render'],'succeeded')
        self.assertEqual(t['delivery']['draftGeneration'],'failed')
        self.assertEqual(t['shots'][0]['components'][0]['capabilities']['final_referenced']['status'],'unverified')

    def test_tampered_observation_or_changed_config_cannot_verify_renderer(self):
        video=self.put('outputs/runs/J/preview.mp4','v');config={'shots':[{'id':'S','duration':30}]}
        observation=observe_renderer_return(self.root,video.parent,'render',config,video)
        changed={**config,'name':'changed input'}
        self.assertEqual(trace_vibe_run(self.root,'J','render',changed,{'status':'completed'},observation=observation)['delivery']['render'],'unverified')
        Path(observation['path']).write_text('{}')
        self.assertEqual(trace_vibe_run(self.root,'J','render',config,{'status':'completed'},observation=observation)['delivery']['render'],'unverified')

    def test_delivery_registration_adds_trace_without_granting_native_editability(self):
        from workbench.production import Store
        store=Store(self.root)
        task=store.create({'title':'fixture','script':'actual','materials':['C:/footage'],'format':'landscape'})
        video=self.put('outputs/final.mp4','fixture')
        meta={'format':{'duration':'2'},'streams':[{'codec_type':'video','width':1920,'height':1080}]}
        with patch('workbench.production.subprocess.run') as probe:
            probe.return_value.stdout=json.dumps(meta)
            result=store.deliver(task['id'],{'executor':'dialogue','expectedRevision':1,'video':str(video)})
        pointer=result['versions'][0]['productionTrace']
        t=json.loads(Path(pointer['path']).read_text(encoding='utf-8'))
        self.assertEqual(t['delivery']['visualAcceptance'],'unverified')
        self.assertEqual(t['delivery']['editability']['level'],'unverified')

    def test_worker_failure_and_partial_scope_are_preserved(self):
        config={'shots':[{'id':'A','duration':30},{'id':'B','duration':30},{'id':'C','duration':30,'enabled':False}]}
        t=trace_vibe_run(self.root,'J','pilot',config,error='missing dependency',scope={'from':30,'to':44})
        self.assertEqual([s['finalStoryboardId'] for s in t['shots']],['B'])
        self.assertEqual(t['shots'][0]['interval']['end'],45)
        self.assertEqual(t['delivery']['render'],'failed')

    def test_feed_hash_binding_and_unverified_legacy_revision(self):
        source=self.put('clip.mp4','clip');video=self.put('outputs/final.mp4','video')
        plan={'task_id':'cutroom-task','preview_path':str(video),'preview_signature':file_record(video),
            'source_signatures':{str(source):file_record(source)},'clips':[{'id':'S1','source':str(source),'start':0,'duration':2}]}
        self.put('results/J/execution_plan.json',plan)
        t=trace_feed(self.root,'J',{'config':{}},plan)
        self.assertIsNone(t['revision'])
        self.assertEqual(t['shots'][0]['components'][0]['capabilities']['final_referenced']['status'],'verified')
        source.write_text('changed')
        self.assertEqual(trace_feed(self.root,'J',{},plan)['shots'][0]['components'][0]['capabilities']['final_referenced']['status'],'unverified')

    def test_worker_and_delivery_hooks_write_sidecars_without_real_render(self):
        from workbench import server
        config={'shots':[{'id':'S1','duration':30,'recipe':'chart'}]}
        def render(*args):return self.put('outputs/runs/J/preview.mp4','fixture-not-real-video')
        jobs={'J':{'id':'J'}}
        with patch.object(server,'ROOT',self.root),patch.object(server,'OUTPUTS',self.root/'outputs'),patch.object(server,'JOBS',jobs),\
             patch.object(server,'run_engine',side_effect=render),patch('workbench.films.register_film',return_value='film-fixture'):
            server.tool_worker('J','render',{},config)
        self.assertIn('productionTrace',jobs['J'])
        self.assertTrue(Path(jobs['J']['productionTrace']['path']).exists())

    def test_state_enum_includes_failure_not_run_and_downgrade(self):
        for status in ('failed','not_executed','degraded','unverified'):
            self.assertEqual(capability(status)['status'],status)

    def test_feed_draft_presence_is_not_an_observed_success_or_editor_acceptance(self):
        self.put('draft/draft_content.json',{'tracks':[]})
        state={'draft_path':str(self.root/'draft')};plan={'clips':[]}
        old=trace_feed(self.root,'J',state,plan)
        self.assertEqual(old['delivery']['draftGeneration'],'unverified')
        new=trace_feed(self.root,'J',state,plan,draft_observation={'observer':'unified_export/build_execution_draft returned','report':{}})
        self.assertEqual(new['delivery']['draftGeneration'],'succeeded')
        self.assertFalse(new['delivery']['editability']['verifiedInEditor'])

    def test_returned_failure_is_not_treated_as_unknown_without_exception(self):
        t=trace_vibe_run(self.root,'J','export_draft',{'shots':[]},{'status':'failed','reason':'exporter rejected input'})
        self.assertEqual(t['invocations'][0]['status'],'failed')
        self.assertEqual(t['invocations'][0]['failureReason'],'exporter rejected input')
        self.assertEqual(t['delivery']['draftGeneration'],'failed')


if __name__=='__main__':unittest.main()
