import { createHash } from "crypto";

export interface SegmentCacheInput {
  text: string;
  spokenText: string;
  speed: number;
  voice: string;
  protocol: string;
  model: string;
  resourceId: string;
  endpoint: string;
  cluster: string;
  appId: string;
  credentialFingerprint: string;
  trimFilter: string;
}

export function segmentCacheDigest(input: SegmentCacheInput): string {
  return createHash("sha256").update(JSON.stringify({ version: 1, input })).digest("hex");
}

export function cachedSegmentMatches(
  metadata: unknown, inputDigest: string, audioSha256: string,
): boolean {
  if (!metadata || typeof metadata !== "object") return false;
  const value = metadata as Record<string, unknown>;
  return value.inputDigest === inputDigest && value.audioSha256 === audioSha256;
}
