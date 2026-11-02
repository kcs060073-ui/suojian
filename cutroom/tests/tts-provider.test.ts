import * as assert from "node:assert/strict";
import { test } from "node:test";

import { buildTtsHttpRequest, parseVolcengineSseAudio } from "../scripts/tts-provider";


test("New API TTS uses OpenAI speech contract and exact frozen model", () => {
  const request = buildTtsHttpRequest("连接测试", 1.1, {
    TTS_PROTOCOL: "openai",
    TTS_ENDPOINT: "http://127.0.0.1:3000/v1/audio/speech",
    TTS_ACCESS_TOKEN: "sk-gateway-41",
    TTS_MODEL: "doubao-tts-frozen",
    TTS_VOICE_TYPE: "zh_female_frozen",
  }, "request-1");

  assert.equal(request.responseKind, "audio");
  assert.equal(request.endpoint, "http://127.0.0.1:3000/v1/audio/speech");
  assert.equal(request.headers.Authorization, "Bearer sk-gateway-41");
  assert.deepEqual(request.body, {
    model: "doubao-tts-frozen",
    voice: "zh_female_frozen",
    input: "连接测试",
    response_format: "mp3",
    speed: 1.1,
  });
});

test("legacy Volcengine TTS contract remains available", () => {
  const request = buildTtsHttpRequest("旧链路", 1, {
    TTS_APPID: "appid",
    TTS_ACCESS_TOKEN: "token",
    TTS_CLUSTER: "volcano_tts",
    TTS_VOICE_TYPE: "voice",
    TTS_ENDPOINT: "https://openspeech.example/api/v1/tts",
  }, "request-2");

  assert.equal(request.responseKind, "volcengine-json");
  assert.equal(request.headers.Authorization, "Bearer;token");
  assert.equal(request.body.app.appid, "appid");
  assert.equal(request.body.request.reqid, "request-2");
});

test("new Volcengine TTS uses API key V3 SSE contract", () => {
  const request = buildTtsHttpRequest("新版链路", 1.1, {
    TTS_PROTOCOL: "volcengine_v3",
    TTS_API_KEY: "speech-api-key",
    TTS_RESOURCE_ID: "seed-tts-1.0",
    TTS_VOICE_TYPE: "voice-v3",
    TTS_ENDPOINT: "https://openspeech.example/api/v3/tts/unidirectional/sse",
  }, "request-v3");

  assert.equal(request.responseKind, "volcengine-sse");
  assert.equal(request.headers["X-Api-Key"], "speech-api-key");
  assert.equal(request.headers["X-Api-Resource-Id"], "seed-tts-1.0");
  assert.equal(request.headers["X-Api-Request-Id"], "request-v3");
  assert.equal(request.body.req_params.speaker, "voice-v3");
  assert.equal(request.body.req_params.audio_params.format, "mp3");
  assert.equal(request.body.req_params.audio_params.speech_rate, 10);
});

test("V3 SSE audio parser joins chunks and rejects provider errors", () => {
  const audio = parseVolcengineSseAudio([
    'data: {"code":0,"data":"SUQz"}',
    "",
    'data: {"code":0,"data":"dGlueQ=="}',
    'data: {"code":20000000,"message":"done"}',
  ].join("\n"));
  assert.equal(audio.toString("utf8"), "ID3tiny");
  assert.throws(
    () => parseVolcengineSseAudio('data: {"code":55000000,"message":"resource not granted"}'),
    /未开通所选配音服务版本/,
  );
});

test("V3 SSE errors do not echo arbitrary provider response text", () => {
  assert.throws(
    () => parseVolcengineSseAudio(
      'data: {"code":55000000,"message":"upstream-private-detail"}',
    ),
    (error: Error) => {
      assert.match(error.message, /错误码 55000000/);
      assert.doesNotMatch(error.message, /upstream-private-detail/);
      return true;
    },
  );
});
