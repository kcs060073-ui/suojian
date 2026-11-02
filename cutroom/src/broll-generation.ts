import fs from "node:fs";
import path from "node:path";

export interface BrollRequest {
  key: string;
  prompt: string;
}

export interface BrollProvenanceItem {
  key: string;
  prompt: string;
  model: string;
  taskId: string;
  file: string;
  createdAt: string;
  sha256: string;
}

export type GenerationAttemptStage = "created" | "poll" | "download" | "probe" | "hash" | "provenance" | "accepted";
export type GenerationAttemptStatus = "pending" | "failed" | "accepted";

export interface BrollGenerationAttempt {
  key: string;
  model: string;
  taskId: string;
  file: string;
  createdAt: string;
  stage: GenerationAttemptStage;
  status: GenerationAttemptStatus;
  error?: string;
  width?: number;
  height?: number;
  duration?: number;
}

export interface GenerationManifest {
  schemaVersion: 1;
  runId: string;
  items: BrollProvenanceItem[];
  attempts?: BrollGenerationAttempt[];
}

export interface VideoProbe {
  width: number;
  height: number;
  duration: number;
}

export interface CreateTaskRequest {
  key: string;
  prompt: string;
  model: string;
  providerText: string;
}

export interface GenerationDependencies {
  createTask(request: CreateTaskRequest): Promise<string>;
  pollTask(taskId: string): Promise<string>;
  download(videoUrl: string, destination: string): Promise<void>;
  probe(file: string): Promise<VideoProbe>;
  sha256(file: string): Promise<string>;
}

export interface GenerationProvenanceStore {
  reserve(manifest: GenerationManifest): Promise<void>;
  persist(manifest: GenerationManifest): Promise<void>;
}

export interface GenerationOptions {
  runId: string;
  outputDir: string;
  model: string;
  dependencies: GenerationDependencies;
  provenanceStore?: GenerationProvenanceStore;
  createdAt?: () => string;
}

const REQUIRED_REQUEST_COUNT = 3;
export const TARGET_DURATION_SECONDS = 5;
export const DURATION_TOLERANCE_SECONDS = 0.25;
const PROVIDER_FLAGS = "--ratio 9:16 --resolution 1080p --duration 5 --watermark false";

export function formatProviderText(prompt: string): string {
  return `${prompt.trim()} ${PROVIDER_FLAGS}`;
}

function validateRequests(requests: BrollRequest[]): void {
  if (requests.length !== REQUIRED_REQUEST_COUNT) {
    throw new Error(`Generation requires exactly ${REQUIRED_REQUEST_COUNT} requests; received ${requests.length}`);
  }
  const keys = new Set<string>();
  for (const request of requests) {
    if (!request.key.trim() || !request.prompt.trim()) {
      throw new Error("Each generation request needs a non-empty key and prompt");
    }
    if (keys.has(request.key)) throw new Error("Generation request keys must be unique");
    keys.add(request.key);
  }
}

function validateRunId(runId: string): void {
  if (!/^[A-Za-z0-9_-]+$/.test(runId)) {
    throw new Error("runId may contain only letters, numbers, underscores, and hyphens");
  }
}

function slug(value: string): string {
  const cleaned = value
    .trim()
    .replace(/[^A-Za-z0-9\u4e00-\u9fff_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  if (!cleaned) throw new Error("Generation request key cannot produce an output filename");
  return cleaned;
}

function outputFileName(runId: string, position: number, key: string): string {
  return `seed_${runId}_${String(position).padStart(2, "0")}_${slug(key)}.mp4`;
}

function assertExpectedVideo(probe: VideoProbe, file: string): void {
  if (probe.width !== 1080 || probe.height !== 1920 || Math.abs(probe.duration - TARGET_DURATION_SECONDS) > DURATION_TOLERANCE_SECONDS) {
    throw new Error(`Generated video does not meet 9:16 1080p 5s contract: ${file}`);
  }
}

function failureSummary(stage: GenerationAttemptStage): string {
  return `Generation ${stage} failed`;
}

export async function runGenerationBatch(
  requests: BrollRequest[],
  options: GenerationOptions,
): Promise<GenerationManifest> {
  validateRequests(requests);
  validateRunId(options.runId);
  if (!options.model.trim()) throw new Error("Generation model is required");

  const outputDir = path.resolve(options.outputDir);
  fs.mkdirSync(outputDir, { recursive: true });
  const plannedFiles = requests.map((request, index) => outputFileName(options.runId, index + 1, request.key));
  for (const file of plannedFiles) {
    if (fs.existsSync(path.join(outputDir, file))) {
      throw new Error(`Refusing to overwrite existing generated video: ${file}`);
    }
  }

  const manifest: GenerationManifest = { schemaVersion: 1, runId: options.runId, items: [] };
  await options.provenanceStore?.reserve(manifest);

  for (let index = 0; index < requests.length; index += 1) {
    const request = requests[index];
    const file = plannedFiles[index];
    const destination = path.join(outputDir, file);
    const taskId = await options.dependencies.createTask({
      key: request.key,
      prompt: request.prompt,
      model: options.model,
      providerText: formatProviderText(request.prompt),
    });
    const createdAt = (options.createdAt || (() => new Date().toISOString()))();
    const attempt: BrollGenerationAttempt = {
      key: request.key, model: options.model, taskId, file, createdAt, stage: "created", status: "pending",
    };
    (manifest.attempts ??= []).push(attempt);
    await options.provenanceStore?.persist(manifest);

    let stage: GenerationAttemptStage = "poll";
    let acceptedItem: BrollProvenanceItem | undefined;
    try {
      const videoUrl = await options.dependencies.pollTask(taskId);
      stage = "download";
      await options.dependencies.download(videoUrl, destination);
      stage = "probe";
      const probe = await options.dependencies.probe(destination);
      attempt.width = probe.width;
      attempt.height = probe.height;
      attempt.duration = probe.duration;
      assertExpectedVideo(probe, file);
      stage = "hash";
      const sha256 = await options.dependencies.sha256(destination);
      if (!sha256) throw new Error(`Generated video hash is empty: ${file}`);
      acceptedItem = { key: request.key, prompt: request.prompt, model: options.model, taskId, file, createdAt, sha256 };
    } catch (error) {
      attempt.stage = stage;
      attempt.status = "failed";
      attempt.error = failureSummary(stage);
      await options.provenanceStore?.persist(manifest);
      throw error;
    }

    if (!acceptedItem) throw new Error(`Generated video acceptance state is missing: ${file}`);
    stage = "provenance";
    attempt.stage = "accepted";
    attempt.status = "accepted";
    manifest.items.push(acceptedItem);
    try {
      await options.provenanceStore?.persist(manifest);
    } catch (error) {
      manifest.items.pop();
      attempt.stage = stage;
      attempt.status = "failed";
      attempt.error = failureSummary(stage);
      await options.provenanceStore?.persist(manifest);
      throw error;
    }
  }

  return manifest;
}