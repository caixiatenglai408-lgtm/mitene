/**
 * 手動送信のステータス表示（経過時間・完了・エラー）
 */
(function () {
  let activeJobId = null;

  function formatElapsed(totalSec) {
    const sec = Math.max(0, Math.floor(totalSec));
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    if (m > 0) {
      return `${m}分${String(s).padStart(2, "0")}秒`;
    }
    return `${s}秒`;
  }

  function getRunStatusEl() {
    return document.getElementById("run-status");
  }

  function getManualResultEl() {
    return document.getElementById("manual-run-result");
  }

  function setRunButtonsDisabled(disabled) {
    const runAll = document.getElementById("btn-run-all");
    if (runAll) runAll.disabled = disabled;
    document.querySelectorAll(".btn-attendance-run").forEach((btn) => {
      btn.disabled = disabled;
    });
    document
      .querySelectorAll(
        "#account-list .btn-run, #working-today-list .btn-run, #off-today-list .btn-run"
      )
      .forEach((btn) => {
        btn.disabled = disabled;
      });
  }

  function isJobRunning() {
    return !!activeJobId;
  }

  function setActiveJob(jobId) {
    activeJobId = jobId;
    setRunButtonsDisabled(true);
  }

  function clearActiveJob() {
    activeJobId = null;
    setRunButtonsDisabled(false);
  }

  function stripDonePrefix(message) {
    return String(message || "").replace(/^完了[:：]\s*/, "");
  }

  function isTerminalJob(job) {
    if (!job) return false;
    if (job.completed === true) return true;
    return job.status === "done" || job.status === "error";
  }

  function resolveJobDisplay(job, title) {
    if (job.display) return job.display;
    if (job.results?.length) {
      if (job.results.length === 1 && job.results[0].name) {
        return displayFromAccountResult(job.results[0], job.results[0].name);
      }
      if (window.MiteneRunResults?.buildDisplay) {
        return window.MiteneRunResults.buildDisplay(job.results);
      }
    }
    if (job.partial_display) return job.partial_display;
    if (job.error || job.message) {
      return {
        summary: job.error || job.message || "送信に失敗しました",
        completed: [],
        errors: [
          {
            name: title || "全体",
            detail: job.error || job.message || "送信に失敗しました",
          },
        ],
        has_issues: true,
      };
    }
    return null;
  }

  /**
   * @param {{ running?: boolean, phase?: string, message?: string, isError?: boolean, elapsedSec?: number, reconnecting?: boolean }} opts
   */
  function setRunStatus(opts) {
    const status = getRunStatusEl();
    if (!status) return;

    const running = !!opts.running || !!opts.reconnecting;
    const isError = !!opts.isError;
    const elapsed =
      typeof opts.elapsedSec === "number"
        ? formatElapsed(opts.elapsedSec)
        : "";

    let message = opts.message || "";
    if (running && elapsed) {
      message = message
        ? `${message}（${elapsed}）`
        : `送信中… ${elapsed}`;
    } else if (!running && opts.phase === "done" && message) {
      message = `完了：${stripDonePrefix(message)}`;
    } else if (!running && isError && message) {
      message = `エラー：${message}`;
    }

    if (activeJobId && running) {
      setRunButtonsDisabled(true);
    } else if (!running && !activeJobId) {
      setRunButtonsDisabled(false);
    }

    status.hidden = !message;
    status.className =
      "run-status " + (isError ? "err" : running ? "running" : "ok");
    status.textContent = message;
    status.setAttribute("role", isError ? "alert" : "status");
    status.setAttribute("aria-live", isError ? "assertive" : "polite");
  }

  function clearManualResult() {
    const el = getManualResultEl();
    if (!el) return;
    el.hidden = true;
    el.innerHTML = "";
  }

  function renderManualResult(display, title) {
    const el = getManualResultEl();
    if (!window.MiteneRunResults || !el) return;
    window.MiteneRunResults.render(display, el, {
      title: title || "実行結果",
    });
  }

  function displayFromAccountResult(result, name) {
    const r = result || {};
    const accountName = name || r.name || "（名前なし）";
    const completed = [];
    const errors = [];
    const sent = Number(r.sent || 0);

    if (r.error || r.ok === false || r.status === "error" || r.status === "zero_send") {
      errors.push({
        name: accountName,
        detail: String(r.error || r.message || "送信できませんでした"),
      });
    } else if (r.dry_run) {
      completed.push({ name: accountName, detail: "ドライラン（送信なし）" });
    } else if (sent > 0) {
      completed.push({
        name: accountName,
        detail: `${sent} 件送信`,
      });
    } else if (r.status === "budget_read_failed") {
      errors.push({
        name: accountName,
        detail: String(r.error || r.message || "ミテネ残り回数取得失敗"),
      });
    } else if (r.status === "no_remaining") {
      completed.push({ name: accountName, detail: "ミテネ残り回数なし" });
    } else {
      errors.push({
        name: accountName,
        detail: String(r.message || "送信0件"),
      });
    }

    return {
      summary: errors.length ? errors[0].detail : completed[0]?.detail || "",
      completed,
      errors,
      has_issues: errors.length > 0,
    };
  }

  function showJobResult(job, title) {
    const display = resolveJobDisplay(job, title);
    const terminal = isTerminalJob(job);
    const errorsOnly =
      !!display?.errors?.length && !(display?.completed?.length);

    setRunStatus({
      running: false,
      phase: terminal && !errorsOnly ? "done" : undefined,
      isError: !terminal || errorsOnly || job.status === "error",
      message: stripDonePrefix(
        display?.summary || job.message || job.error || "処理が終わりました"
      ),
    });

    if (display) renderManualResult(display, title);
  }

  function finalizeJob(job, title) {
    try {
      window.MiteneRunProgress?.clear?.();
      showJobResult(job, title);
    } finally {
      clearActiveJob();
    }
  }

  function createElapsedTicker(onTick) {
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      onTick(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    onTick(0);
    return function stop() {
      window.clearInterval(timer);
    };
  }

  window.MiteneRunUI = {
    formatElapsed,
    setRunStatus,
    setRunButtonsDisabled,
    clearManualResult,
    renderManualResult,
    displayFromAccountResult,
    showJobResult,
    finalizeJob,
    createElapsedTicker,
    isJobRunning,
    setActiveJob,
    clearActiveJob,
    isTerminalJob,
  };
})();
