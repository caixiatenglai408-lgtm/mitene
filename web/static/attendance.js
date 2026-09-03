/**
 * 本日出勤チェックと出勤/お休み一覧の更新
 */
(function () {
  function escapeHtml(text) {
    const d = document.createElement("div");
    d.textContent = text;
    return d.innerHTML;
  }

  function buildAttendanceCardHtml(a) {
    return `
      <li class="attendance-row" data-account-id="${escapeHtml(a.id)}" data-account-name="${escapeHtml(a.name)}">
        <div class="attendance-row-info">
          <span class="attendance-row-name">${escapeHtml(a.name)}</span>
        </div>
        <button type="button" class="btn-primary btn-run btn-run-sm"
          data-id="${escapeHtml(a.id)}"
          data-name="${escapeHtml(a.name)}">今すぐ送信</button>
      </li>`;
  }

  function applyAttendanceLists(data) {
    const working = data.working_today || [];
    const off = data.off_today || [];
    const workingIds = new Set(data.working_today_ids || []);

    const workingCount = document.getElementById("working-count-hint");
    const offCount = document.getElementById("off-count-hint");
    if (workingCount) workingCount.textContent = String(working.length);
    if (offCount) offCount.textContent = String(off.length);

    const workingList = document.getElementById("working-today-list");
    const offList = document.getElementById("off-today-list");
    const workingEmpty = document.getElementById("working-today-empty");
    const offEmpty = document.getElementById("off-today-empty");

    if (workingList) {
      workingList.innerHTML = working.map(buildAttendanceCardHtml).join("");
    }
    if (offList) {
      offList.innerHTML = off.map(buildAttendanceCardHtml).join("");
    }
    if (workingEmpty) workingEmpty.hidden = working.length > 0;
    if (offEmpty) offEmpty.hidden = off.length > 0;

    document.querySelectorAll(".attendance-checkbox").forEach((cb) => {
      const id = cb.dataset.id;
      cb.checked = workingIds.has(id);
    });

    const title = document.querySelector("#attendance-section h2");
    if (title && data.attendance_label) {
      title.textContent = `本日の出勤（${data.attendance_label}）`;
    }
  }

  async function setWorking(accountId, working) {
    window.MiteneSync?.pause(4000);
    const res = await fetch(`/api/attendance/${encodeURIComponent(accountId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ working }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `エラー (${res.status})`);
    }
    applyAttendanceLists(data);
    return data;
  }

  async function resetAttendance() {
    window.MiteneSync?.pause(4000);
    const res = await fetch("/api/attendance/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `エラー (${res.status})`);
    }
    applyAttendanceLists(data);
    return data;
  }

  function bindAttendanceReset() {
    const btn = document.getElementById("btn-attendance-reset");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      const checked = document.querySelectorAll(".attendance-checkbox:checked");
      if (checked.length === 0) {
        return;
      }
      if (!confirm("出勤チェックをすべて外しますか？")) {
        return;
      }
      btn.disabled = true;
      try {
        await resetAttendance();
      } catch (err) {
        alert(err.message || "リセットに失敗しました");
      } finally {
        btn.disabled = false;
      }
    });
  }

  function bindAttendanceCheckboxes() {
    const list = document.getElementById("account-list");
    if (!list) return;
    list.addEventListener("change", async (e) => {
      const cb = e.target.closest(".attendance-checkbox");
      if (!cb || !list.contains(cb)) return;
      const accountId = cb.dataset.id;
      const working = cb.checked;
      cb.disabled = true;
      try {
        await setWorking(accountId, working);
      } catch (err) {
        cb.checked = !working;
        alert(err.message || "出勤の更新に失敗しました");
      } finally {
        cb.disabled = false;
      }
    });
  }

  bindAttendanceCheckboxes();
  bindAttendanceReset();

  window.MiteneAttendance = {
    applyAttendanceLists,
    setWorking,
    resetAttendance,
  };
})();
