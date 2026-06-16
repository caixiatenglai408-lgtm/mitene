/**
 * 手動送信のステータス表示（経過時間・完了・エラー）
 */
(function () {
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
    ["btn-run-all", "btn-dry-all"].forEach((id) => {
      const btn = document.getElementById(id);
      if (btn) btn.disabled = disabled;
    });
    document.querySelectorAll("#account-list .btn-run").forEach((btn) => {
      btn.disabled = disabled;
    });
  }

  /**
   * @param {{ running?: boolean, phase?: string, message?: string, isError?: boolean, elapsedSec?: number }} opts
   */
  function setRunStatus(opts) {
    const status = getRunStatusEl();
    if (!status) return;

    const running = !!opts.running;
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
      message = `完了：${message}`;
    } else if (!running && isError && message) {
      message = `エラー：${message}`;
    }

    setRunButtonsDisabled(running);

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

    if (r.error || r.ok === false || r.status === "error" || r.status === "zero_send") {
      errors.push({
        name: accountName,
        detail: String(r.error || r.message || "送信できませんでした"),
      });
    } else if (r.dry_run) {
      completed.push({ name: accountName, detail: "ドライラン（送信なし）" });
    } else if ((r.sent || 0) > 0) {
      completed.push({
        name: accountName,
        detail: `${r.sent} 件送信`,
      });
    } else if (r.status === "no_remaining" || /残り回数.*0/.test(String(r.message || ""))) {
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
    clearManualResult,
    renderManualResult,
    displayFromAccountResult,
    createElapsedTicker,
  };
})();
