export interface TtsEnvironment {
  [key: string]: string | undefined;
}

export interface TtsHttpRequest {
  endpoint: string;
  headers: Record<string, string>;
  body: any;
  responseKind: "audio" | "volcengine-json" | "volcengine-sse";
}

/** Strip repeated ID3 metadata only at proven complete MPEG layer III boundaries.
 * V3 can return independently tagged sentence streams in one SSE response.
 * Never scan payload bytes for ID3 or drop bad/truncated audio to make it pass.
 */
export function normalizeSentenceMp3(audio: Buffer): Buffer {
  let cursor = 0, removed = false, frames = 0;
  let format: string | null = null;
  const parts: Buffer[] = [];
  while (cursor < audio.length) {
    if (audio[cursor] === 0x49 && audio[cursor + 1] === 0x44 && audio[cursor + 2] === 0x33) {
      if (cursor + 10 > audio.length) return audio;
      const version = audio[cursor + 3], flags = audio[cursor + 5];
      const sizes = audio.subarray(cursor + 6, cursor + 10);
      if (![2, 3, 4].includes(version) || audio[cursor + 4] === 255
        || sizes.some(value => value >= 128) || (flags & (version === 2 ? 63 : version === 3 ? 31 : 15))) return audio;
      const size = 10 + sizes.reduce((total, value) => total * 128 + value, 0)
        + (version === 4 && (flags & 16) ? 10 : 0);
      if (cursor + size > audio.length) return audio;
      if (cursor === 0) parts.push(audio.subarray(cursor, cursor + size));
      else { if (!frames || cursor + size === audio.length) return audio; removed = true; }
      cursor += size;
      continue;
    }
    if (cursor + 4 > audio.length) return audio;
    const header = audio.readUInt32BE(cursor);
    const version = (header >>> 19) & 3, layer = (header >>> 17) & 3;
    const bitrateIndex = (header >>> 12) & 15, rateIndex = (header >>> 10) & 3;
    if ((header >>> 21) !== 2047 || version === 1 || layer !== 1
      || bitrateIndex === 0 || bitrateIndex === 15 || rateIndex === 3) return audio;
    const rate = [44100, 48000, 32000][rateIndex] / (version === 3 ? 1 : version === 2 ? 2 : 4);
    const key = `${version}/${rate}/${((header >>> 6) & 3) === 3 ? 1 : 2}`;
    if (format !== null && format !== key) return audio;
    format = key;
    const bitrates = version === 3 ? [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320]
      : [0,8,16,24,32,40,48,56,64,80,96,112,128,144,160];
    const size = Math.floor((version === 3 ? 144 : 72) * bitrates[bitrateIndex] * 1000 / rate) + ((header >>> 9) & 1);
    if (cursor + size > audio.length) return audio;
    parts.push(audio.subarray(cursor, cursor + size));cursor += size;frames++;
  }
  return removed && frames ? Buffer.concat(parts) : audio;
}

export function parseVolcengineSseAudio(payload: string): Buffer {
  const chunks: Buffer[] = [];
  for (const rawLine of payload.split(/\r?\n/)) {
    const line = rawLine.trimStart();
    if (!line.startsWith("data:")) continue;
    let event: any;
    try {
      event = JSON.parse(line.slice(5).trim());
    } catch {
      throw new Error("豆包 V3 响应格式不兼容");
    }
    const code = event?.code ?? 0;
    if (code !== 0 && code !== 20000000) {
      const providerMessage = String(event?.message || "").toLowerCase();
      if (providerMessage.includes("resource not granted")) {
        throw new Error("当前 API Key 未开通所选配音服务版本，请在火山控制台开通后重试");
      }
      if (providerMessage.includes("api key") || providerMessage.includes("unauthorized")) {
        throw new Error("豆包 API Key 无效或没有访问权限");
      }
      throw new Error(`豆包 V3 请求被供应商拒绝（错误码 ${code}）`);
    }
    if (typeof event?.data === "string" && event.data) {
      chunks.push(Buffer.from(event.data, "base64"));
    }
  }
  if (!chunks.length) throw new Error("豆包 V3 没有返回有效音频");
  return normalizeSentenceMp3(Buffer.concat(chunks));
}

export function buildTtsHttpRequest(
  text: string,
  speed: number,
  environment: TtsEnvironment,
  requestId: string,
): TtsHttpRequest {
  const protocol = (environment.TTS_PROTOCOL || "volcengine").trim().toLowerCase();
  const endpoint = environment.TTS_ENDPOINT
    || (protocol === "volcengine_v3"
      ? "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse"
      : "https://openspeech.bytedance.com/api/v1/tts");
  const token = environment.TTS_ACCESS_TOKEN || "";
  const voice = environment.TTS_VOICE_TYPE || "";

  if (protocol === "openai") {
    const model = environment.TTS_MODEL || "";
    if (!token || !voice || !model) {
      throw new Error("OpenAI 兼容 TTS 缺少 Token、模型或音色");
    }
    return {
      endpoint,
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: {
        model,
        voice,
        input: text,
        response_format: "mp3",
        speed,
      },
      responseKind: "audio",
    };
  }

  if (protocol === "volcengine_v3") {
    const apiKey = environment.TTS_API_KEY || "";
    const resourceId = environment.TTS_RESOURCE_ID || "";
    if (!apiKey || !resourceId || !voice) {
      throw new Error("豆包 V3 缺少 API Key、配音服务版本或音色");
    }
    const speechRate = Math.max(-50, Math.min(100, Math.round((speed - 1) * 100)));
    return {
      endpoint,
      headers: {
        "Content-Type": "application/json",
        "X-Api-Key": apiKey,
        "X-Api-Resource-Id": resourceId,
        "X-Api-Request-Id": requestId,
      },
      body: {
        user: { uid: "ad-pipeline" },
        req_params: {
          text,
          speaker: voice,
          sample_rate: 24000,
          audio_params: {
            format: "mp3",
            speech_rate: speechRate,
            loudness_rate: 0,
            bit_rate: 64000,
          },
          additions: JSON.stringify({
            post_process: { pitch: 0 },
            disable_markdown_filter: true,
            enable_latex_tn: false,
            latex_parser: "v2",
          }),
        },
      },
      responseKind: "volcengine-sse",
    };
  }

  const appid = environment.TTS_APPID || "";
  const cluster = environment.TTS_CLUSTER || "volcano_tts";
  if (!appid || !token || !voice) {
    throw new Error("火山 TTS 缺少 AppID、Token 或音色");
  }
  return {
    endpoint,
    headers: {
      Authorization: `Bearer;${token}`,
      "Content-Type": "application/json",
    },
    body: {
      app: { appid, token, cluster },
      user: { uid: "ad-pipeline" },
      audio: { voice_type: voice, encoding: "mp3", speed_ratio: speed },
      request: { reqid: requestId, text, operation: "query" },
    },
    responseKind: "volcengine-json",
  };
}
