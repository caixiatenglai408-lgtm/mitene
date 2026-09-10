"""STEP 13 — account error handoff のユニット/モックテスト.

REAL SEND なし・Playwright 起動なし。
1. _finalize_phased_run が multi-pass stop_reason を status_hint / reason に反映するか
2. result_display が NO_SENDABLE_CANDIDATES / MAX_PASSES_REACHED を ERROR に分類するか
   （部分送信 sent>0 でも ERROR）
3. run_send_job が 1 キャスト ERROR でも次キャストを自動処理するか
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import mitene_sender as ms  # noqa: E402
import result_display as rd  # noqa: E402


def fin_sender(stop_reason: str, remaining_vals):
    s = ms.MiteneSender.__new__(ms.MiteneSender)
    s._multi_pass_count = 2
    s._multi_pass_stop_reason = stop_reason
    s._current_phase = ""
    s._check_account_timeout = MagicMock()
    s._note_remaining_observation = MagicMock()
    s._log_run_send_reconciliation = MagicMock()
    s._log_debug_run_summary = MagicMock()
    s._read_remaining_after_phases = MagicMock(
        side_effect=list(remaining_vals) if isinstance(remaining_vals, (list, tuple))
        else [remaining_vals, remaining_vals]
    )
    # 追加消化はしない（候補なし）
    s._execute_random_consume = MagicMock(
        side_effect=lambda page, budget, sent, sbs, rem: (sent, "no_safe_candidate")
    )
    return s


class Test_Finalize_StatusHint(unittest.TestCase):
    def _run(self, stop_reason, remaining_vals, sent=3, budget=5):
        s = fin_sender(stop_reason, remaining_vals)
        s._finalize_phased_run(MagicMock(), budget, sent, {}, [])
        return s._last_run_report

    def test_no_sendable_remaining_gt0_is_error_hint(self):
        r = self._run("no_sendable_candidates", 2, sent=3)
        self.assertEqual(r["status_hint"], "no_sendable_candidates")
        self.assertEqual(r["reason"], "NO_SENDABLE_CANDIDATES")
        self.assertEqual(r["multi_pass_stop_reason"], "no_sendable_candidates")

    def test_max_passes_remaining_gt0_is_error_hint(self):
        r = self._run("max_passes", 2, sent=4)
        self.assertEqual(r["status_hint"], "max_passes_reached")
        self.assertEqual(r["reason"], "MAX_PASSES_REACHED")

    def test_remaining_zero_always_completed(self):
        for reason in ("no_sendable_candidates", "max_passes", "remaining_zero", "budget_reached"):
            r = self._run(reason, 0, sent=3)
            self.assertEqual(r["status_hint"], "completed", reason)
            self.assertNotIn("reason", r, reason)

    def test_budget_reached_remaining_gt0_stays_completed_series(self):
        r = self._run("budget_reached", 1, sent=5, budget=5)
        self.assertEqual(r["status_hint"], "completed_with_remaining")
        self.assertNotIn("reason", r)

    def test_remaining_none_not_error_when_sent_gt0(self):
        # §10: remaining_final None だけでは NO_SENDABLE 扱いにしない
        r = self._run("no_sendable_candidates", [None, None], sent=3)
        self.assertEqual(r["status_hint"], "sent")
        self.assertNotIn("reason", r)


class Test_ResultDisplay_Classify(unittest.TestCase):
    def test_no_sendable_is_error_even_with_partial_send(self):
        r = {"name": "A", "status": "error", "reason": "NO_SENDABLE_CANDIDATES",
             "sent": 2, "error": "残り1回ぶんの対象なし"}
        self.assertEqual(rd.classify_result(r), "error")

    def test_max_passes_is_error(self):
        r = {"name": "A", "status": "error", "reason": "MAX_PASSES_REACHED",
             "sent": 0, "error": "上限巡到達"}
        self.assertEqual(rd.classify_result(r), "error")

    def test_build_run_display_puts_it_in_errors(self):
        results = [
            {"name": "A", "status": "error", "reason": "NO_SENDABLE_CANDIDATES",
             "sent": 2, "error": "残りありだが対象なし"},
            {"name": "B", "status": "success", "sent": 3},
        ]
        disp = rd.build_run_display(results)
        err_names = [e["name"] for e in disp["errors"]]
        ok_names = [c["name"] for c in disp["completed"]]
        self.assertIn("A", err_names)
        self.assertIn("B", ok_names)
        self.assertTrue(disp["has_issues"])

    def test_completed_with_remaining_still_not_error(self):
        r = {"name": "A", "status": "completed_with_remaining", "sent": 1, "remaining": 2}
        self.assertEqual(rd.classify_result(r), "completed_with_remaining")


class Test_Batch_AutoNextCast(unittest.TestCase):
    def test_error_cast_does_not_stop_batch(self):
        import scheduler_service as sched

        acc_a = MagicMock(id="A", name="Cast A", login_id="1", password="p", enabled=True)
        acc_b = MagicMock(id="B", name="Cast B", login_id="2", password="p", enabled=True)

        def fake_run_for_account(account, base_url, **kw):
            if account.id == "A":
                return {"account_id": "A", "name": "Cast A", "ok": False,
                        "status": "error", "reason": "NO_SENDABLE_CANDIDATES",
                        "sent": 2, "error": "残りありだが送信対象なし"}
            return {"account_id": "B", "name": "Cast B", "ok": True,
                    "status": "success", "sent": 3}

        settings = MagicMock(base_url="https://x")
        with patch.object(sched, "run_for_account", side_effect=fake_run_for_account), \
             patch.object(sched, "load_settings", return_value=settings), \
             patch.object(sched, "accounts_for_manual_group", return_value=[acc_a, acc_b]), \
             patch.object(sched, "save_last_run_report"), \
             patch.object(sched, "HumanBehavior", return_value=MagicMock()), \
             patch("job_runner.JobCancelled", new=type("JC", (Exception,), {})), \
             patch("job_runner.bind_send_progress", create=True), \
             patch("job_runner.clear_send_progress", create=True), \
             patch("job_runner.wait_if_paused", create=True), \
             patch("job_runner.report_job_progress", create=True):
            out = sched.run_send_job(group="all", dry_run=False, job_id=None)

        self.assertEqual(len(out), 2)
        by_id = {r["account_id"]: r for r in out}
        self.assertEqual(by_id["A"]["status"], "error")           # A は ERROR
        self.assertEqual(by_id["B"]["status"], "success")          # B は自動処理された
        self.assertEqual(rd.classify_result(by_id["A"]), "error")
        self.assertEqual(rd.classify_result(by_id["B"]), "success")


if __name__ == "__main__":
    unittest.main(verbosity=2)
