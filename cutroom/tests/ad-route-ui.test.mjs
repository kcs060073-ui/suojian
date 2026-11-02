import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  ROUTE_OPTIONS,
  buildCreateRequest,
} from "../web-ui/public/task-create.mjs";
import {
  normalizePublishDraft,
} from "../web-ui/public/publish-controls.mjs";

test("信息流广告路线可选并只提交广告所需输入", () => {
  assert.equal(
    ROUTE_OPTIONS.find((item) => item.key === "ad_mix").available,
    true,
  );
  assert.deepEqual(
    buildCreateRequest({
      route: "ad_mix",
      title: "招商推介会信息流",
      final_script: "三十秒讲清楚活动价值。",
      primary_library_id: "event-footage",
      selected_generic_ids: ["generic-city"],
      include_default_generic: false,
      purpose: "抖音信息流获客",
      target_duration_seconds: 30,
      aspect_ratio: "9:16",
      voice_mode: "ai_tts",
      planning_profile_id: "profile-ad-planning",
      tts_profile_id: "profile-ad-tts",
      tts_voice_id: "voice-for-this-ad",
      broll_profile_id: "profile-ad-broll",
      digital_human_mp4: "E:\\must-not-leak.mp4",
      subtype: "zhaoshang",
    }),
    {
      route: "ad_mix",
      orchestration_mode: "standalone",
      title: "招商推介会信息流",
      final_script: "三十秒讲清楚活动价值。",
      primary_library_id: "event-footage",
      selected_generic_ids: ["generic-city"],
      include_default_generic: false,
      purpose: "抖音信息流获客",
      target_duration_seconds: 30,
      aspect_ratio: "9:16",
      voice_mode: "ai_tts",
      tts_voice_id: "voice-for-this-ad",
      model_profile_ids: {
        planning: "profile-ad-planning",
        tts: "profile-ad-tts",
        broll: "profile-ad-broll",
      },
    },
  );
});

test("信息流广告未选择本条视频音色时不会进入预检", () => {
  assert.throws(
    () => buildCreateRequest({
      route: "ad_mix",
      tts_profile_id: "profile-ad-tts",
      tts_voice_id: "",
    }),
    /请选择本条视频音色/,
  );
});

test("信息流广告沿用统一的封面标签和可编辑交付契约", () => {
  assert.deepEqual(normalizePublishDraft({
    platform: "抖音",
    tags: "招商，获客,招商",
    coverChoice: "pending",
    deliveryType: "editable",
  }), {
    platform: "抖音",
    title: "",
    description: "",
    tags: ["招商", "获客"],
    coverChoice: "pending",
    deliveryType: "editable",
  });
  assert.equal(ROUTE_OPTIONS.find((item) => item.key === "park_video").available, false);
});

test("创建页按路线切换数字人输入与广告输入", () => {
  const html = readFileSync(
    new URL("../web-ui/public/index.html", import.meta.url),
    "utf8",
  );
  const app = readFileSync(
    new URL("../web-ui/public/app.mjs", import.meta.url),
    "utf8",
  );
  assert.match(html, /data-route-digital/);
  assert.match(html, /data-route-ad/);
  assert.match(html, /name="aspect_ratio"/);
  assert.match(html, /name="voice_mode"/);
  assert.match(html, /name="planning_profile_id"/);
  assert.match(html, /name="broll_profile_id"/);
  assert.match(html, /name="tts_profile_id"/);
  assert.match(html, /name="tts_voice_id"/);
  assert.doesNotMatch(html, /name="copywriting_profile_id"/);
  assert.doesNotMatch(html, /name="storyboard_profile_id"/);
  assert.doesNotMatch(html, /name="packaging_profile_id"/);
  assert.doesNotMatch(html, /name="video_generation_profile_id"/);
  assert.match(app, /function updateCreateRouteFields/);
  assert.match(app, /aspect_ratio: data\.get\("aspect_ratio"\)/);
  assert.match(app, /voice_mode: data\.get\("voice_mode"\)/);
  assert.match(app, /planning_profile_id: data\.get\("planning_profile_id"\)/);
  assert.match(app, /broll_profile_id: data\.get\("broll_profile_id"\)/);
});
