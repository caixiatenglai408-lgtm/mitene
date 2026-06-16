/**
 * 管理画面の定期同期（数分おき・タブ復帰時）
 * 別タブや別端末での変更を反映し、古い表示のまま再登録するのを防ぐ
 */
(function () {
  const INTERVAL_MS = 3 * 60 * 1000;
  const MIN_VISIBLE_GAP_MS = 45 * 1000;

  let lastSyncAt = 0;
  let syncing = false;
  let userQuietUntil = 0;

  function escapeHtml(text) {
    const d = document.createElement("div");
    d.textContent = text;
    return d.innerHTML;
  }

  function pause(ms = 5000) {
    userQuietUntil = Date.now() + ms;
  }

  function shouldSkipSync() {
    if (Date.now() < userQuietUntil) return true;

    const modal = document.getElementById("duplicate-name-modal");
    if (modal && !modal.hidden) return true;

    const runStatus = document.getElementById("run-status");
    if (runStatus && !runStatus.hidden) return true;

    const form = document.getElementById("form-account-save");
    if (form) {
      const active = document.activeElement;
      if (active && form.contains(active)) return true;
    }
    return false;
  }

  function setSyncIndicator(timeLabel) {
    const el = document.getElementById("sync-indicator");
    if (!el) return;
    el.textContent = `同期 ${timeLabel}`;
    el.classList.add("sync-indicator--flash");
    window.setTimeout(() => el.classList.remove("sync-indicator--flash"), 2000);
  }

  function applyIndexPage(data) {
    const badge = document.getElementById("automation-badge");
    const onBtn = document.getElementById("btn-automation-on");
    const offBtn = document.getElementById("btn-automation-off");
    if (badge && onBtn && offBtn) {
      if (data.automation_enabled) {
        badge.textContent = "ON";
        badge.className = "status-badge on";
        onBtn.classList.add("active");
        offBtn.classList.remove("active");
      } else {
        badge.textContent = "OFF";
        badge.className = "status-badge off";
        onBtn.classList.remove("active");
        offBtn.classList.add("active");
      }
    }

    const countEl = document.getElementById("accounts-count-hint");
    if (countEl) {
      countEl.textContent = String(data.accounts_count ?? 0);
    }
  }

  function buildAccountCardHtml(a) {
    const compact = document.body.dataset.accountListCompact === "1";
    const enabled = !!a.enabled;
    const toggleClass = enabled ? "btn-toggle-auto" : "btn-toggle-auto btn-toggle-auto-off";
    const toggleLabel = enabled ? "自動送信対象外にする" : "自動送信対象にする";
    const metaExtra = enabled ? "" : " · 自動送信対象外";
    const editBtn = compact
      ? ""
      : `<button type="button" class="btn-secondary btn-edit"
              data-id="${escapeHtml(a.id)}"
              data-name="${escapeHtml(a.name)}"
              data-login="${escapeHtml(a.login_id)}"
              data-enabled="${enabled ? "true" : "false"}">編集</button>`;
    const deleteBtn = compact
      ? ""
      : `<button type="button" class="btn-danger btn-delete-account"
              data-id="${escapeHtml(a.id)}"
              data-name="${escapeHtml(a.name)}">削除</button>`;
    return `
      <li class="account-card" data-account-id="${escapeHtml(a.id)}" data-account-name="${escapeHtml(a.name)}">
        <div class="account-card-main">
          <strong class="account-name">${escapeHtml(a.name)}</strong>
          <span class="hint account-meta">${escapeHtml(a.login_id)}${metaExtra}</span>
          <div class="actions-inline actions-main">
            ${editBtn}
            <button type="button" class="btn-primary btn-run"
              data-id="${escapeHtml(a.id)}"
              data-name="${escapeHtml(a.name)}">今すぐ送信</button>
            <button type="button" class="${toggleClass}"
              data-id="${escapeHtml(a.id)}"
              data-name="${escapeHtml(a.name)}"
              data-enabled="${enabled ? "true" : "false"}">${toggleLabel}</button>
            ${deleteBtn}
          </div>
        </div>
      </li>`;
  }

  function applyAccountsPage(data) {
    window.existingAccountsData = (data.accounts || []).map((a) => ({
      id: a.id,
      name: a.name,
    }));

    const baseUrl = document.getElementById("base_url");
    if (baseUrl && document.activeElement !== baseUrl) {
      baseUrl.value = data.base_url || "";
    }

    const enabledHint = document.getElementById("enabled-count-hint");
    const accountsHint = document.getElementById("accounts-count-hint");
    if (enabledHint || accountsHint) {
      const enabled = (data.accounts || []).filter((a) => a.enabled).length;
      const total = (data.accounts || []).length;
      if (enabledHint) enabledHint.textContent = String(enabled);
      if (accountsHint) accountsHint.textContent = String(total);
    }

    const title = document.getElementById("account-list-title");
    const list = document.getElementById("account-list");
    const empty = document.getElementById("account-list-empty");
    const section = document.getElementById("account-list-section");
    if (!section) return;

    const accounts = data.accounts || [];
    if (title) title.textContent = `登録一覧（${accounts.length}名）`;

    if (!list) return;

    if (accounts.length === 0) {
      list.innerHTML = "";
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    list.innerHTML = accounts.map(buildAccountCardHtml).join("");
    if (window.accountSearchActive && typeof window.applyAccountNameFilter === "function") {
      window.applyAccountNameFilter();
    } else if (typeof window.showAllAccountCards === "function") {
      window.showAllAccountCards();
    }
  }

  function applyPage(data) {
    if (document.getElementById("account-list-section")) {
      applyAccountsPage(data);
    }
    const page = document.body.dataset.page;
    if (page === "index") applyIndexPage(data);
  }

  async function pullData() {
    if (syncing || shouldSkipSync()) return;
    syncing = true;
    try {
      const res = await fetch("/api/data", { cache: "no-store" });
      const data = await res.json();
      if (!data.ok) return;
      applyPage(data);
      lastSyncAt = Date.now();
      setSyncIndicator(data.server_time || "");
    } catch (_) {
      /* 次回再試行 */
    } finally {
      syncing = false;
    }
  }

  window.MiteneSync = { pull: pullData, refresh: pullData, pause };

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    if (Date.now() - lastSyncAt < MIN_VISIBLE_GAP_MS) return;
    pullData();
  });

  window.setInterval(pullData, INTERVAL_MS);
  window.setTimeout(pullData, 8000);
})();
