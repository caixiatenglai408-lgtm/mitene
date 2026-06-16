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
    status.className = "run-status " + (isError ? "err" : running ? "running" : "ok");
    status.textContent = message || "";
  }

  const accountList = document.getElementById("account-list");
  if (accountList) {
    accountList.addEventListener("click", async (e) => {
      const btn = e.target.closest("button");
      if (!btn || !accountList.contains(btn)) return;

      if (btn.classList.contains("btn-edit")) {
        document.getElementById("account_id").value = btn.dataset.id;
        document.getElementById("name").value = btn.dataset.name;
        document.getElementById("login_id").value = btn.dataset.login;
        document.getElementById("enabled").checked = btn.dataset.enabled === "true";
        document.getElementById("password").value = "";
        window.scrollTo({ top: 0, behavior: "smooth" });
        return;
      }

      if (btn.classList.contains("btn-run")) {
        if (!confirm(`「${btn.dataset.name}」でミテネを送信しますか？`)) return;
        window.MiteneSync?.pause(60000);
        const ui = window.MiteneRunUI;
        const accountName = btn.dataset.name;
        ui?.clearManualResult();
        btn.disabled = true;

        const stopElapsed = ui?.createElapsedTicker((sec) => {
          ui.setRunStatus({
            running: true,
            message: `「${accountName}」送信中…`,
            elapsedSec: sec,
          });
        });

        try {
          const res = await fetch(`/api/run-account/${btn.dataset.id}`, {
            method: "POST",
          });
          const data = await res.json().catch(() => ({}));
          stopElapsed?.();

          if (!res.ok) {
            ui?.setRunStatus({
              running: false,
              isError: true,
              message: data.error || `エラー (${res.status})`,
            });
            return;
          }

          const result = data.result || {};
          const display = ui?.displayFromAccountResult(result, accountName);
          const hasIssues = display?.has_issues;

          ui?.setRunStatus({
            running: false,
            phase: "done",
            isError: hasIssues,
            message: display?.summary || result.message || "処理が終わりました",
          });
          if (display) {
            ui.renderManualResult(display, `「${accountName}」の結果`);
          }
        } catch (e) {
          stopElapsed?.();
          ui?.setRunStatus({
            running: false,
            isError: true,
            message: e.message || "通信エラー",
          });
        } finally {
          btn.disabled = false;
        }
        return;
      }

      if (btn.classList.contains("btn-toggle-auto")) {
        window.MiteneSync?.pause(6000);
        const enabled = btn.dataset.enabled !== "true";
        const action = enabled
          ? "自動送信の対象に戻します"
          : "自動送信の対象外にします（一覧には残ります）";
        if (!confirm(`「${btn.dataset.name}」を${action}\n\n・「今すぐ送信」は引き続き使えます`)) return;

        const res = await fetch(`/api/accounts/${btn.dataset.id}/enabled`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        });
        const data = await res.json();
        if (data.ok) {
          window.MiteneSync?.pull();
        } else {
          alert(data.error || "更新に失敗しました");
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

        const res = await fetch(`/api/accounts/${btn.dataset.id}`, { method: "DELETE" });
        const data = await res.json();
        if (data.ok) {
          alert(data.message);
          window.MiteneSync?.pull();
        } else {
          alert(data.error || "削除に失敗しました");
        }
      }
    });
  }
})();
