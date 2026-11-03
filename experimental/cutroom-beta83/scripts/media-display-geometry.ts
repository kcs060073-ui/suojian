import { execFileSync } from "child_process";

type DisplayDirection = "9:16" | "16:9";

type VideoStream = {
  width?: unknown;
  height?: unknown;
  sample_aspect_ratio?: unknown;
  side_data_list?: unknown;
  tags?: unknown;
};

function positiveDimension(value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value <= 0)
    throw new Error("SOURCE_GEOMETRY_INVALID: source dimensions are missing or invalid");
  return value;
}

function pixelAspectRatio(value: unknown): number {
  if (value === undefined || value === null || value === "N/A") return 1;
  if (typeof value !== "string")
    throw new Error("SOURCE_GEOMETRY_INVALID: sample aspect ratio is invalid");
  const match = /^(\d+):(\d+)$/.exec(value);
  if (!match || Number(match[2]) === 0)
    throw new Error("SOURCE_GEOMETRY_INVALID: sample aspect ratio is invalid");
  return Number(match[1]) / Number(match[2]);
}

function quarterTurns(stream: VideoStream): number {
  const values: unknown[] = [];
  if (stream.side_data_list !== undefined) {
    if (!Array.isArray(stream.side_data_list))
      throw new Error("SOURCE_GEOMETRY_INVALID: rotation metadata is invalid");
    for (const item of stream.side_data_list) {
      if (item && typeof item === "object" && "rotation" in item)
        values.push(item.rotation);
    }
  }
  if (stream.tags && typeof stream.tags === "object" && "rotate" in stream.tags)
    values.push(stream.tags.rotate);
  if (!values.length) return 0;
  const turns = values.map((value) => {
    const angle = Number(value);
    const turn = Math.round(angle / 90);
    if (!Number.isFinite(angle) || Math.abs(angle - turn * 90) > 1e-6)
      throw new Error("SOURCE_GEOMETRY_INVALID: rotation must be a quarter turn");
    return ((turn % 4) + 4) % 4;
  });
  if (new Set(turns).size !== 1)
    throw new Error("SOURCE_GEOMETRY_INVALID: rotation metadata conflicts");
  return turns[0];
}

export function displayDirectionFromStream(stream: VideoStream): DisplayDirection {
  let displayWidth = positiveDimension(stream.width) * pixelAspectRatio(stream.sample_aspect_ratio);
  let displayHeight = positiveDimension(stream.height);
  if (!Number.isFinite(displayWidth) || displayWidth <= 0)
    throw new Error("SOURCE_GEOMETRY_INVALID: displayed width is invalid");
  if (quarterTurns(stream) % 2) [displayWidth, displayHeight] = [displayHeight, displayWidth];
  if (Math.abs(displayWidth - displayHeight) < 1e-9)
    throw new Error("SOURCE_GEOMETRY_INVALID: square source has no output direction");
  return displayWidth > displayHeight ? "16:9" : "9:16";
}

export function probeDisplayDirection(file: string, mediaKind: "video" | "image" = "video"): DisplayDirection {
  let payload: unknown;
  try {
    const probeArgs = mediaKind === "image"
      ? ["-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1", "-show_frames", "-of", "json", file]
      : ["-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", file];
    payload = JSON.parse(execFileSync("ffprobe", probeArgs,
      { encoding: "utf8", maxBuffer: 1024 * 1024, timeout: 30_000 }));
  } catch {
    throw new Error("SOURCE_GEOMETRY_UNAVAILABLE: cannot probe source display metadata");
  }
  const entries = mediaKind === "image"
    ? (payload as { frames?: unknown })?.frames
    : (payload as { streams?: unknown })?.streams;
  if (!Array.isArray(entries) || !entries[0] || typeof entries[0] !== "object")
    throw new Error("SOURCE_GEOMETRY_UNAVAILABLE: source display metadata is missing");
  return displayDirectionFromStream(entries[0] as VideoStream);
}

export function assertSourceDirection(file: string, expected: DisplayDirection, label: string,
                                      mediaKind: "video" | "image" = "video"): void {
  const actual = probeDisplayDirection(file, mediaKind);
  if (actual !== expected)
    throw new Error(`SOURCE_ORIENTATION_CONFLICT: ${label} displays ${actual}, task requires ${expected}`);
}

export function assertOutputDirection(width: number, height: number, expected: DisplayDirection): void {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0)
    throw new Error("OUTPUT_ORIENTATION_CONFLICT: output dimensions are invalid");
  const actual = width > height ? "16:9" : height > width ? "9:16" : null;
  if (actual !== expected)
    throw new Error(`OUTPUT_ORIENTATION_CONFLICT: output displays ${actual || "square"}, task requires ${expected}`);
}
