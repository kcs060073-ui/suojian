/** Server-private sentence generation. No dotenv, DB, cache, queue or apply. */
import { createHash, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { buildTtsHttpRequest, TtsEnvironment } from "./tts-provider";
import { executeTtsWithReceipt, TtsExecution, TtsExecutionError } from "./tts-execution";

const sha = (value: string | Buffer) => createHash("sha256").update(value).digest("hex");
class JsonNumber { constructor(readonly source: string) {} }
function canonical(value: any): string {
  if (value instanceof JsonNumber) return value.source;
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") return `{${Object.keys(value).sort()
    .map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}
function fail(code: string): never { throw new Error(code); }
function object(value: any, keys: string[]) {
  if (!value || typeof value !== "object" || Array.isArray(value)
      || canonical(Object.keys(value).sort()) !== canonical([...keys].sort())) fail("VOICE_TTS_INPUT_INVALID");
}
function integer(value: any): boolean { return Number.isSafeInteger(value) && value >= 0; }
function digest(value: any): boolean { return typeof value === "string" && /^[0-9a-f]{64}$/.test(value); }
function characters(text: any): string[] {
  if (typeof text !== "string" || !text.trim() || text.length > 100000
      || /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(text)) {
    fail("VOICE_TTS_TEXT_INVALID");
  }
  return Array.from(text);
}
function checkConfig(config: any) {
  object(config, ["script", "script_sha256", "rules", "voice", "tts_text", "span_map", "compiler_version", "fingerprint"]);
  const chars = characters(config.script); characters(config.tts_text);
  object(config.voice, ["provider", "resource", "voice", "revision"]);
  if (Object.values(config.voice).some(v => typeof v !== "string" || !v.trim() || v.length > 256)
      || !Array.isArray(config.rules) || config.rules.length > 500 || !Array.isArray(config.span_map)
      || config.compiler_version !== "span-substitution-v1" || config.script_sha256 !== sha(config.script)) {
    fail("VOICE_TTS_CONFIG_INVALID");
  }
  const body = { ...config }; delete body.fingerprint;
  if (!digest(config.fingerprint) || sha(canonical(body)) !== config.fingerprint) fail("VOICE_TTS_CONFIG_DIGEST_INVALID");
  // Rebuild only the existing span-substitution compiler result, never add a reading rule.
  let previous = 0, cursor = 0;
  const pieces: string[] = [], mapping: any[] = [];
  for (const rule of config.rules) {
    object(rule, ["start", "end", "original", "reading", "version", "source"]);
    if (!integer(rule.start) || !integer(rule.end) || !(previous <= rule.start && rule.start < rule.end && rule.end <= chars.length)
        || !integer(rule.version) || rule.version < 1 || !["user", "confirmed_dictionary"].includes(rule.source)
        || chars.slice(rule.start,rule.end).join("") !== rule.original || rule.original === rule.reading) {
      fail("VOICE_TTS_RULE_INVALID");
    }
    const reading = characters(rule.reading);
    const prefix = chars.slice(previous,rule.start).join("");
    pieces.push(prefix);
    if (prefix) mapping.push({original_start:previous,original_end:rule.start,tts_start:cursor,tts_end:cursor+rule.start-previous});
    cursor += rule.start-previous; pieces.push(rule.reading);
    mapping.push({original_start:rule.start,original_end:rule.end,tts_start:cursor,tts_end:cursor+reading.length});
    cursor += reading.length; previous = rule.end;
  }
  pieces.push(chars.slice(previous).join(""));
  if (previous < chars.length) mapping.push({original_start:previous,original_end:chars.length,tts_start:cursor,tts_end:cursor+chars.length-previous});
  if (pieces.join("") !== config.tts_text || canonical(mapping) !== canonical(config.span_map)) fail("VOICE_TTS_COMPILED_TEXT_INVALID");
  return chars;
}
function validate(value: any, environment: TtsEnvironment, digestSource: any = value) {
  object(value, ["schema_version", "task_id", "draft_id", "request_id", "context_digest", "base_revision", "profile_config_digest", "config", "segments",
    ...(value?.schema_version === 2 ? ["generation_segment_ids", "reuse_plan_digest", "source_bundle_id", "reuse_plan"] : [])]);
  if (![1,2].includes(value.schema_version) || !integer(value.base_revision) || !digest(value.context_digest) || !digest(value.profile_config_digest)
      || [value.task_id,value.draft_id,value.request_id].some(v => typeof v !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(v))
      || !Array.isArray(value.segments) || !value.segments.length || value.segments.length > 100) fail("VOICE_TTS_INPUT_INVALID");
  const chars = checkConfig(value.config), voice = value.config.voice;
  const protocol = (environment.TTS_PROTOCOL || "").trim().toLowerCase();
  if (environment.CUTROOM_TTS_PROVIDER !== voice.provider || environment.CUTROOM_TTS_PROFILE_DIGEST !== value.profile_config_digest
      || voice.revision !== value.profile_config_digest || environment.TTS_VOICE_TYPE !== voice.voice
      || !["volcengine_v3","volcengine","openai"].includes(protocol)
      || (voice.provider === "doubao_tts" ? protocol === "openai" : protocol !== "openai")
      || (protocol === "volcengine_v3" ? environment.TTS_RESOURCE_ID : protocol === "openai"
        ? environment.TTS_MODEL : environment.CUTROOM_TTS_RESOURCE_ID) !== voice.resource) fail("VOICE_TTS_FROZEN_ENV_MISMATCH");
  let cursor = 0;
  const ids = new Set<string>();
  for (const segment of value.segments) {
    object(segment,["segment_id","original_start","original_end","config"]);
    const id = segment.segment_id;
    if (typeof id !== "string" || !/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(id)
        || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i.test(id) || ids.has(id.toLowerCase())) fail("VOICE_TTS_SEGMENT_ID_INVALID");
    ids.add(id.toLowerCase()); checkConfig(segment.config);
    if (!integer(segment.original_start) || !integer(segment.original_end) || segment.original_start !== cursor
        || segment.original_end <= cursor || segment.original_end > chars.length
        || segment.config.script !== chars.slice(cursor,segment.original_end).join("")
        || canonical(segment.config.voice) !== canonical(voice)) fail("VOICE_TTS_SEGMENT_CONFIG_MISMATCH");
    const projected = [];
    for (const rule of value.config.rules) {
      if (rule.end <= segment.original_start || rule.start >= segment.original_end) continue;
      if (rule.start < segment.original_start || rule.end > segment.original_end) fail("VOICE_TTS_RULE_CROSSES_SEGMENT");
      projected.push({...rule,start:rule.start-segment.original_start,end:rule.end-segment.original_start});
    }
    if (canonical(projected) !== canonical(segment.config.rules)) fail("VOICE_TTS_SEGMENT_RULE_MISMATCH");
    cursor = segment.original_end;
  }
  if (cursor !== chars.length || value.segments.map((s: any) => s.config.script).join("") !== value.config.script
      || value.segments.map((s: any) => s.config.tts_text).join("") !== value.config.tts_text) fail("VOICE_TTS_ORIGINAL_COVERAGE_INVALID");
  // Validate every frozen segment first, including those inherited by the root assembler.
  let selected = value.segments;
  if (value.schema_version === 2) {
    if (!digest(value.reuse_plan_digest) || !Array.isArray(value.generation_segment_ids)
        || !value.generation_segment_ids.length || value.generation_segment_ids.length > value.segments.length) {
      fail("VOICE_TTS_GENERATION_SUBSET_INVALID");
    }
    let previousIndex = -1;
    selected = value.generation_segment_ids.map((id: any) => {
      const index = value.segments.findIndex((segment: any) => segment.segment_id === id);
      // Exact IDs, strict original order: no unknown, duplicate, reordered or case-folded selection.
      if (index <= previousIndex) fail("VOICE_TTS_GENERATION_SUBSET_INVALID");
      previousIndex = index;
      return value.segments[index];
    });
  }
  if (value.schema_version === 2) {
    const frozen = value.reuse_plan;
    if (!digest(value.source_bundle_id) || !frozen || typeof frozen !== "object" || Array.isArray(frozen)) {
      fail("VOICE_TTS_REUSE_PLAN_INVALID");
    }
    const body = {...digestSource.reuse_plan}; delete body.digest;
    const planned = frozen.plan?.segments;
    if (!digest(frozen.digest) || sha(canonical(body)) !== frozen.digest || value.reuse_plan_digest !== frozen.digest
        || frozen.task_id !== value.task_id || frozen.context_digest !== value.context_digest
        || frozen.base_revision !== value.base_revision || frozen.config_fingerprint !== value.config.fingerprint
        || !integer(frozen.maximum_requests) || frozen.maximum_requests !== selected.length
        || canonical(frozen.generation_segments) !== canonical(selected)
        || !Array.isArray(planned) || planned.length !== value.segments.length
        || planned.some((part: any, index: number) => !part || part.segment_id !== value.segments[index].segment_id
          || canonical(part.config) !== canonical(value.segments[index].config) || !["reuse","generate"].includes(part.mode))
        || canonical(planned.filter((part: any) => part.mode === "generate").map((part: any) => part.segment_id))
          !== canonical(value.generation_segment_ids)) fail("VOICE_TTS_REUSE_PLAN_INVALID");
  }
  // Validate actual request construction and credentials in memory before reserving outputs.
  const request = buildTtsHttpRequest(value.config.tts_text,1,environment,randomUUID());
  const endpoint = new URL(request.endpoint);
  if (!["https:","http:"].includes(endpoint.protocol) || endpoint.username || endpoint.password) fail("VOICE_TTS_ENDPOINT_INVALID");
  return selected;
}
function noLinkedParents(target: string) {
  for (let current = path.dirname(target);; current = path.dirname(current)) {
    if (fs.existsSync(current) && fs.lstatSync(current).isSymbolicLink()) fail("VOICE_TTS_OUTPUT_PATH_INVALID");
    if (path.dirname(current) === current) break;
  }
}
function writeExclusive(file: string, data: string | Buffer) {
  const fd = fs.openSync(file,"wx");
  try { fs.writeFileSync(fd,data); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}
function atomicJson(file: string, value: any) {
  const temporary = file + "." + randomUUID() + ".tmp";
  try { writeExclusive(temporary,JSON.stringify(value)); fs.renameSync(temporary,file); }
  finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
}

export async function runVoiceEditingTts(input: any, outputRoot: string, environment: TtsEnvironment, sourceJson?: string) {
  // Snapshot all inputs. Async recording cannot change future sentence inputs.
  const value = JSON.parse(JSON.stringify(input));
  const env = { ...environment };
  // Python-produced private JSON can contain 1.0/0.0 in inherited timing metadata.
  // Keep its numeric lexemes for canonical SHA; ordinary JSON.parse erases that distinction.
  let digestSource = value;
  if (sourceJson !== undefined) {
    if (typeof sourceJson !== "string" || Buffer.byteLength(sourceJson) > 5*1024*1024) fail("VOICE_TTS_INPUT_JSON_INVALID");
    digestSource = (JSON.parse as any)(sourceJson, (_key: string, item: any, context: any) => {
      if (typeof item !== "number") return item;
      if (!context || typeof context.source !== "string") fail("VOICE_TTS_JSON_NUMBER_SOURCE_REQUIRED");
      return new JsonNumber(context.source);
    });
    if (canonical(JSON.parse(canonical(digestSource))) !== canonical(value)) fail("VOICE_TTS_INPUT_SOURCE_MISMATCH");
  }
  const generationSegments = validate(value,env,digestSource);
  const root = path.resolve(outputRoot);
  if (fs.existsSync(root)) fail("VOICE_TTS_OUTPUT_EXISTS");
  noLinkedParents(root);
  fs.mkdirSync(path.dirname(root),{recursive:true});
  try { fs.mkdirSync(root); } catch { fail("VOICE_TTS_OUTPUT_EXISTS"); }
  fs.mkdirSync(path.join(root,"raw"));
  const binding = {schema_version:value.schema_version,executor_version:"voice-editing-tts-v1",run_id:randomUUID(),input_sha256:sha(canonical(digestSource)),
    task_id:value.task_id,draft_id:value.draft_id,request_id:value.request_id,context_digest:value.context_digest,
    base_revision:value.base_revision,profile_config_digest:value.profile_config_digest,
    compiled_config_fingerprint:value.config.fingerprint,script_sha256:value.config.script_sha256,
    ...(value.schema_version === 2 ? {reuse_plan_digest:value.reuse_plan_digest,
      generation_segment_ids:[...value.generation_segment_ids]} : {})};
  const execution = {...binding,events:[] as any[]};
  const result = {...binding,status:"running",code:null as string|null,failed_segment_id:null as string|null,
    sample_rate:48000,channels:1,total_frame_count:0,ready_for_application:false,segments:[] as any[]};
  const executionPath = path.join(root,"execution.json"), resultPath = path.join(root,"result.json");
  writeExclusive(executionPath,JSON.stringify(execution)); writeExclusive(resultPath,JSON.stringify(result));
  for (const segment of generationSegments) {
    const indexes: number[] = [];
    try {
      const generated = await executeTtsWithReceipt(segment.config.tts_text,1,env,{
        profileConfigDigest:value.profile_config_digest,compiledConfigFingerprint:segment.config.fingerprint,
        record:(event: TtsExecution) => {
          const index = execution.events.length;
          execution.events.push({event_index:index,segment_id:segment.segment_id,...event}); indexes.push(index);
          atomicJson(executionPath,execution);
        },
      });
      const receipt = generated.execution, decoded = receipt.decoded!;
      if (!decoded || indexes.length !== 2 || receipt.tts_text_sha256 !== sha(segment.config.tts_text)
          || result.total_frame_count + decoded.frame_count > 48000*3600) fail("VOICE_TTS_EXECUTION_INVALID");
      const relative = `raw/${segment.segment_id}.mp3`, file = path.join(root,relative);
      writeExclusive(file,generated.audio);
      if (sha(fs.readFileSync(file)) !== receipt.raw_audio!.sha256) fail("VOICE_TTS_RAW_CHANGED");
      const start = result.total_frame_count, end = start + decoded.frame_count;
      result.segments.push({segment_id:segment.segment_id,original_start:segment.original_start,original_end:segment.original_end,
        original_text:segment.config.script,compiled_config_fingerprint:segment.config.fingerprint,
        tts_text_sha256:receipt.tts_text_sha256,actual_input_digest:receipt.actual_input_digest,
        client_request_uuid:receipt.client_request_uuid,event_indexes:indexes,raw_path:relative,
        raw_sha256:receipt.raw_audio!.sha256,raw_size_bytes:receipt.raw_audio!.size_bytes,decoded,
        start_frame:start,end_frame:end,timing_kind:"generated_segment_boundary"});
      result.total_frame_count = end; atomicJson(resultPath,result);
    } catch (error) {
      result.status = error instanceof TtsExecutionError ? error.execution.status : "failed";
      result.code = error instanceof TtsExecutionError ? error.execution.error_code : "VOICE_TTS_OUTPUT_FAILED";
      result.failed_segment_id = segment.segment_id; atomicJson(resultPath,result);
      return result;
    }
  }
  result.status="succeeded"; atomicJson(resultPath,result); return result;
}

export async function main(argv: string[], environment: TtsEnvironment = process.env): Promise<number> {
  const options = new Map<string,string>();
  for (let i=0;i<argv.length;i+=2) {
    if (!["--input","--output-root"].includes(argv[i]) || options.has(argv[i]) || !argv[i+1] || argv[i+1].startsWith("--")) fail("VOICE_TTS_ARGUMENTS_INVALID");
    options.set(argv[i],argv[i+1]);
  }
  if (options.size !== 2) fail("VOICE_TTS_ARGUMENTS_INVALID");
  const file=path.resolve(options.get("--input")!);
  if (fs.lstatSync(file).isSymbolicLink() || !fs.statSync(file).isFile() || fs.statSync(file).size > 5*1024*1024) fail("VOICE_TTS_INPUT_FILE_INVALID");
  const bytes=fs.readFileSync(file); if (bytes.length > 5*1024*1024) fail("VOICE_TTS_INPUT_FILE_INVALID");
  let value: any;
  try { value=JSON.parse(bytes.toString("utf8")); } catch { fail("VOICE_TTS_INPUT_JSON_INVALID"); }
  const result=await runVoiceEditingTts(value,options.get("--output-root")!,environment,value.schema_version === 2 ? bytes.toString("utf8") : undefined);
  return result.status === "succeeded" ? 0 : 1;
}
if (require.main === module) main(process.argv.slice(2)).then(code => { process.exitCode=code; })
  .catch(error => { const code = error instanceof Error && /^VOICE_TTS_[A-Z_]+$/.test(error.message)
    ? error.message : "VOICE_TTS_EXECUTION_ABORTED"; console.error(code); process.exitCode=1; });
