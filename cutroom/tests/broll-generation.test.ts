import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

type BrollRequest = { key: string; prompt: string };

async function loadGenerationModule(): Promise<any> {
  return import(path.join(__dirname, "..", "src", "broll-generation"));
}

function requests(): BrollRequest[] {
  return [
    { key: "urumqi-skyline", prompt: "乌鲁木齐 skyline" },
    { key: "factory-floor", prompt: "现代工厂车间" },
    { key: "mountain-road", prompt: "新疆山区公路" },
  ];
}

function tempDirectory(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "broll-generation-"));
}

test("creates exactly one task per each of three requests and records complete provenance", async () => {
  const { runGenerationBatch } = await loadGenerationModule();
  const outputDir = tempDirectory();
  const creates: Array<{ key: string; prompt: string; model: string; providerText: string }> = [];
  const downloaded: string[] = [];
  try {
    const result = await runGenerationBatch(requests(), {
      runId: "xinjiang-r4",
      outputDir,
      model: "seedance-test-model",
      createdAt: () => "2026-08-24T10:00:00.000Z",
      dependencies: {
        createTask: async (request: { key: string; prompt: string; model: string; providerText: string }) => {
          creates.push(request);
          return `task-${creates.length}`;
        },
        pollTask: async (taskId: string) => `https://example.invalid/${taskId}.mp4`,
        download: async (_url: string, destination: string) => {
          downloaded.push(destination);
          fs.writeFileSync(destination, "video");
        },
        probe: async () => ({ width: 1080, height: 1920, duration: 5 }),
        sha256: async (file: string) => `sha256:${path.basename(file)}`,
      },
    });

    assert.equal(creates.length, 3);
    assert.deepEqual(creates.map((item) => item.key), requests().map((item) => item.key));
    assert.deepEqual(downloaded.map((file) => path.basename(file)), [
      "seed_xinjiang-r4_01_urumqi-skyline.mp4",
      "seed_xinjiang-r4_02_factory-floor.mp4",
      "seed_xinjiang-r4_03_mountain-road.mp4",
    ]);
    const { attempts, ...legacyManifest } = result;
    assert.deepEqual(legacyManifest, {
      schemaVersion: 1,
      runId: "xinjiang-r4",
      items: [
        {
          key: "urumqi-skyline",
          prompt: "乌鲁木齐 skyline",
          model: "seedance-test-model",
          taskId: "task-1",
          file: "seed_xinjiang-r4_01_urumqi-skyline.mp4",
          createdAt: "2026-08-24T10:00:00.000Z",
          sha256: "sha256:seed_xinjiang-r4_01_urumqi-skyline.mp4",
        },
        {
          key: "factory-floor",
          prompt: "现代工厂车间",
          model: "seedance-test-model",
          taskId: "task-2",
          file: "seed_xinjiang-r4_02_factory-floor.mp4",
          createdAt: "2026-08-24T10:00:00.000Z",
          sha256: "sha256:seed_xinjiang-r4_02_factory-floor.mp4",
        },
        {
          key: "mountain-road",
          prompt: "新疆山区公路",
          model: "seedance-test-model",
          taskId: "task-3",
          file: "seed_xinjiang-r4_03_mountain-road.mp4",
          createdAt: "2026-08-24T10:00:00.000Z",
          sha256: "sha256:seed_xinjiang-r4_03_mountain-road.mp4",
        },
      ],
    });
    assert.deepEqual(attempts?.map(({ key, model, taskId, file, createdAt, stage, status, width, height, duration }: any) =>
      ({ key, model, taskId, file, createdAt, stage, status, width, height, duration })), [
      { key: "urumqi-skyline", model: "seedance-test-model", taskId: "task-1", file: "seed_xinjiang-r4_01_urumqi-skyline.mp4", createdAt: "2026-08-24T10:00:00.000Z", stage: "accepted", status: "accepted", width: 1080, height: 1920, duration: 5 },
      { key: "factory-floor", model: "seedance-test-model", taskId: "task-2", file: "seed_xinjiang-r4_02_factory-floor.mp4", createdAt: "2026-08-24T10:00:00.000Z", stage: "accepted", status: "accepted", width: 1080, height: 1920, duration: 5 },
      { key: "mountain-road", model: "seedance-test-model", taskId: "task-3", file: "seed_xinjiang-r4_03_mountain-road.mp4", createdAt: "2026-08-24T10:00:00.000Z", stage: "accepted", status: "accepted", width: 1080, height: 1920, duration: 5 },
    ]);
    for (const create of creates) {
      assert.match(create.providerText, /--ratio 9:16/);
      assert.match(create.providerText, /--resolution 1080p/);
      assert.match(create.providerText, /--duration 5/);
      assert.match(create.providerText, /--watermark false/);
    }
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

test("aborts immediately when the second create fails without retrying or falling back", async () => {
  const { runGenerationBatch } = await loadGenerationModule();
  const outputDir = tempDirectory();
  let creates = 0;
  try {
    await assert.rejects(
      runGenerationBatch(requests(), {
        runId: "xinjiang-r4",
        outputDir,
        model: "seedance-test-model",
        dependencies: {
          createTask: async () => {
            creates += 1;
            if (creates === 2) throw new Error("create failed");
            return "task-1";
          },
          pollTask: async () => "https://example.invalid/video.mp4",
          download: async (_url: string, destination: string) => fs.writeFileSync(destination, "video"),
          probe: async () => ({ width: 1080, height: 1920, duration: 5 }),
          sha256: async () => "sha256",
        },
      }),
      /create failed/,
    );
    assert.equal(creates, 2);
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

test("rejects request lists that do not contain exactly three unique keys", async () => {
  const { runGenerationBatch } = await loadGenerationModule();
  const outputDir = tempDirectory();
  const options = {
    runId: "xinjiang-r4",
    outputDir,
    model: "seedance-test-model",
    dependencies: {
      createTask: async () => "unused",
      pollTask: async () => "unused",
      download: async () => undefined,
      probe: async () => ({ width: 1080, height: 1920, duration: 5 }),
      sha256: async () => "unused",
    },
  };
  try {
    await assert.rejects(runGenerationBatch(requests().slice(0, 2), options), /exactly 3/);
    await assert.rejects(runGenerationBatch([...requests(), { key: "four", prompt: "four" }], options), /exactly 3/);
    await assert.rejects(runGenerationBatch([requests()[0], requests()[0], requests()[2]], options), /unique/);
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

function successfulDependencies() {
  let creates = 0;
  return {
    get creates() { return creates; },
    dependencies: {
      createTask: async () => { creates += 1; return `task-${creates}`; },
      pollTask: async (taskId: string) => `https://example.invalid/${taskId}.mp4`,
      download: async (_url: string, destination: string) => fs.writeFileSync(destination, "video"),
      probe: async () => ({ width: 1080, height: 1920, duration: 5 }),
      sha256: async (file: string) => `sha256:${path.basename(file)}`,
    },
  };
}

test("reserves the provenance manifest before the first create", async () => {
  const { runGenerationBatch } = await loadGenerationModule();
  const outputDir = tempDirectory();
  const events: string[] = [];
  const provider = successfulDependencies();
  try {
    await runGenerationBatch(requests(), {
      runId: "xinjiang-r4",
      outputDir,
      model: "seedance-test-model",
      dependencies: {
        ...provider.dependencies,
        createTask: async () => {
          assert.deepEqual(events, ["reserve:0"]);
          return provider.dependencies.createTask();
        },
      },
      provenanceStore: {
        reserve: async (manifest: { items: unknown[] }) => { events.push(`reserve:${manifest.items.length}`); },
        persist: async () => undefined,
      },
    });
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

test("persists the first complete provenance item when the second create fails", async () => {
  const { runGenerationBatch } = await loadGenerationModule();
  const outputDir = tempDirectory();
  let creates = 0;
  let stored = "";
  try {
    await assert.rejects(
      runGenerationBatch(requests(), {
        runId: "xinjiang-r4",
        outputDir,
        model: "seedance-test-model",
        createdAt: () => "2026-08-24T10:00:00.000Z",
        dependencies: {
          createTask: async () => {
            creates += 1;
            if (creates === 2) throw new Error("second create failed");
            return "task-1";
          },
          pollTask: async () => "https://example.invalid/task-1.mp4",
          download: async (_url: string, destination: string) => fs.writeFileSync(destination, "video"),
          probe: async () => ({ width: 1080, height: 1920, duration: 5 }),
          sha256: async () => "sha256-first",
        },
        provenanceStore: {
          reserve: async (manifest: unknown) => { stored = JSON.stringify(manifest); },
          persist: async (manifest: unknown) => { stored = JSON.stringify(manifest); },
        },
      }),
      /second create failed/,
    );
    assert.equal(creates, 2);
    const { attempts, ...legacyManifest } = JSON.parse(stored);
    assert.deepEqual(legacyManifest, {
      schemaVersion: 1,
      runId: "xinjiang-r4",
      items: [{
        key: "urumqi-skyline",
        prompt: "乌鲁木齐 skyline",
        model: "seedance-test-model",
        taskId: "task-1",
        file: "seed_xinjiang-r4_01_urumqi-skyline.mp4",
        createdAt: "2026-08-24T10:00:00.000Z",
        sha256: "sha256-first",
      }],
    });
    assert.deepEqual(attempts, [{
      key: "urumqi-skyline", model: "seedance-test-model", taskId: "task-1",
      file: "seed_xinjiang-r4_01_urumqi-skyline.mp4", createdAt: "2026-08-24T10:00:00.000Z",
      stage: "accepted", status: "accepted", width: 1080, height: 1920, duration: 5,
    }]);
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

test("persists provenance after every successful item", async () => {
  const { runGenerationBatch } = await loadGenerationModule();
  const outputDir = tempDirectory();
  const provider = successfulDependencies();
  const itemCounts: number[] = [];
  try {
    await runGenerationBatch(requests(), {
      runId: "xinjiang-r4",
      outputDir,
      model: "seedance-test-model",
      dependencies: provider.dependencies,
      provenanceStore: {
        reserve: async () => undefined,
        persist: async (manifest: { items: unknown[] }) => { itemCounts.push(manifest.items.length); },
      },
    });
    assert.equal(provider.creates, 3);
    assert.deepEqual(itemCounts, [0, 1, 1, 2, 2, 3]);
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

test("applies the 5 second duration tolerance inclusively", async () => {
  const { runGenerationBatch } = await loadGenerationModule();
  for (const [duration, accepted] of [[5.085011, true], [4.75, true], [5.25, true], [4.749, false], [5.251, false]] as Array<[number, boolean]>) {
    const outputDir = tempDirectory();
    const operation = runGenerationBatch(requests(), {
      runId: `duration-${String(duration).replace(".", "-")}`,
      outputDir,
      model: "seedance-test-model",
      dependencies: {
        createTask: async () => "task-1", pollTask: async () => "https://example.invalid/video.mp4",
        download: async (_url: string, destination: string) => fs.writeFileSync(destination, "video"),
        probe: async () => ({ width: 1080, height: 1920, duration }), sha256: async () => "sha256",
      },
    });
    try {
      if (accepted) await operation;
      else await assert.rejects(operation, /5s contract/);
    } finally { fs.rmSync(outputDir, { recursive: true, force: true }); }
  }
});

test("persists task ID and failed probe stage before stopping the batch", async () => {
  const { runGenerationBatch } = await loadGenerationModule();
  const outputDir = tempDirectory();
  const stored: unknown[] = [];
  let creates = 0;
  try {
    await assert.rejects(runGenerationBatch(requests(), {
      runId: "probe-failure", outputDir, model: "seedance-test-model", createdAt: () => "2026-08-24T10:00:00.000Z",
      dependencies: {
        createTask: async () => { creates += 1; return `task-${creates}`; }, pollTask: async () => "https://example.invalid/video.mp4",
        download: async (_url: string, destination: string) => fs.writeFileSync(destination, "video"),
        probe: async () => { throw new Error("ffprobe failed"); }, sha256: async () => "unused",
      },
      provenanceStore: {
        reserve: async (manifest: unknown) => { stored.push(JSON.parse(JSON.stringify(manifest))); },
        persist: async (manifest: unknown) => { stored.push(JSON.parse(JSON.stringify(manifest))); },
      },
    }), /ffprobe failed/);
    assert.equal(creates, 1);
    assert.deepEqual(stored.at(-1), {
      schemaVersion: 1, runId: "probe-failure", items: [], attempts: [{
        key: "urumqi-skyline", model: "seedance-test-model", taskId: "task-1",
        file: "seed_probe-failure_01_urumqi-skyline.mp4", createdAt: "2026-08-24T10:00:00.000Z",
        stage: "probe", status: "failed", error: "Generation probe failed",
      }],
    });
  } finally { fs.rmSync(outputDir, { recursive: true, force: true }); }
});

test("fails closed before polling when task provenance persistence fails", async () => {
  const { runGenerationBatch } = await loadGenerationModule();
  const outputDir = tempDirectory();
  let polls = 0;
  try {
    await assert.rejects(runGenerationBatch(requests(), {
      runId: "provenance-write-failure", outputDir, model: "seedance-test-model",
      dependencies: {
        createTask: async () => "task-1", pollTask: async () => { polls += 1; return "https://example.invalid/video.mp4"; },
        download: async () => undefined, probe: async () => ({ width: 1080, height: 1920, duration: 5 }), sha256: async () => "unused",
      },
      provenanceStore: { reserve: async () => undefined, persist: async () => { throw new Error("manifest write failed"); } },
    }), /manifest write failed/);
    assert.equal(polls, 0);
  } finally { fs.rmSync(outputDir, { recursive: true, force: true }); }
});

test("stores a credential-safe failure summary", async () => {
  const { runGenerationBatch } = await loadGenerationModule();
  const outputDir = tempDirectory();
  let stored: any;
  const leaked = "Authorization: Bearer secret-token; api_key=secret-value; https://provider.invalid/video.mp4";
  try {
    await assert.rejects(runGenerationBatch(requests(), {
      runId: "credential-safe-error", outputDir, model: "seedance-test-model",
      dependencies: {
        createTask: async () => "task-1", pollTask: async () => "https://example.invalid/video.mp4",
        download: async () => { throw new Error(leaked); },
        probe: async () => ({ width: 1080, height: 1920, duration: 5 }), sha256: async () => "unused",
      },
      provenanceStore: {
        reserve: async () => undefined,
        persist: async (manifest: unknown) => { stored = JSON.parse(JSON.stringify(manifest)); },
      },
    }), /secret-token/);
    const error = stored.attempts[0].error as string;
    assert.doesNotMatch(error, /secret-token|secret-value|https?:\/\//i);
    assert.equal(error, "Generation download failed");
  } finally { fs.rmSync(outputDir, { recursive: true, force: true }); }
});

test("rolls back an accepted item when final provenance persistence fails", async () => {
  const { runGenerationBatch } = await loadGenerationModule();
  const outputDir = tempDirectory();
  const stored: any[] = [];
  let persists = 0;
  try {
    await assert.rejects(runGenerationBatch(requests(), {
      runId: "final-provenance-failure", outputDir, model: "seedance-test-model", createdAt: () => "2026-08-24T10:00:00.000Z",
      dependencies: {
        createTask: async () => "task-1", pollTask: async () => "https://example.invalid/video.mp4",
        download: async (_url: string, destination: string) => fs.writeFileSync(destination, "video"),
        probe: async () => ({ width: 1080, height: 1920, duration: 5 }), sha256: async () => "sha256",
      },
      provenanceStore: {
        reserve: async () => undefined,
        persist: async (manifest: unknown) => {
          persists += 1;
          if (persists === 2) throw new Error("final manifest write failed");
          stored.push(JSON.parse(JSON.stringify(manifest)));
        },
      },
    }), /final manifest write failed/);
    assert.deepEqual(stored.at(-1), {
      schemaVersion: 1, runId: "final-provenance-failure", items: [], attempts: [{
        key: "urumqi-skyline", model: "seedance-test-model", taskId: "task-1",
        file: "seed_final-provenance-failure_01_urumqi-skyline.mp4", createdAt: "2026-08-24T10:00:00.000Z",
        stage: "provenance", status: "failed", error: "Generation provenance failed",
        width: 1080, height: 1920, duration: 5,
      }],
    });
  } finally { fs.rmSync(outputDir, { recursive: true, force: true }); }
});
test("rejects an already reserved manifest before creating a task", async () => {
  const { runGenerationBatch } = await loadGenerationModule();
  const outputDir = tempDirectory();
  const provider = successfulDependencies();
  try {
    await assert.rejects(
      runGenerationBatch(requests(), {
        runId: "xinjiang-r4",
        outputDir,
        model: "seedance-test-model",
        dependencies: provider.dependencies,
        provenanceStore: {
          reserve: async () => { throw new Error("manifest already exists"); },
          persist: async () => undefined,
        },
      }),
      /manifest already exists/,
    );
    assert.equal(provider.creates, 0);
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

test("stops before the next create when partial provenance persistence fails", async () => {
  const { runGenerationBatch } = await loadGenerationModule();
  const outputDir = tempDirectory();
  const provider = successfulDependencies();
  try {
    await assert.rejects(
      runGenerationBatch(requests(), {
        runId: "xinjiang-r4",
        outputDir,
        model: "seedance-test-model",
        dependencies: provider.dependencies,
        provenanceStore: {
          reserve: async () => undefined,
          persist: async () => { throw new Error("manifest write failed"); },
        },
      }),
      /manifest write failed/,
    );
    assert.equal(provider.creates, 1);
  } finally {
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});
test("keeps the legacy fixed Seedance seed in provider text", async () => {
  const script = await import(path.join(__dirname, "..", "scripts", "generate-broll"));
  assert.match(script.formatLegacyProviderText("legacy prompt", "repeatable-seed"), /--seed repeatable-seed/);
  assert.doesNotMatch(script.formatLegacyProviderText("legacy prompt"), /--seed/);
});