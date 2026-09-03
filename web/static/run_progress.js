/**
 * 送信中ジョブの表示ラベル（job_type / group / progress から決定）
 */
(function () {
  const BATCH_LABELS = {
    all: "全員送信中…",
    working: "出勤送信中…",
    off: "お休み送信中…",
  };

  function isSingleJob(job) {
    if (!job) return false;
    const jobType = String(job.job_type || "").trim();
    if (jobType === "single") return true;
    return jobType !== "batch" && !!String(job.account_name || "").trim();
  }

  function runningLabelForJob(job, fallback) {
    if (!job) return fallback || "送信中…";
    if (job.dry_run) return "ドライラン実行中…";
    if (isSingleJob(job)) {
      const name =
        String(job.account_name || job.progress?.account_name || "").trim();
      return name ? `「${name}」送信中…` : fallback || "個別送信中…";
    }
    const group = String(job.group || "all").trim();
    return BATCH_LABELS[group] || fallback || BATCH_LABELS.all;
  }

  function progressMessage(job, fallback) {
    if (!job) return fallback || "送信中…";
    const progress = job.progress;
    const done = Number(progress?.send_done) || 0;
    const budget = Number(progress?.send_budget) || 0;
    if (isSingleJob(job)) {
      const name =
        String(job.account_name || progress?.account_name || "").trim();
      if (name && budget > 0) {
        return `「${name}」送信中（${done}/${budget}）`;
      }
      return runningLabelForJob(job, fallback);
    }
    const batchLabel = runningLabelForJob(job, fallback);
    if (progress?.account_name) {
      if (budget > 0) {
        return `${batchLabel.replace(/…$/, "")}：${progress.account_name}（${done}/${budget}）`;
      }
      return `${batchLabel.replace(/…$/, "")}：${progress.account_name}…`;
    }
    const msg = String(job.message || "").trim();
    if (
      msg &&
      msg !== "実行中…" &&
      !msg.startsWith("ドライラン実行中…")
    ) {
      return msg;
    }
    return batchLabel;
  }

  function runningLabelFromHeartbeat(status, fallback) {
    if (!status) return fallback || "送信中…";
    const active = status.active_job;
    if (active) {
      return runningLabelForJob(active, fallback);
    }
    if (status.job_type === "single") {
      const name = String(status.account_name || "").trim();
      return name ? `「${name}」送信中…` : fallback || "個別送信中…";
    }
    const group = String(status.group || "all").trim();
    return BATCH_LABELS[group] || fallback || BATCH_LABELS.all;
  }

  function clearAccountRowBadges() {
    document
      .querySelectorAll(".attendance-row, .account-card")
      .forEach((row) => {
        row.classList.remove("is-sending", "is-completed");
        row.querySelectorAll(
          ".sending-progress-badge, .send-complete-badge"
        ).forEach((el) => el.remove());
      });
  }

  function findAccountRow(accountId) {
    if (!accountId) return null;
    const esc = CSS.escape(String(accountId));
    return (
      document.querySelector(`.attendance-row[data-account-id="${esc}"]`) ||
      document.querySelector(`.account-card[data-account-id="${esc}"]`)
    );
  }

  function removeRowBadges(row) {
    row.classList.remove("is-sending", "is-completed");
    row.querySelectorAll(
      ".sending-progress-badge, .send-complete-badge"
    ).forEach((el) => el.remove());
  }

  function placeBadgeAfterName(row, badge) {
    const nameEl = row.querySelector(".attendance-row-name, .account-name");
    if (!nameEl) return;
    nameEl.insertAdjacentElement("afterend", badge);
  }

  function markRowCompleted(row) {
    removeRowBadges(row);
    row.classList.add("is-completed");
    const badge = document.createElement("span");
    badge.className = "send-complete-badge";
    badge.textContent = "完了";
    placeBadgeAfterName(row, badge);
  }

  function markRowSending(row, done, budget) {
    removeRowBadges(row);
    row.classList.add("is-sending");
    const badge = document.createElement("span");
    badge.className = "sending-progress-badge";
    if (budget > 0) {
      badge.textContent = `（${done}/${budget}）`;
    } else {
      badge.textContent = "送信中…";
    }
    placeBadgeAfterName(row, badge);
  }

  function applyAccountRows(job) {
    if (!job || job.status !== "running") {
      if (!job || job.status === "done") {
        clearAccountRowBadges();
      }
      return;
    }

    clearAccountRowBadges();

    const currentId = String(job.progress?.account_id || "").trim();
    const doneIds = new Set();

    for (const r of job.partial_results || []) {
      const id = String(r.account_id || "").trim();
      if (id) doneIds.add(id);
    }
    for (const id of job.completed_account_ids || []) {
      const sid = String(id || "").trim();
      if (sid) doneIds.add(sid);
    }

    for (const id of doneIds) {
      if (id === currentId) continue;
      const row = findAccountRow(id);
      if (row) markRowCompleted(row);
    }

    if (currentId) {
      const row = findAccountRow(currentId);
      if (row) {
        markRowSending(
          row,
          Number(job.progress?.send_done) || 0,
          Number(job.progress?.send_budget) || 0
        );
      }
    }
  }

  function apply(job) {
    const el = document.getElementById("run-status");
    if (!el || !job || job.status !== "running") return;
    const msg = progressMessage(job);
    if (!msg) return;
    el.hidden = false;
    el.className = "run-status running";
    el.textContent = msg;
  }

  function finish(job) {
    /* run_ui が完了表示を担当 */
  }

  function clear() {
    clearAccountRowBadges();
  }

  window.MiteneRunProgress = {
    isSingleJob,
    runningLabelForJob,
    progressMessage,
    runningLabelFromHeartbeat,
    apply,
    applyAccountRows,
    clearAccountRowBadges,
    finish,
    clear,
  };
})();
