/** Actual TTS client with optional server-owned execution evidence, no queue or DB. */
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { buildTtsHttpRequest, parseVolcengineSseAudio, TtsEnvironment } from "./tts-provider";

export interface ExecutionOptions {
  profileConfigDigest?: string;
  compiledConfigFingerprint?: string;
  record?: (event: TtsExecution) => void | Promise<void>;
}

export interface TtsExecution {
  schema_version: 1;
  executor_version: "tts-controlled-execution-v1";
  phase: "intent" | "result";
  client_request_uuid: string;
  wire_request_id: string | null;
  profile_config_digest: string | null;
  compiled_config_fingerprint: string | null;
  actual_input_digest: string;
  tts_text_sha256: string;
  parameters: Record<string, any>;
  started_at: string;
  finished_at: string | null;
  dispatch_state: "not_sent" | "dispatch_attempted" | "response_received";
  status: "succeeded" | "failed" | "unknown";
  error_code: string | null;
  response: { http_status: number | null; protocol_codes: number[]; identifiers: Record<string, string> };
  raw_audio: { sha256: string; size_bytes: number } | null;
  decoded: { sample_rate: 48000; channels: 1; frame_count: number; duration_seconds: number } | null;
}

export class TtsExecutionError extends Error {
  constructor(public readonly execution: TtsExecution) {
    super(execution.error_code || "TTS_EXECUTION_FAILED");
    this.name = "TtsExecutionError";
  }
}

const hash = (value: string | Buffer) => createHash("sha256").update(value).digest("hex");
function canonical(value: any): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") return `{${Object.keys(value).sort()
    .map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}
