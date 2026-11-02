import { reviewGate } from "./review-closure.mjs";

export function buildReviewWorkspace(candidate, watchSession, gates = {}) {
  const candidateReady = Boolean(
    (candidate?.mixSnapshotId || candidate?.mix_snapshot_id)
    && (candidate?.qaSnapshotId || candidate?.qa_snapshot_id)
    && (candidate?.qaReportId || candidate?.qa_report_id)
    && candidate?.status !== "qa_blocked",
  );
  const watchCompleted = Boolean(
    watchSession?.completed_at && watchSession?.can_approve,
  );
  return {
    candidateReady,
    watchCompleted,
    canApprove: candidateReady && reviewGate(candidate, watchSession, gates).canApprove,
    canPublish: candidateReady && Boolean(gates?.human_reviewed),
  };
}

export function reviewMediaUrl(taskId, candidate) {
  if (!taskId || !candidate) return null;
  if (candidate?.mediaUrl) return String(candidate.mediaUrl);
  const candidateId = candidate?.candidateId || candidate?.candidate_id;
  if (candidateId) {
    return "/api/tasks/" + encodeURIComponent(String(taskId))
      + "/review-media/" + encodeURIComponent(String(candidateId));
  }
  if (!buildReviewWorkspace(candidate, null).candidateReady) return null;
  return "/api/tasks/" + encodeURIComponent(String(taskId)) + "/review-media";
}

export function nextWatchReport(
  watchSession,
  currentTime,
  duration,
  lastReportedPosition,
  ended = false,
) {
  if (
    !watchSession?.session_id
    || watchSession.status !== "active"
    || !Number.isFinite(currentTime)
    || !Number.isFinite(duration)
    || duration <= 0
  ) {
    return null;
  }
  const position = Math.max(0, Math.min(Number(currentTime), Number(duration)));
  const previous = Number.isFinite(lastReportedPosition)
    ? Number(lastReportedPosition)
    : 0;
  if (!ended && position - previous < 0.75) return null;
  return {
    sessionId: String(watchSession.session_id),
    positionSeconds: position,
    completeAfterTick: Boolean(ended),
  };
}

export function createWatchReporter(watchSession, send) {
  if (typeof send !== "function") throw new TypeError("send is required");
  let lastReported = Number(watchSession?.last_position_seconds || 0);
  let pending = Promise.resolve();
  return {
    report(currentTime, duration, ended = false) {
      pending = pending.then(async () => {
        const action = nextWatchReport(
          watchSession,
          Number(currentTime),
          Number(duration),
          lastReported,
          ended,
        );
        if (action === null) return null;
        await send(action);
        lastReported = action.positionSeconds;
        return action;
      });
      return pending;
    },
  };
}

export function createPlaybackWatchController(watchSession, startSession, send) {
  if (typeof startSession !== "function") throw new TypeError("startSession is required");
  if (typeof send !== "function") throw new TypeError("send is required");
  let session = watchSession || null;
  let reporter = session?.status === "active"
    ? createWatchReporter(session, send)
    : null;
  let starting = null;

  async function ensureStarted() {
    if (session) return session;
    if (!starting) {
      starting = Promise.resolve()
        .then(startSession)
        .then((created) => {
          if (!created?.session_id || created.status !== "active") {
            throw new Error("开始完整审片后未返回有效观看会话");
          }
          session = created;
          reporter = createWatchReporter(session, send);
          return session;
        })
        .finally(() => {
          starting = null;
        });
    }
    return starting;
  }

  return {
    ensureStarted,
    requiresStart() {
      return session === null;
    },
    async report(currentTime, duration, ended = false) {
      const current = await ensureStarted();
      if (current.status !== "active" || reporter === null) return null;
      return reporter.report(currentTime, duration, ended);
    },
  };
}
