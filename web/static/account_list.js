(function () {
  function normalizeSearchKey(value) {
    return value.trim().replace(/\u3000/g, " ").trim();
  }

  window.accountSearchActive = false;

  function showAllAccountCards() {
    const list = document.getElementById("account-list");
    if (!list) return;
    list.querySelectorAll(".account-card").forEach((card) => {
      card.hidden = false;
    });
  }

  function applyAccountNameFilter() {
    const input = document.getElementById("account-name-search");
    const result = document.getElementById("account-search-result");
    const list = document.getElementById("account-list");
    if (!input || !list) return;

    const q = normalizeSearchKey(input.value);
    const cards = list.querySelectorAll(".account-card");
    let visible = 0;
    let exactNameMatch = false;

    if (!q) {
      showAllAccountCards();
      if (result) {
        result.hidden = true;
        result.textContent = "";
      }
      window.accountSearchActive = false;
      return;
    }

    window.accountSearchActive = true;

    cards.forEach((card) => {
      const name = normalizeSearchKey(card.dataset.accountName || "");
      const login = normalizeSearchKey(
        (card.querySelector(".account-meta")?.textContent || "").split("·")[0]
      );
      const match = name.includes(q) || login.includes(q);
      card.hidden = !match;
      if (match) {
        visible += 1;
        if (name === q) exactNameMatch = true;
      }
    });

    if (!result) return;

    const label = input.value.trim();
    if (visible === 0) {
      result.hidden = false;
      result.textContent = `「${label}」は登録一覧にありません`;
      result.className = "account-search-result is-missing";
      return;
    }

    result.hidden = false;
    if (exactNameMatch) {
      result.textContent = `「${label}」は登録済みです`;
      result.className = "account-search-result is-found";
    } else {
      result.textContent = `該当 ${visible}件を表示しています`;
      result.className = "account-search-result is-partial";
    }
  }

  window.applyAccountNameFilter = applyAccountNameFilter;
  window.showAllAccountCards = showAllAccountCards;

  const accountNameSearch = document.getElementById("account-name-search");
  const btnAccountSearch = document.getElementById("btn-account-search");
  if (accountNameSearch) {
    accountNameSearch.addEventListener("input", applyAccountNameFilter);
    accountNameSearch.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        applyAccountNameFilter();
      }
    });
  }
  if (btnAccountSearch) {
    btnAccountSearch.addEventListener("click", applyAccountNameFilter);
  }

  const post = (url) => fetch(url, { method: "POST" }).then((r) => r.json());

  function updateListCounts() {
    const cards = document.querySelectorAll("#account-list .account-card");
    const total = cards.length;
    let enabled = 0;
    cards.forEach((card) => {
      const toggle = card.querySelector(".btn-toggle-auto");
      const edit = card.querySelector(".btn-edit");
      const isEnabled = (toggle || edit)?.dataset.enabled !== "false";
      if (isEnabled) enabled += 1;
    });
    const accountsHint = document.getElementById("accounts-count-hint");
    const enabledHint = document.getElementById("enabled-count-hint");
    const title = document.getElementById("account-list-title");
    if (accountsHint) accountsHint.textContent = String(total);
    if (enabledHint) enabledHint.textContent = String(enabled);
    if (title) title.textContent = `登録一覧（${total}名）`;
    const empty = document.getElementById("account-list-empty");
    if (empty) empty.hidden = total > 0;
  }

  function updateAccountEnabledUi(accountId, enabled, loginId) {
    const card = document.querySelector(
      `.account-card[data-account-id="${CSS.escape(accountId)}"]`
    );
    if (!card) return;

    const toggleBtn = card.querySelector(".btn-toggle-auto");
    const editBtn = card.querySelector(".btn-edit");
    const login = loginId || editBtn?.dataset.login || "";

    if (toggleBtn) {
      toggleBtn.dataset.enabled = enabled ? "true" : "false";
      toggleBtn.textContent = enabled
        ? "自動送信対象外にする"
        : "自動送信対象にする";
      toggleBtn.classList.toggle("btn-toggle-auto-off", !enabled);
    }
    if (editBtn) {
      editBtn.dataset.enabled = enabled ? "true" : "false";
    }

    const meta = card.querySelector(".account-meta:not(.account-meta--status)");
    if (meta && login) {
      meta.textContent = enabled ? login : `${login} · 自動送信対象外`;
    }

    const statusMeta = card.querySelector(".account-meta--status");
    if (statusMeta) {
      statusMeta.hidden = enabled;
    } else if (!enabled && card.classList.contains("account-card--register")) {
      const body = card.querySelector(".account-card-body");
      if (body && !card.querySelector(".account-meta--status")) {
        const span = document.createElement("span");
        span.className = "hint account-meta account-meta--status";
        span.textContent = "自動送信対象外";
        const nameEl = body.querySelector(".account-name");
        if (nameEl?.nextSibling) {
          body.insertBefore(span, nameEl.nextSibling);
        } else {
          body.appendChild(span);
        }
      }
    }

    updateListCounts();
  }

  function removeAccountCard(accountId) {
    const card = document.querySelector(
      `.account-card[data-account-id="${CSS.escape(accountId)}"]`
    );
    if (card) card.remove();

    window.existingAccountsData = (window.existingAccountsData || []).filter(
      (a) => a.id !== accountId
    );

    updateListCounts();
    if (typeof window.applyAccountNameFilter === "function") {
      window.applyAccountNameFilter();
    }
  }

  async function syncAccountsAfterChange() {
    if (window.MiteneSync?.pullForce) {
      await window.MiteneSync.pullForce();
      return;
    }
    try {
      const res = await fetch("/api/data", { cache: "no-store" });
      const data = await res.json();
      if (!data.ok) return;
      if (window.MiteneSync?.applyData) {
        window.MiteneSync.applyData(data);
      } else {
        window.MiteneAttendance?.applyAttendanceLists(data);
      }
    } catch (_) {
      /* 表示はローカル更新済み */
    }
  }

  function setRunStatus(running, message, isError, elapsedSec) {
    if (window.MiteneRunUI) {
      window.MiteneRunUI.setRunStatus({
        running,
        message,
        isError,
        elapsedSec,
        phase: running ? undefined : isError ? undefined : "done",
      });
      return;
    }
    const status = document.getElementById("run-status");
    if (!status) return;
    status.hidden = !message;
    status.className =
      "run-status " + (isError ? "err" : running ? "running" : "ok");
    status.textContent = message || "";
  }

  const accountList = document.getElementById("account-list");
  const runListSelector = "#account-list, #working-today-list, #off-today-list";

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".btn-run");
    if (!btn) return;
    const listRoot = btn.closest(runListSelector);
    if (!listRoot) return;

    const ui = window.MiteneRunUI;
    if (ui?.isJobRunning?.()) {
      alert("すでに実行中です。完了を待ってから再度お試しください。");
      return;
    }

    if (!confirm(`「${btn.dataset.name}」でミテネを送信しますか？`)) return;
    window.MiteneSync?.pause(60000);
    const accountName = btn.dataset.name;
    const accountId = btn.dataset.id;
    const poll = window.MiteneRunPoll;
    ui?.clearManualResult();

    const runningLabel = `「${accountName}」送信中…`;

    try {
      const res = await fetch(`/api/run-account/${accountId}`, {
        method: "POST",
      });
      const data = await res.json().catch(() => ({}));

      if (!res.ok) {
        throw new Error(data.error || `エラー (${res.status})`);
      }

      const jobId = data.job_id;
      if (!jobId) {
        throw new Error("ジョブIDを取得できませんでした");
      }

      ui?.setActiveJob?.(jobId);
      ui?.setRunStatus?.({
        running: true,
        message: runningLabel,
        elapsedSec: 0,
      });

      poll?.watchJob(jobId, {
        runningLabel,
        sendMode: "single",
        accountName,
        onProgress(job) {
          window.MiteneRunProgress?.apply(job);
          window.MiteneRunProgress?.applyAccountRows?.(job);
          const partial = job.partial_display;
          if (partial && (partial.completed?.length || partial.errors?.length)) {
            window.MiteneRunResults?.render(
              partial,
              document.getElementById("manual-run-result")
            );
          }
        },
        onStatus(opts) {
          ui?.setRunStatus({
            ...opts,
            running: true,
            message: opts.message || runningLabel,
            elapsedSec: opts.elapsedSec,
          });
        },
        onDone(job) {
          ui?.finalizeJob?.(job, `「${accountName}」の結果`);
        },
        onError(message) {
          ui?.clearActiveJob?.();
          window.MiteneRunProgress?.clear?.();
          ui?.setRunStatus({
            running: false,
            isError: true,
            message,
          });
        },
      });
    } catch (err) {
      ui?.clearActiveJob?.();
      ui?.setRunStatus({
        running: false,
        isError: true,
        message: err.message || "通信エラー",
      });
    }
  });

  if (accountList) {
    accountList.addEventListener("click", async (e) => {
      const btn = e.target.closest("button");
      if (!btn || !accountList.contains(btn)) return;

      if (btn.classList.contains("btn-edit")) {
        document.getElementById("account_id").value = btn.dataset.id;
        document.getElementById("name").value = btn.dataset.name;
        const kanaEl = document.getElementById("name_kana");
        if (kanaEl) kanaEl.value = btn.dataset.kana || "";
        document.getElementById("login_id").value = btn.dataset.login;
        document.getElementById("enabled").checked = btn.dataset.enabled === "true";
        document.getElementById("password").value = "";
        window.scrollTo({ top: 0, behavior: "smooth" });
        return;
      }

      if (btn.classList.contains("btn-toggle-auto")) {
        window.MiteneSync?.pause(6000);
        const enabled = btn.dataset.enabled !== "true";
        const action = enabled
          ? "自動送信の対象に戻します"
          : "自動送信の対象外にします（一覧には残ります）";
        if (!confirm(`「${btn.dataset.name}」を${action}\n\n・「今すぐ送信」は引き続き使えます`)) return;

        btn.disabled = true;
        try {
          const res = await fetch(`/api/accounts/${btn.dataset.id}/enabled`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled }),
          });
          const data = await res.json();
          if (data.ok) {
            updateAccountEnabledUi(
              btn.dataset.id,
              data.enabled,
              btn.dataset.login
            );
            syncAccountsAfterChange();
          } else {
            alert(data.error || "更新に失敗しました");
          }
        } finally {
          btn.disabled = false;
        }
        return;
      }

      if (btn.classList.contains("btn-delete-account")) {
        window.MiteneSync?.pause(6000);
        const name = btn.dataset.name;
        const ok = confirm(
          `「${name}」を削除しますか？\n\n・登録一覧から消えます\n・ログイン情報を削除します`
        );
        if (!ok) return;

        btn.disabled = true;
        try {
          const res = await fetch(`/api/accounts/${btn.dataset.id}`, {
            method: "DELETE",
          });
          const data = await res.json();
          if (data.ok) {
            removeAccountCard(btn.dataset.id);
            syncAccountsAfterChange();
          } else {
            alert(data.error || "削除に失敗しました");
          }
        } finally {
          btn.disabled = false;
        }
      }
    });
  }
})();
