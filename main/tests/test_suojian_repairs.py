import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from suo_jian.pipeline import AnalysisCheckpoints
from suo_jian.blueprint.templates import TemplateStore
from suo_jian.jianying.execution_export import editable_emphasis_plan

class WorkflowRepairTests(unittest.TestCase):
    def test_checkpoint_resume_and_source_change_invalidation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);video=root/'a.mp4';video.write_bytes(b'original')
            cache=AnalysisCheckpoints(video,root,{})
            compute=Mock(return_value={'value':42})
            self.assertEqual(cache.stage('ocr',compute),{'value':42})
            self.assertEqual(AnalysisCheckpoints(video,root,{}).stage('ocr',compute),{'value':42})
            self.assertEqual(compute.call_count,1)
            with self.assertRaises(RuntimeError):cache.stage('later',Mock(side_effect=RuntimeError('interrupted')))
            self.assertFalse((cache.root/'later.json').exists())
            video.write_bytes(b'changed')
            AnalysisCheckpoints(video,root,{}).stage('ocr',compute)
            self.assertEqual(compute.call_count,2)

    def test_saved_template_lists_choices_and_retains_previous_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            store=TemplateStore(Path(folder));template=store.save({'source':{},'shots':[]},'test','test')
            choices={'subtitle':'white','brand':None}
            saved=store.link_packaging(template['template_id'],'pt-new',choices=choices,reference_policy={'frozen_reference':True})
            self.assertEqual(store.list()[0]['packaging_choices'],choices)
            self.assertEqual(store.get(template['template_id'])['reference_policy'],{'frozen_reference':True})
            self.assertEqual(len(list((Path(folder)/'revisions').glob('*.json'))),1)
            self.assertEqual(saved['schema_version'],'suojian-edit-template-2')

    def test_editable_emphasis_does_not_duplicate_or_mutate_preview(self):
        plan={'texts':[],'overlays':[{'kind':'重点花字素材','source':'a.png','start':2,'duration':3}], 'canvas':{'width':1920},
              'emphasis_report':[{'text':'层高11米','start':2,'end':5,'template':'slanted-number-B','center_y':.54}],
              'reference_match':{'policy':{'subtitle':{'primary_hex':'#FFE820'}}},'limitations':[]}
        before=json.dumps(plan)
        value=editable_emphasis_plan(plan)
        self.assertEqual(json.dumps(plan),before)
        self.assertEqual(value['overlays'],[])
        self.assertEqual(value['texts'][0]['text'],'层高11米')
        self.assertEqual(value['texts'][0]['style']['primary_hex'],'#FFE820')


    def test_new_script_reuses_saved_policy_without_reanalyzing_source(self):
        from suo_jian.integrations.cutroom.unified import UnifiedExecution, ACTIVE
        from suo_jian.integrations.cutroom.packaging_options import PACKAGING_SLOTS
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=TemplateStore(root/'templates')
            template=store.save({'source':{'file_name':'missing.mp4'}},'reference','old')
            choices={key:None for key in PACKAGING_SLOTS};choices['subtitle']='saved-style'
            policy={'frozen_reference':True,'subtitle':{'primary_hex':'#FFE820'}}
            store.link_packaging(template['template_id'],'pt-saved',choices=choices,reference_policy=policy)
            catalog={'libraries':[{'library_id':'new-library','online':True}],
                     'tts_profiles':[{'profileId':'profile','enabled':True,'voices':[{'voiceId':'voice','selectable':True}]}],
                     'packaging_items':{'subtitle':[{'id':'saved-style'}]}}
            runner=UnifiedExecution(root,root/'results',store,invoke=lambda op,args:catalog)
            try:
                with patch('suo_jian.integrations.cutroom.unified.threading.Thread'), patch('suo_jian.integrations.cutroom.unified.build_reference_policy',side_effect=AssertionError('source must not reopen')):
                    state=runner.start({'template_id':template['template_id'],'script':'全新的脚本','library_id':'new-library',
                                        'tts_profile_id':'profile','tts_voice_id':'voice','confirm_cost':True,'owns_materials':True,'delegate_routine':True,'match_reference':True})
                self.assertEqual(state['config']['reference_policy'],policy)
                self.assertEqual(state['config']['packaging_choices'],choices)
                self.assertEqual(state['config']['script'],'全新的脚本')
            finally:ACTIVE.discard(str((root/'results').resolve()))


    def test_checkpoint_missing_output_is_regenerated(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);video=root/'a.mp4';video.write_bytes(b'video')
            output=root/'stem.wav';output.write_bytes(b'audio')
            cache=AnalysisCheckpoints(video,root,{})
            def compute():
                output.write_bytes(b'audio')
                return {'path':str(output)}
            run=Mock(side_effect=compute)
            cache.stage('stems',run)
            output.unlink()
            cache.stage('stems',run)
            self.assertEqual(run.call_count,2)
            self.assertTrue(output.exists())

if __name__=='__main__':unittest.main()
