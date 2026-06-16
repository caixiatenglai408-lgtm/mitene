/**
 * 手動・自動送信の結果一覧（名前・エラー表示）
 */
(function () {
  function escapeHtml(text) {
    const d = document.createElement("div");
    d.textContent = text == null ? "" : String(text);
    return d.innerHTML;
  }

  function formatRunTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString("ja-JP", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function renderRunDisplay(display, container, opts) {
    if (!container) return;
    const options = opts || {};
    if (!display) {
      container.hidden = true;
      container.innerHTML = "";
      return;
    }

    let html = "";
    if (options.title) {
      html += `<p class="run-result-title">${escapeHtml(options.title)}</p>`;
    }
    if (options.timeLabel) {
      html += `<p class="run-result-time">${escapeHtml(options.timeLabel)}</p>`;
    }

    if (display.completed && display.completed.length) {
      html += '<p class="run-result-heading">送信完了</p><ul class="run-result-list run-result-list--ok">';
      for (const item of display.completed) {
        html += "<li>";
        html += `<span class="run-result-name">${escapeHtml(item.name)}</span>`;
        if (item.detail) {
          html += `<span class="run-result-meta">${escapeHtml(item.detail)}</span>`;
        }
        html += "</li>";
      }
      html += "</ul>";
    }

    if (display.errors && display.errors.length) {
      html += '<p class="run-result-heading run-result-heading--err">未完了・エラー</p>';
      html += '<ul class="run-result-list run-result-list--err">';
      for (const item of display.errors) {
        html += "<li>";
        html += `<span class="run-result-name run-result-name--err">${escapeHtml(item.name)}</span>`;
        if (item.detail) {
          html += `<span class="run-result-err-detail">${escapeHtml(item.detail)}</span>`;
        }
        html += "</li>";
      }
      html += "</ul>";
    }

    if (!html) {
      container.hidden = true;
      container.innerHTML = "";
      return;
    }
    container.innerHTML = html;
    container.hidden = false;
  }

  window.MiteneRunResults = {
    render: renderRunDisplay,
    formatRunTime,
  };
})();
