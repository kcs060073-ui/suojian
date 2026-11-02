import copy
import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from suo_jian.jianying.layered_emphasis import layered_emphasis_plan, resolve_native_binding
from suo_jian.jianying.execution_export import build_execution_draft


class LayeredEmphasisTests(unittest.TestCase):
    def fixture(self, root):
        return {"schema_version":"suojian-execution-plan-1","task_id":"test","render_input_digest":"test",
            "canvas":{"width":1920,"height":1080,"fps":30},"duration":2,"texts":[],"audio":[],"clips":[],
            "overlays":[{"kind":"重点花字素材","source":"baked-1.png","start":0,"duration":1},
                        {"kind":"重点花字素材","source":"baked-2.png","start":1,"duration":1}],
            "emphasis_report":[{"text":"可租可售","template":"business-label-A","start":0,"end":1,"center_y":.89},
                               {"text":"层高11到12米","template":"slanted-number-B","start":1,"end":2,"center_y":.54}],
            "reference_match":{"policy":{"subtitle":{"primary_hex":"#FFE820","outline_hex":"#111111"}}},"limitations":[]}

    def test_component_separation_keeps_editable_digits_and_clean_decorations(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);plan=self.fixture(root);original=copy.deepcopy(plan)
            layered=layered_emphasis_plan(plan,Path.home()/"CUTROOM",root/"assets")
            self.assertEqual(plan,original)
            self.assertEqual([x["text"] for x in layered["texts"]],["可租","可售","层高","11–12","米"])
            digit=layered["texts"][3];body=layered["texts"][2]
            self.assertGreater(digit["style"]["size"],body["style"]["size"])
            self.assertTrue(digit["style"]["italic"])
            self.assertTrue(all(x["kind"].startswith("花字装饰") for x in layered["overlays"]))
            self.assertNotIn("baked-1.png",[x["source"] for x in layered["overlays"]])
            self.assertEqual(layered_emphasis_plan(layered,Path.home()/"CUTROOM",root/"assets"),layered)

    def test_pending_or_other_version_native_template_never_activates(self):
        registry={"bindings":{"business-label-A":{"status":"pending","effect_id":"placeholder"}}}
        self.assertIsNone(resolve_native_binding(registry,"business-label-A","11.5"))
        registry["bindings"]["business-label-A"]={"status":"verified","editor_version":"5.9","kind":"bubble","effect_id":"effect","resource_id":"resource"}
        self.assertIsNone(resolve_native_binding(registry,"business-label-A","11.5"))
        self.assertEqual(resolve_native_binding(registry,"business-label-A","5.9")["effect_id"],"effect")

    def test_real_export_has_native_text_animation_and_background_below_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);plan=self.fixture(root)
            video=root/"video.mp4"
            subprocess.run(["ffmpeg","-v","error","-y","-f","lavfi","-i","color=c=gray:s=1920x1080:r=30:d=2","-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p",str(video)],check=True,capture_output=True,timeout=30)
            plan["clips"]=[{"source":str(video),"start":0,"duration":2,"source_start":0}]
            layered=layered_emphasis_plan(plan,Path.home()/"CUTROOM",root/"assets")
            # This isolated test must not register a project in the user's editor.
            with patch("suo_jian.jianying.execution_export.register_native_draft",return_value={"registered":False,"isolated":True}):
                output,report=build_execution_draft(layered,root/"drafts","test")
                second, _ = build_execution_draft(layered, root/"drafts", "second")
            content=json.loads((output/"draft_content.json").read_text("utf-8"))
            other=json.loads((second/"draft_content.json").read_text("utf-8"))
            self.assertNotEqual(content["id"], other["id"])
            self.assertEqual(json.loads((output/"draft_meta_info.json").read_text("utf-8"))["draft_id"], content["id"])
            texts=[t for t in content["tracks"] if t["type"]=="text"]
            decor=[t for t in content["tracks"] if t["name"].startswith("花字装饰")]
            self.assertEqual(report["editable_text_segments"],5)
            self.assertEqual(report["animated_text_segments"],5)
            self.assertEqual(report["animated_decoration_segments"],2)
            self.assertTrue(all(t["segments"][0]["extra_material_refs"] for t in texts))
            self.assertTrue(all(t["segments"][0]["common_keyframes"] for t in decor))
            self.assertLess(max(t["segments"][0]["render_index"] for t in decor),min(t["segments"][0]["render_index"] for t in texts))
            body=json.loads(content["materials"]["texts"][3]["content"])
            self.assertEqual(body["text"],"11–12")
            self.assertEqual(len(content["materials"]["material_animations"]),5)

if __name__=="__main__":unittest.main()
