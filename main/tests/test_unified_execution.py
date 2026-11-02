import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from suo_jian.blueprint.templates import TemplateStore, digest, write_json
from suo_jian.integrations.cutroom.execution_plan import normalize_render_plan, load_bound_plan, file_signature
from suo_jian.integrations.cutroom.unified import UnifiedExecution, plan_storyboard, read_state, match_subtitle_preset
from suo_jian.ui.unified_api import register_unified_routes, analysis_audio_path
from suo_jian.jianying.execution_export import register_native_draft
from suo_jian.integrations.cutroom.packaging_options import PACKAGING_SLOTS, validate_packaging_choices


class UnifiedTests(unittest.TestCase):
    def test_reference_copy_splits_complete_short_phrases_before_tts(self):
        from suo_jian.script_parser.semantic_splitter import semantic_split, visible_chars
        text="上周周边制造业企业选择老厂房续租，还是换层高11米、能上10吨行车的新载体？"
        parts=semantic_split(text,target_chars=10,max_chars=15)
        self.assertEqual("".join(parts),text)
        self.assertTrue(all(visible_chars(part)<=15 for part in parts))
        self.assertIn("上周周边制造业企业",parts[0])
        self.assertTrue(any("11米" in part for part in parts))
        self.assertTrue(any("10吨" in part for part in parts))

    def test_reference_visual_plan_keeps_independent_single_line_caption_events(self):
        raw = {"ratio":"16:9","voice":"voice.wav","transitionMode":"cut","referenceMatch":{"sourceShotCount":6},
               "segments":[{"src":"a.mp4","start":i,"end":i+1,"dur":1,"text":"镜头文案"} for i in range(6)],
               "subtitlePreset":{"max_lines":1,"primary_hex":"#FFE820"},
               "subtitleEvents":[{"start":0,"end":3,"dur":3,"text":"一行字幕","displayText":"一行字幕","displaySize":90},
                                 {"start":3,"end":6,"dur":3,"text":"另一行","displayText":"另一行"}]}
        with tempfile.TemporaryDirectory() as folder:
            plan=normalize_render_plan(raw,root=Path(folder),task_id="task-test",preview_path="preview.mp4")
            self.assertEqual(len(plan["clips"]),6)
            self.assertEqual(len(plan["texts"]),2)
            self.assertEqual(plan["texts"][0]["style"]["size"],90)
            self.assertEqual(plan["texts"][0]["style"]["primary_hex"],"#FFE820")
            self.assertEqual(plan["transition"]["kind"],"切镜")
            self.assertEqual(plan["reference_match"]["sourceShotCount"],6)

    def test_export_keeps_continuous_caption_across_two_independent_shots(self):
        raw = {"ratio": "16:9", "voice": "voice.wav", "segments": [
            {"src": "a.mp4", "text": "还是换层高11米、", "displayText": "还是换层高11米\n能上10吨行车的新载体",
             "start": 0, "dur": 2, "subtitleStart": 0, "subtitleEnd": 4},
            {"src": "b.mp4", "text": "能上10吨行车的新载体？", "displayText": "", "start": 2, "dur": 2},
        ]}
        with tempfile.TemporaryDirectory() as folder:
            plan = normalize_render_plan(raw, root=Path(folder), task_id="task_test", preview_path="preview.mp4")
            self.assertEqual(len(plan["clips"]), 2)
            self.assertEqual(len(plan["texts"]), 1)
            self.assertEqual(plan["texts"][0]["duration"], 4)
            self.assertEqual([clip["duration"] for clip in plan["clips"]], [2, 2])
            raw["segments"][0]["subtitleEnd"] = 0
            with self.assertRaises(ValueError):
                normalize_render_plan(raw, root=Path(folder), task_id="task_test", preview_path="preview.mp4")

    def test_refresh_recovers_completed_repackage_without_starting_another_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            saved = {"mode": "repackage", "status": "running", "task_id": "existing-task", "更新包装预览_job": "existing-render"}
            write_json(root / "execution.json", saved)
            runner = UnifiedExecution(root, root, TemplateStore(root / "templates"), invoke=lambda op, args: {"status": "completed"})
            with patch.object(runner, "call", return_value={"status": "completed"}) as call, patch.object(runner, "collect") as collect:
                runner.refresh_completed_repackage()
                call.assert_called_once_with("job", job_id="existing-render")
                collect.assert_called_once()
            write_json(root / "execution.json", {**saved, "更新包装预览_job": "newer-render"})
            with patch.object(runner, "collect") as collect:
                runner.finish_repackage()
                collect.assert_not_called()

    def test_repackage_preserves_previous_preview_and_never_calls_tts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = TemplateStore(root / "templates")
            result = root / "results"
            previous = {"status": "ready", "task_id": "task_old", "preview_path": "original.mp4",
                        "candidate_id": "candidate-old", "config": {"script": "现有配音"}, "draft_path": "old-draft"}
            write_json(result / "execution.json", previous)
            write_json(result / "execution_plan.json", {"old": True})
            current = {slot: None for slot in PACKAGING_SLOTS}
            current.update(subtitle="old-sub", brand="phone")
            choices = {**current, "subtitle": "white", "brand": None}
            catalog = {slot: [] for slot in PACKAGING_SLOTS}
            catalog["subtitle"] = [{"id": "old-sub"}, {"id": "white"}]
            catalog["brand"] = [{"id": "phone"}]
            calls = []
            def invoke(op, args):
                calls.append((op, args))
                if op == "packaging_options": return {"catalog": catalog, "current": {"choices": current, "plan_id": "current-plan"}}
                if op == "repackage": return {"plan_id": "new-plan", "choices": choices}
                if op == "start":
                    self.assertEqual(args["action"], "render_and_qa")
                    self.assertNotIn("confirm_cost", args)
                    return {"jobId": "rerender"}
                if op == "job": return {"status": "completed"}
                self.fail("包装更新不应创建任务或生成配音：" + op)
            runner = UnifiedExecution(root, result, store, invoke=invoke)
            with self.assertRaises(ValueError):
                runner.repackage({"choices": {**choices, "subtitle": "invented"}, "expected_plan_id": "current-plan"})
            with self.assertRaisesRegex(ValueError, "已被更新"):
                runner.repackage({"choices": choices, "expected_plan_id": "stale-plan"})
            self.assertEqual(read_state(result), previous)
            with patch("suo_jian.integrations.cutroom.unified.threading.Thread"), patch("suo_jian.integrations.cutroom.unified.ACTIVE", set()):
                runner.repackage({"choices": choices, "expected_plan_id": "current-plan"})
            history = result / "preview_history" / runner.state["packaging_revision"]
            self.assertEqual(json.loads((history / "execution.json").read_text(encoding="utf-8")), previous)
            self.assertIsNone(runner.state["draft_path"])
            with patch.object(runner, "collect") as collect:
                runner.run_repackage()
                runner.run_repackage()
                self.assertEqual(collect.call_count, 2)
            self.assertEqual(len([op for op, _ in calls if op == "repackage"]), 1)
            self.assertEqual(len([op for op, _ in calls if op == "start"]), 1)
            self.assertEqual(runner.state["config"]["script"], "现有配音")

    def test_packaging_optional_slots_can_be_disabled_but_subtitle_is_required(self):
        catalog = {slot: [] for slot in PACKAGING_SLOTS}
        catalog["subtitle"] = [{"id": "white"}]
        choices = {slot: None for slot in PACKAGING_SLOTS}
        with self.assertRaises(ValueError):
            validate_packaging_choices(choices, catalog)
        choices["subtitle"] = "white"
        self.assertEqual(validate_packaging_choices(choices, catalog), choices)
        with self.assertRaises(ValueError):
            validate_packaging_choices({**choices, "untrusted": "file"}, catalog)

    def test_voice_selection_is_validated_and_changes_generation_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = TemplateStore(root / "templates")
            template = store.save({"source": {}, "shots": []}, "voice test", "abcd1234")
            catalog = {"libraries": [{"library_id": "park", "online": True}],
                       "tts_profiles": [{"profileId": "tts", "enabled": True,
                         "voices": [{"voiceId": "a", "selectable": True}, {"voiceId": "b", "selectable": True},
                                    {"voiceId": "removed", "selectable": False}]},
                         {"profileId": "other", "enabled": True, "voices": [{"voiceId": "other-only", "selectable": True}]}]}
            calls = []
            def invoke(op, args):
                calls.append(op)
                if op == "catalog": return catalog
                self.fail("Voice validation must not invoke paid generation")
            runner = UnifiedExecution(root, root / "results", store, invoke=invoke)
            config = {"template_id": template["template_id"], "script": "园区招商。", "library_id": "park",
                      "tts_profile_id": "tts", "owns_materials": True, "delegate_routine": True, "confirm_cost": True}
            for voice in (None, "invented", "removed", "other-only"):
                with self.assertRaisesRegex(ValueError, "可用音色"):
                    runner.start({**config, "tts_voice_id": voice})
            self.assertFalse((root / "results/execution.json").exists())
            with patch("suo_jian.integrations.cutroom.unified.threading.Thread"), patch("suo_jian.integrations.cutroom.unified.ACTIVE", set()):
                first = runner.start({**config, "tts_voice_id": "a"})
            first_digest = first["config_digest"]
            first["status"] = "ready"
            write_json(root / "results/execution.json", first)
            with patch("suo_jian.integrations.cutroom.unified.threading.Thread"), patch("suo_jian.integrations.cutroom.unified.ACTIVE", set()):
                second = runner.start({**config, "tts_voice_id": "b"})
            self.assertEqual(second["config"]["tts_voice_id"], "b")
            self.assertNotEqual(first_digest, second["config_digest"])
            self.assertEqual(set(calls), {"catalog"})

    def test_workbench_history_uses_real_state_and_skips_corrupt_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root / "results/abcd1234/edit_blueprint.json", {"source": {"file_name": "参考.mp4"}})
            write_json(root / "results/abcd1234/analysis_status.json", {"status": "completed"})
            write_json(root / "results/abcd1234/execution.json", {"status": "qa_blocked", "phase": "预览待审看"})
            write_json(root / "results/efab1234/analysis_status.json", {"status": "running", "stage": "识别字幕"})
            write_json(root / "results/not-a-job/edit_blueprint.json", {"source": {"file_name": "不能出现"}})
            broken = root / "results/aaaa1234/edit_blueprint.json"
            broken.parent.mkdir(parents=True); broken.write_text("{bad", encoding="utf-8")
            app = Flask(__name__)
            register_unified_routes(app, root, lambda job: None)
            with patch("suo_jian.ui.unified_api.call_local_adapter") as remote:
                tasks = app.test_client().get("/api/workbench/tasks").get_json()["tasks"]
                remote.assert_not_called()
            self.assertEqual({t["job"] for t in tasks}, {"abcd1234", "efab1234"})
            task = next(t for t in tasks if t["job"] == "abcd1234")
            self.assertEqual(task["title"], "参考.mp4")
            self.assertEqual(task["status_label"], "有质检提示")
            self.assertTrue(task["needs_attention"])

    def test_analysis_audio_stays_within_its_own_result_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "audio_stems/voice.wav"
            audio.parent.mkdir(); audio.write_bytes(b"audio")
            blueprint = {"audio_stems": {"tracks": [{"kind": "voice", "path": str(audio)}]}}
            self.assertEqual(analysis_audio_path(blueprint, root, "voice"), audio)
            blueprint["audio_stems"]["tracks"][0]["path"] = str(root / "elsewhere.wav")
            with self.assertRaises(ValueError):
                analysis_audio_path(blueprint, root, "voice")
            with self.assertRaises(FileNotFoundError):
                analysis_audio_path(blueprint, root, "missing")

    def test_native_registration_preserves_existing_projects_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "新草稿"
            write_json(root / "root_meta_info.json", {"all_draft_store": [{"draft_id": "user-owned", "custom": 42}], "draft_ids": 2})
            write_json(output / "draft_content.json", {"id": "new-id", "duration": 35000000})
            write_json(output / "draft_meta_info.json", {"draft_id": "template-id", "draft_name": ""})
            with patch("suo_jian.jianying.execution_export.subprocess.run") as processes:
                processes.return_value.returncode = 0
                processes.return_value.stdout = ""
                result = register_native_draft(output)
                register_native_draft(output)
            index = json.loads((root / "root_meta_info.json").read_text(encoding="utf-8"))
            meta = json.loads((output / "draft_meta_info.json").read_text(encoding="utf-8"))
            self.assertTrue(result["registered"])
            self.assertEqual(index["all_draft_store"][1], {"draft_id": "user-owned", "custom": 42})
            self.assertEqual(len(index["all_draft_store"]), 2)
            self.assertEqual(index["draft_ids"], 3)
            self.assertEqual(meta["draft_id"], "new-id")
            self.assertEqual(meta["draft_name"], "新草稿")
            self.assertEqual(meta["tm_duration"], 35000000)
            self.assertEqual(len(list(root.glob("root_meta_info.suojian-backup-*.json"))), 1)

    def test_native_registration_refuses_unknown_index_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root / "root_meta_info.json", {"all_draft_store": "unknown-format"})
            original = (root / "root_meta_info.json").read_bytes()
            with self.assertRaises(ValueError):
                register_native_draft(root / "new")
            self.assertEqual((root / "root_meta_info.json").read_bytes(), original)

    def test_reference_style_matches_real_catalog_not_fake_template_id(self):
        match = match_subtitle_preset({"styles": {"subtitle_style_01": {"fill": "#FFD800"}}}, [
            {"id": "white", "primary_hex": "#FFFFFF"}, {"id": "orange", "primary_hex": "#FFB300"}])
        self.assertEqual(match["id"], "orange")

    def test_full_orchestration_orders_steps_and_keeps_human_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = TemplateStore(root / "templates")
            template = store.save({"source": {"width": 1920, "height": 1080}, "shots": []}, "test", "abcd1234")
            calls, gates = [], {"brief_locked": False, "picture_lock": False, "packaging_confirmed": False}
            cards, candidates = [], []
            def invoke(op, args):
                calls.append((op, args))
                if op == "preflight": return {"blocking": []}
                if op == "create": return {"taskId": "task_test"}
                if op == "inspect": return {"workflow": {"gates": dict(gates), "stages": [
                    {"stage_key": "material_sync", "status": "completed" if gates["brief_locked"] else "not_started"}]},
                    "cards": list(cards), "candidates": list(candidates)}
                if op == "lock_brief": gates["brief_locked"] = True; return {}
                if op == "sync_preview": return {"impact_token": "impact"}
                if op == "context": return {"inputDigest": "ctx", "script": {"text": "园区招商。"}, "materials": [
                    {"mediaKind": "video", "assetId": "a", "relativePath": "横屏/园区.mp4"}]}
                if op == "start":
                    if args["action"] == "recommend_storyboard": cards.append({"card_id": "test"})
                    return {"jobId": args["action"]}
                if op == "job": return {"status": "completed"}
                if op == "lock_picture": gates["picture_lock"] = True; return {}
                if op == "packaging": gates["packaging_confirmed"] = True; return {}
                if op == "catalog": return {"subtitle_presets": []}
                return {}
            runner = UnifiedExecution(root, root / "results", store, invoke=invoke)
            runner.state = {"config": {"template_id": template["template_id"], "script": "园区招商。",
                "library_id": "park", "tts_profile_id": "tts", "tts_voice_id": "chosen-voice", "confirm_cost": True, "owns_materials": True},
                "config_digest": "stable", "human_reviewed": False}
            with patch.object(runner, "collect", return_value={}):
                runner.run()
            operations = [op for op, args in calls]
            self.assertLess(operations.index("preflight"), operations.index("create"))
            for operation, arguments in calls:
                if operation in {"preflight", "create"}:
                    self.assertEqual(arguments["request"]["tts_voice_id"], "chosen-voice")
            self.assertLess(operations.index("submit"), operations.index("lock_picture"))
            self.assertLess(operations.index("lock_picture"), operations.index("packaging"))
            paid = [args for op, args in calls if op == "start" and args["action"] == "recommend_storyboard"]
            self.assertEqual(len(paid), 1)
            self.assertTrue(paid[0]["confirm_cost"])
            self.assertFalse(runner.state["human_reviewed"])
            self.assertTrue((root / "results/generation_blueprint.json").exists())

    def test_template_immutable_deduplicated_and_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            store = TemplateStore(Path(temp))
            reference = {"source": {"width": 1920}, "styles": {"a": {"fill": "#FFF"}}, "shots": []}
            template = store.save(reference, "园区", "abcd1234")
            self.assertEqual(store.save(reference, "园区", "abcd1234"), template)
            self.assertEqual(len(store.list()), 1)
            store.link_packaging(template["template_id"], "pt-example")
            self.assertEqual(store.get(template["template_id"])["recipe"]["styles"], reference["styles"])
            self.assertEqual(store.save(reference, "园区", "abcd1234")["cutroom_packaging_template_id"], "pt-example")
            with self.assertRaises(ValueError):
                store.get("../../escape")

    def test_storyboard_full_script_coverage_real_scoped_paths(self):
        text = "层高11到12米，一楼承重5吨。\n点击下方链接，预约考察园区。"
        materials = [{"assetId": "a", "relativePath": "内部/横屏/一楼.mp4", "mediaKind": "video"},
                     {"assetId": "b", "relativePath": "门口/横屏/进园区.mp4", "mediaKind": "video"},
                     {"assetId": "c", "relativePath": "竖屏/其它.mp4", "mediaKind": "video"}]
        plan = plan_storyboard(text, materials, "16:9", {})["plan"]
        self.assertEqual("".join(s["text"] for s in plan["segments"]), text.replace("\n", ""))
        self.assertTrue(all("横屏" in s["query"] for s in plan["segments"]))

    def test_normalization_preserves_layers_and_rejects_gaps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = {"ratio": "16:9", "segments": [{"src": "a.mp4", "start": 0, "dur": 2, "text": "园区"}],
                   "voice": {"src": "voice.wav"}, "bgm": {"src": "bgm.wav", "duck": True},
                   "sfx": [{"src": "sfx.wav", "start": 1.2}], "watermark": {"src": "logo.png"}}
            plan = normalize_render_plan(raw, root=root, task_id="task_test", preview_path="preview.mp4")
            self.assertEqual(plan["canvas"]["width"], 1920)
            self.assertEqual([a["kind"] for a in plan["audio"]], ["人声", "背景音乐", "音效"])
            self.assertEqual(len(plan["texts"]), 1)
            raw["segments"][0]["displayText"] = "园区\n招商"
            displayed = normalize_render_plan(raw, root=root, task_id="task_test", preview_path="preview.mp4")
            self.assertEqual(displayed["texts"][0]["text"], "园区\n招商")
            self.assertEqual(raw["segments"][0]["text"], "园区")
            self.assertEqual(len(plan["overlays"]), 1)
            self.assertTrue(plan["clips"][0]["audio_muted"])
            raw["segments"][0]["start"] = 1
            with self.assertRaises(ValueError):
                normalize_render_plan(raw, root=root, task_id="task_test", preview_path="preview.mp4")

    def test_render_binding_rejects_wrong_candidate_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "runtime/workbench/tasks/task_test/render/candidate-v2/adrender.json"
            with self.assertRaises(ValueError):
                load_bound_plan(path, root, "task_test", {"version_label": "candidate-v1"})

    def test_paid_job_recovery_reuses_same_request(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            def invoke(op, args):
                calls.append((op, args))
                if op == "start":
                    return {"jobId": "job-same"}
                return {"status": "completed", "progress": {"current": 1, "total": 1}}
            runner = UnifiedExecution(Path(temp), Path(temp), TemplateStore(Path(temp)), invoke=invoke)
            runner.state = {"task_id": "task_test", "config_digest": "stable"}
            runner._wait_job("配音与分镜", "recommend_storyboard", confirm_cost=True)
            restored = UnifiedExecution(Path(temp), Path(temp), TemplateStore(Path(temp)), invoke=invoke)
            restored._wait_job("配音与分镜", "recommend_storyboard", confirm_cost=True)
            self.assertEqual(len([c for c in calls if c[0] == "start"]), 1)
            self.assertEqual(calls[0][1]["request_id"], "stable配音与分镜")

    def test_unknown_paid_outcome_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            def invoke(op, args):
                return {"jobId": "job-unknown"} if op == "start" else {"status": "interrupted", "needsManualCheck": True}
            runner = UnifiedExecution(Path(temp), Path(temp), TemplateStore(Path(temp)), invoke=invoke)
            runner.state = {"task_id": "task_test", "config_digest": "stable"}
            with self.assertRaises(RuntimeError):
                runner._wait_job("配音与分镜", "recommend_storyboard", confirm_cost=True)
            self.assertEqual(read_state(Path(temp))["status"], "needs_check")
            with self.assertRaises(ValueError):
                runner.resume()

    def test_cross_site_and_non_json_cannot_mutate(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Flask(__name__)
            register_unified_routes(app, Path(temp), lambda job: ({"source": {}}, None, Path(temp)))
            client = app.test_client()
            response = client.post("/api/unified/templates/abcd1234", json={"name": "test"}, headers={"Origin": "https://bad.example"})
            self.assertEqual(response.status_code, 403)
            response = client.post("/api/unified/templates/abcd1234", data="{}")
            self.assertEqual(response.status_code, 403)
            response = client.post("/api/unified/templates/abcd1234", json={"name": "测试"})
            self.assertEqual(response.status_code, 200)

    def test_reference_emphasis_excludes_small_environment_text(self):
        from suo_jian.integrations.cutroom.reference_policy import build_emphasis_policy
        recipe={'onscreen_text':[{'text':'厂房租金1.2元','start':0,'end':2,'confidence':.98,'bbox':{'x':.2,'y':.44,'width':.6,'height':.18}},
                                {'text':'912685','start':8,'end':9,'confidence':1,'bbox':{'x':.2,'y':.46,'width':.03,'height':.03}}]}
        policy=build_emphasis_policy(recipe,.84,.95)
        self.assertEqual(len(policy['evidence']),1)
        self.assertAlmostEqual(policy['number_center_y'],.53)

    def test_emphasis_overlay_remains_independent_in_draft_plan(self):
        raw={'ratio':'16:9','voice':'voice.wav','segments':[{'src':'a.mp4','start':0,'dur':2,'text':'国资','displayText':''}],
             'emphasisOverlays':[{'src':'label.png','start':0,'end':2,'x':0,'y':0,'scale':1,'opacity':1}], 'emphasisReport':[{'text':'国资'}]}
        with tempfile.TemporaryDirectory() as folder:
            plan=normalize_render_plan(raw,root=Path(folder),task_id='task-a',preview_path='preview.mp4')
            self.assertEqual(len(plan['overlays']),1)
            self.assertEqual(len(plan['texts']),0)
            self.assertEqual(plan['emphasis_report'][0]['text'],'国资')

    def test_music_offset_and_fades_survive_execution_mapping(self):
        raw={"ratio":"16:9","voice":"voice.wav","segments":[{"src":"a.mp4","start":0,"dur":10,"text":"园区"}],
             "bgm":{"src":"music.mp3","start":164,"gain":.12,"duck":True,"fadeIn":.35,"fadeOut":1.5},"musicMatch":{"confidence":"approximate"}}
        with tempfile.TemporaryDirectory() as folder:
            plan=normalize_render_plan(raw,root=Path(folder),task_id="task-a",preview_path="preview.mp4")
            music=next(x for x in plan['audio'] if x['kind']=='背景音乐')
            self.assertEqual(music['source_start'],164)
            self.assertEqual(music['fade_out'],1.5)
            self.assertEqual(plan['music_match']['confidence'],'approximate')

    def test_colon_intro_and_short_selling_points_never_merge(self):
        from suo_jian.script_parser.semantic_splitter import semantic_split
        text = "嘉兴平湖新仓智谷天地：国资、可租可售、无二房东。"
        parts = semantic_split(text, target_chars=10, max_chars=15)
        self.assertEqual(parts, ["嘉兴平湖新仓智谷天地：", "国资、", "可租可售、", "无二房东。"])
        self.assertEqual("".join(parts), text)
        self.assertEqual(semantic_split("项目：国资、可租可售。"), ["项目：", "国资、", "可租可售。"]) 

    def test_collection_keeps_qa_blockers_not_fake_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_id = "task_test"
            preview = root / "out" / task_id / "candidate-v2" / "video-final.mp4"
            preview.parent.mkdir(parents=True)
            preview.write_bytes(b"media")
            source = root / "original.mp4"; source.write_bytes(b"original")
            voice = root / "voice.wav"; voice.write_bytes(b"voice")
            render = root / "runtime/workbench/tasks" / task_id / "render/candidate-v2/adrender.json"
            write_json(render, {"ratio": "16:9", "segments": [{"src": str(source), "start": 0, "dur": 2, "text": "园区"}], "voice": str(voice)})
            qa = {"video_spec": {"width": 1920, "height": 1080}, "blocking_count": 1,
                  "issues": [{"ok": False, "detail": "画幅提示"}]}
            candidate = {"candidate_id": "candidate-v2", "version_no": 2, "version_label": "candidate-v2",
                         "output_path": str(preview), "output_signature": file_signature(preview), "qa_report": qa}
            runner = UnifiedExecution(root, root / "results", TemplateStore(root), invoke=lambda op, args: {
                "candidates": [candidate], "render_plan_path": str(render)})
            runner.state = {"task_id": task_id, "human_reviewed": False}
            result = runner.collect()
            self.assertEqual(result["status"], "qa_blocked")
            self.assertFalse(result["human_reviewed"])
            self.assertEqual(len(result["qa_issues"]), 1)
            self.assertTrue((root / "results/execution_plan.json").exists())
            preview.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                runner.collect()


if __name__ == "__main__":
    unittest.main()