function optionalDigest(value: string | undefined): string | null {
  if (value === undefined) return null;
  if (!/^[0-9a-f]{64}$/.test(value)) throw new Error("TTS_FROZEN_DIGEST_INVALID");
  return value;
}
function decodeLength(audio: Buffer): TtsExecution["decoded"] {
  // Seekable MP3 input preserves encoder end-padding; pipe input can retain it.
  const temporaryRoot = fs.realpathSync(os.tmpdir());
  const directory = fs.mkdtempSync(path.join(temporaryRoot, "cutroom-tts-execution-"));
  let output: string;
  try {
    const source = path.join(directory, "raw.mp3");
    fs.writeFileSync(source, audio, { flag: "wx" });
    output = execFileSync("ffmpeg", ["-v", "error", "-nostdin", "-xerror", "-err_detect", "explode",
    "-i", source, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le",
    "-f", "framehash", "-hash", "sha256", "pipe:1"],
    { encoding: "utf8", timeout: 120000, maxBuffer: 8 * 1024 * 1024, windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"] });
  } finally {
    if (path.dirname(fs.realpathSync(directory)) !== temporaryRoot) throw new Error("TTS_TEMP_PATH_INVALID");
    fs.rmSync(directory, { recursive: true, force: true });
  }
  if (!/^#tb 0:\s*1\/48000\s*$/m.test(output)) throw new Error("TTS_AUDIO_DECODE_INVALID");
  let frames = 0;
  for (const line of output.split(/\r?\n/)) {
    if (!line.trim() || line.startsWith("#")) continue;
    const fields = line.split(",").map(value => value.trim());
    const count = Number(fields[3]);
    if (fields.length !== 6 || fields[0] !== "0" || !Number.isSafeInteger(count) || count <= 0
        || Number(fields[4]) !== count * 2 || !/^[0-9a-f]{64}$/.test(fields[5])) {
      throw new Error("TTS_AUDIO_DECODE_INVALID");
    }
    frames += count;
    if (frames > 48000 * 3600) throw new Error("TTS_AUDIO_DURATION_LIMIT");
  }
  if (!frames) throw new Error("TTS_AUDIO_DECODE_INVALID");
  return { sample_rate: 48000, channels: 1, frame_count: frames, duration_seconds: frames / 48000 };
}

async function boundedBody(response: Response): Promise<Buffer> {
  if (!response.body) return Buffer.alloc(0);
  const reader = response.body.getReader();
  const chunks: Buffer[] = [];
  let size = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.length;
      if (size > 64 * 1024 * 1024) throw new Error("TTS_RESPONSE_LIMIT");
      chunks.push(Buffer.from(part.value));
    }
  } catch (error) { await reader.cancel().catch(() => {}); throw error; }
  finally { reader.releaseLock(); }
  return Buffer.concat(chunks);
}

async function execute(text: string, speed: number, environment: TtsEnvironment,
                       options: ExecutionOptions, evidence: boolean) {
  if (typeof text !== "string" || !text.trim() || text.length > 100000 || !Number.isFinite(speed) || speed <= 0) {
    throw new Error("TTS_INPUT_INVALID");
  }
  const env = { ...environment };
  const uuid = randomUUID();
  const request = buildTtsHttpRequest(text, speed, env, uuid);
  const secrets = [env.TTS_ACCESS_TOKEN, env.TTS_API_KEY].filter(Boolean) as string[];
  const safeValue = (value: any): string | null => typeof value === "string" && value.length <= 256
    && !secrets.some(secret => value.includes(secret) || value.includes(encodeURIComponent(secret))) ? value : null;
  const endpoint = new URL(request.endpoint);
  const params = request.body.req_params;
  const parameters = {
    protocol: request.responseKind === "audio" ? "openai" : request.responseKind === "volcengine-sse" ? "volcengine_v3" : "volcengine",
    endpoint: safeValue(endpoint.origin + endpoint.pathname),
    voice: safeValue(params?.speaker ?? request.body.audio?.voice_type ?? request.body.voice),
    resource_id: safeValue(request.headers["X-Api-Resource-Id"] || "") || null,
    model: safeValue(request.body.model || "") || null,
    cluster: safeValue(request.body.app?.cluster || "") || null,
    audio: params ? { ...params.audio_params, sample_rate: params.sample_rate,
      additions: JSON.parse(params.additions) } : request.responseKind === "audio"
      ? { format: request.body.response_format, speed: request.body.speed }
      : { format: request.body.audio.encoding, speed: request.body.audio.speed_ratio },
  };
  const actualText = params?.text ?? request.body.request?.text ?? request.body.input;
  const receipt: TtsExecution = {
    schema_version: 1, executor_version: "tts-controlled-execution-v1", phase: "intent",
    client_request_uuid: uuid, wire_request_id: null,
    profile_config_digest: optionalDigest(options.profileConfigDigest),
    compiled_config_fingerprint: optionalDigest(options.compiledConfigFingerprint),
    actual_input_digest: hash(canonical({ parameters, tts_text_sha256: hash(actualText) })),
    tts_text_sha256: hash(actualText), parameters, started_at: new Date().toISOString(), finished_at: null,
    dispatch_state: "not_sent", status: "unknown", error_code: "REQUEST_INTENT_ONLY",
    response: { http_status: null, protocol_codes: [], identifiers: {} }, raw_audio: null, decoded: null,
  };
  const record = async () => { if (options.record) await options.record(JSON.parse(JSON.stringify(receipt))); };
  try { await record(); }
  catch { receipt.error_code = "TTS_EXECUTION_RECORD_FAILED"; throw new TtsExecutionError(receipt); }
  let audio: Buffer | undefined;
  try {
    receipt.dispatch_state = "dispatch_attempted";
    receipt.wire_request_id = request.responseKind === "audio" ? null : uuid;
    const response = await fetch(request.endpoint, { method: "POST", headers: request.headers,
      body: JSON.stringify(request.body), signal: AbortSignal.timeout(120000),
      redirect: evidence ? "error" : "follow" });
    receipt.dispatch_state = "response_received";
    receipt.response.http_status = response.status;
    for (const header of ["x-request-id", "x-tt-logid", "x-tt-log-id", "x-api-request-id"]) {
      const value = safeValue(response.headers.get(header));
      if (value && /^[A-Za-z0-9._:-]{1,128}$/.test(value)) receipt.response.identifiers[header] = value;
    }
    if (!response.ok) {
      await response.body?.cancel().catch(() => {});
      receipt.status = response.status >= 500 ? "unknown" : "failed";
      receipt.error_code = "TTS_HTTP_REJECTED";
    } else {
      const payload = await boundedBody(response);
      if (request.responseKind === "audio") audio = payload;
      else if (request.responseKind === "volcengine-json") {
        let data: any;
        try { data = JSON.parse(payload.toString("utf8")); }
        catch { throw new Error("TTS_RESPONSE_INVALID"); }
        if (Number.isSafeInteger(data.code)) receipt.response.protocol_codes = [data.code];
        const id = safeValue(data.reqid);
        if (id && /^[A-Za-z0-9._:-]{1,128}$/.test(id)) receipt.response.identifiers.reqid = id;
        if (data.code !== 3000 || typeof data.data !== "string" || !data.data) {
          receipt.status = Number.isSafeInteger(data.code) && data.code !== 3000 ? "failed" : "unknown";
          receipt.error_code = "TTS_PROVIDER_REJECTED";
        } else audio = Buffer.from(data.data, "base64");
      } else {
        const textPayload = payload.toString("utf8");
        let finalCode: number | null = null;
        for (const line of textPayload.split(/\r?\n/)) {
          if (!line.trimStart().startsWith("data:")) continue;
          let data: any;
          try { data = JSON.parse(line.trimStart().slice(5).trim()); }
          catch { throw new Error("TTS_RESPONSE_INVALID"); }
          finalCode = Number.isSafeInteger(data.code) ? data.code : null;
          if (Number.isSafeInteger(data.code) && !receipt.response.protocol_codes.includes(data.code)) {
            receipt.response.protocol_codes.push(data.code);
          }
        }
        if (receipt.response.protocol_codes.some(code => code !== 0 && code !== 20000000)) {
          receipt.status = "failed"; receipt.error_code = "TTS_PROVIDER_REJECTED";
        } else if (evidence && finalCode !== 20000000) {
          receipt.status = "unknown"; receipt.error_code = "TTS_STREAM_INCOMPLETE";
        } else audio = parseVolcengineSseAudio(textPayload);
      }
      if (audio) {
        if (!audio.length) throw new Error("TTS_RESPONSE_INVALID");
        receipt.raw_audio = { sha256: hash(audio), size_bytes: audio.length };
        if (evidence) {
          try { receipt.decoded = decodeLength(audio); }
          catch { receipt.status = "failed"; receipt.error_code = "TTS_AUDIO_DECODE_FAILED"; }
        }
        if (!receipt.error_code || receipt.error_code === "REQUEST_INTENT_ONLY") {
          receipt.status = "succeeded"; receipt.error_code = null;
        }
      }
    }
  } catch {
    receipt.status = "unknown"; receipt.error_code = "TTS_RESPONSE_UNKNOWN";
  }
  receipt.phase = "result"; receipt.finished_at = new Date().toISOString();
  try { await record(); }
  catch { receipt.status = "unknown"; receipt.error_code = "TTS_EXECUTION_RECORD_FAILED"; }
  if (receipt.status !== "succeeded" || !audio) throw new TtsExecutionError(receipt);
  return { audio, execution: receipt };
}

/** Existing advertisement contract: return the provider bytes, no extra decode. */
export async function requestTtsAudio(text: string, speed: number, environment: TtsEnvironment): Promise<Buffer> {
  return (await execute(text, speed, environment, {}, false)).audio;
}

/** A result is local execution evidence; only the worker may register provenance. */
export async function executeTtsWithReceipt(text: string, speed: number, environment: TtsEnvironment,
                                          options: ExecutionOptions = {}) {
  return execute(text, speed, environment, options, true);
}
