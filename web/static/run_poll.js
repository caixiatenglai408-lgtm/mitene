/**
 * 送信中のジョブ監視（スリープ復帰など一時的な通信断でエラーにしない）
 */
(function () {
  const TRANSIENT_RE =
    /failed to fetch|networkerror|load failed|network request failed|aborted|timeout|econnreset|socket/i;

  function isTransientError(err) {
    if (!err) return false;
    const msg = String(err.message || err);
    if (TRANSIENT_RE.test(msg)) return true;
    return err.name === "TypeError" && /fetch/i.test(msg);
  }

  function safeCall(fn, ...args) {
    try {
      fn?.(...args);
    } catch (e) {
      console.error("watchJob callback error", e);
    }
  }

  async function fetchJson(url, options) {
    const res = await fetch(url, { cache: "no-store", ...options });
    const data = await res.json().catch(() => ({}));
    return { res, data };
  }

  async function fetchServerBusy() {
    try {
      const { res, data } = await fetchJson("/api/client-heartbeat/status");
      if (!res.ok) return null;
      return data;
    } catch (_) {
      return null;
    }
  }

  async function pollJob(jobId) {
    const { res, data } = await fetchJson(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (!res.ok) {
      const err = new Error(data.error || `ジョブ取得失敗 (${res.status})`);
      err.status = res.status;
      throw err;
    }
    if (!data.ok) throw new Error(data.error || "ジョブ取得失敗");
    return data.job;
  }

  function emitResumePoll() {
    document.dispatchEvent(new CustomEvent("mitene:resume-poll"));
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      emitResumePoll();
    }
  });

  function resolveRunningMessage(job, options) {
    const dryRun = !!options.dryRun;
    const defaultLabel = dryRun ? "ドライラン実行中…" : "全員送信中…";
    const fallback = options.runningLabel || defaultLabel;
    let resolvedJob = job || {};
    if (!resolvedJob.job_type && options.sendMode === "single") {
      resolvedJob = {
        ...resolvedJob,
        job_type: "single",
        account_name:
          options.accountName ||
          resolvedJob.account_name ||
          resolvedJob.progress?.account_name ||
          "",
      };
    }
    if (!resolvedJob.group && options.group) {
      resolvedJob = { ...resolvedJob, group: options.group, job_type: "batch" };
    }
    return (
      window.MiteneRunProgress?.progressMessage(resolvedJob, fallback) ||
      fallback
    );
  }

  function reconnectingMessage(job, options, busyInfo) {
    const base =
      resolveRunningMessage(job, options) ||
      window.MiteneRunProgress?.runningLabelFromHeartbeat(
        busyInfo,
        options.runningLabel
      ) ||
      options.runningLabel ||
      "送信中…";
    return `${base}（通信復帰待ち）`;
  }

  /**
   * バックグラウンドジョブを監視
   * 完了判定・表示は pollJob(jobId) のみ（job.display / partial_display）
   */
  function watchJob(jobId, options) {
    options = options || {};
    const intervalMs = options.intervalMs || 2000;
    const startedAt = Date.now();
    let stopped = false;
    let timer = null;
    let inFlight = false;
    let lastJob = null;

    function stop() {
      stopped = true;
      if (timer) window.clearInterval(timer);
      timer = null;
      document.removeEventListener("mitene:resume-poll", tick);
    }

    function handleDone(job) {
      stop();
      safeCall(options.onDone, job);
    }

    async function tick() {
      if (stopped || inFlight) return;
      inFlight = true;
      const elapsed = Math.floor((Date.now() - startedAt) / 1000);

      try {
        const job = await pollJob(jobId);
        lastJob = job;
        if (stopped) return;

        if (job.status === "running") {
          safeCall(options.onProgress, job, elapsed);
          safeCall(options.onStatus, {
            running: true,
            message: resolveRunningMessage(job, options),
            elapsedSec: elapsed,
          });
          return;
        }

        handleDone(job);
      } catch (e) {
        if (stopped) return;
        const busyInfo = await fetchServerBusy();
        if (busyInfo?.busy || isTransientError(e)) {
          safeCall(options.onStatus, {
            running: true,
            reconnecting: true,
            message: reconnectingMessage(lastJob, options, busyInfo),
            elapsedSec: elapsed,
          });
          return;
        }

        stop();
        safeCall(
          options.onError,
          e.message || (e.status === 404 ? "ジョブが見つかりません" : "通信エラー")
        );
      } finally {
        inFlight = false;
      }
    }

    document.addEventListener("mitene:resume-poll", tick);
    timer = window.setInterval(tick, intervalMs);
    tick();

    return { stop };
  }

  async function recoverAfterDisconnect(options) {
    const intervalMs = options.intervalMs || 2000;
    const startedAt = Date.now();

    while (true) {
      const elapsed = Math.floor((Date.now() - startedAt) / 1000);
      options.onTick?.(elapsed);

      const busyInfo = await fetchServerBusy();
      if (busyInfo && !busyInfo.busy) {
        return { ok: false, reason: "no_job" };
      }

      await new Promise((resolve) => window.setTimeout(resolve, intervalMs));
    }
  }

  window.MiteneRunPoll = {
    isTransientError,
    fetchServerBusy,
    pollJob,
    watchJob,
    recoverAfterDisconnect,
    emitResumePoll,
  };
})();
