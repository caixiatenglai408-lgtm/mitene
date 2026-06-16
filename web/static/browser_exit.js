/**
 * 管理画面タブの生存監視（タブを閉じるとローカルサーバーを終了）
 * 送信中はサーバー側 busy と同期し、誤終了を防ぐ
 */
(function () {
  const HEARTBEAT_MS = 2000;
  const HEARTBEAT_BUSY_MS = 800;
  const STATUS_POLL_MS = 3000;
  const STORAGE_KEY = "mitene-tab-id";

  let tabId = sessionStorage.getItem(STORAGE_KEY);
  if (!tabId) {
    tabId = crypto.randomUUID();
    sessionStorage.setItem(STORAGE_KEY, tabId);
  }

  let serverBusy = false;

  function isRunStatusActive() {
    const status = document.getElementById("run-status");
    return !!(status && !status.hidden && status.classList.contains("running"));
  }

  function isBusy() {
    return serverBusy || isRunStatusActive();
  }

  function ping() {
    fetch("/api/client-heartbeat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tab_id: tabId, busy: isBusy() }),
      keepalive: true,
    })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (data && typeof data.busy === "boolean") {
          serverBusy = data.busy;
        }
      })
      .catch(function () {});
  }

  function pollStatus() {
    fetch("/api/client-heartbeat/status", { cache: "no-store" })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (data && typeof data.busy === "boolean") {
          serverBusy = data.busy;
        }
      })
      .catch(function () {});
  }

  function leave() {
    if (isBusy()) return;
    const url =
      "/api/client-heartbeat/leave?tab_id=" + encodeURIComponent(tabId);
    if (navigator.sendBeacon) {
      navigator.sendBeacon(url);
    } else {
      fetch(url, { method: "POST", keepalive: true }).catch(function () {});
    }
  }

  ping();
  pollStatus();

  let intervalMs = HEARTBEAT_MS;
  let timer = setInterval(ping, intervalMs);

  setInterval(function () {
    const next = isBusy() ? HEARTBEAT_BUSY_MS : HEARTBEAT_MS;
    if (next === intervalMs) return;
    intervalMs = next;
    clearInterval(timer);
    timer = setInterval(ping, intervalMs);
  }, 400);

  setInterval(pollStatus, STATUS_POLL_MS);

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      ping();
      pollStatus();
    }
  });

  window.addEventListener("pagehide", function (ev) {
    if (isBusy()) return;
    clearInterval(timer);
    if (ev.persisted) return;
    leave();
  });
})();
