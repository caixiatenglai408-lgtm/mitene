"""STEP 13.1 — final remaining == 0 は sent 件数に関係なく「完了」.
「ミテネ残り回数なし」は initial remaining == 0（DailyLimitReached 経路）だけ。

REAL SEND なし・mock/unit のみ。
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


def fin_sender(stop_reason, remaining_vals):
    s = ms.MiteneSender.__new__(ms.MiteneSender)
    s._multi_pass_count = 1
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
    s._execute_random_consume = MagicMock(
        side_effect=lambda page, budget, sent, sbs, rem: (sent, "no_safe_candidate")
    )
    return s


class Test_FinalizeHint_FinalZero(unittest.TestCase):
    def _hint(self, stop_reason, remaining, sent):
        s = fin_sender(stop_reason, remaining)
        s._finalize_phased_run(MagicMock(), 5, sent, {}, [])
        return s._last_run_report["status_hint"]

    def test_final_zero_sent_gt0_is_completed(self):
        self.assertEqual(self._hint("remaining_zero", 0, sent=3), "completed")

    def test_final_zero_sent_zero_is_completed_not_no_remaining(self):
        # 送信処理へ入ったあと final=0 → 完了。sent==0 でも "no_remaining" にしない。
        self.assertEqual(self._hint("no_sendable_candidates", 0, sent=0), "completed")
        self.assertEqual(self._hint("budget_reached", 0, sent=0), "completed")
        self.assertEqual(self._hint("max_passes", 0, sent=0), "completed")


class Test_RunnerMapping_Completed(unittest.TestCase):
    """run_for_account: hint=="completed" → status "completed"（"no_remaining" ではない）."""

    def _map(self, hint, sent, remaining_final=0):
        import runner
        report = {"status_hint": hint, "remaining_final": remaining_final, "note": "x"}
        sender = MagicMock()
        sender.run.return_value = sent
        sender._last_run_report = report
        sender.zero_send_message.return_value = "残り回数を使い切りました。"
        acc = MagicMock(id="A", name="Cast A", login_id="1", password="p", enabled=True)
        with patch.object(runner, "build_sender", return_value=sender):
            res = runner.run_for_account(acc, "https://x", dry_run=False)
        return res

    def test_completed_sent_zero_maps_to_completed_ok(self):
        res = self._map("completed", sent=0)
        self.assertEqual(res["status"], "completed")
        self.assertTrue(res["ok"])
        self.assertNotEqual(res["status"], "no_remaining")

    def test_completed_sent_gt0_maps_to_success(self):
        res = self._map("completed", sent=4)
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["ok"])

    def test_no_sendable_still_error_even_sent_gt0(self):
        res = self._map("no_sendable_candidates", sent=2, remaining_final=1)
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["reason"], "NO_SENDABLE_CANDIDATES")
        self.assertFalse(res["ok"])

    def test_max_passes_still_error(self):
        res = self._map("max_passes_reached", sent=0, remaining_final=3)
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["reason"], "MAX_PASSES_REACHED")


class Test_ResultDisplay_Completed(unittest.TestCase):
    def test_classify_completed(self):
        self.assertEqual(rd.classify_result({"status": "completed", "sent": 0}), "completed")
        self.assertEqual(rd.classify_result({"status": "completed", "sent": 5}), "completed")

    def test_initial_zero_still_no_remaining(self):
        self.assertEqual(
            rd.classify_result({"status": "no_remaining", "sent": 0}), "no_remaining"
        )

    def test_completed_goes_to_completed_bucket(self):
        disp = rd.build_run_display([
            {"name": "A", "status": "completed", "ok": True, "sent": 0},
            {"name": "B", "status": "error", "reason": "NO_SENDABLE_CANDIDATES",
             "sent": 1, "error": "残りありだが対象なし"},
        ])
        self.assertIn("A", [c["name"] for c in disp["completed"]])
        self.assertIn("B", [e["name"] for e in disp["errors"]])
        self.assertTrue(any("完了" in c["detail"] for c in disp["completed"] if c["name"] == "A"))
        self.assertTrue(disp["has_issues"])  # B があるので


class Test_Batch_CompletedZeroSentContinues(unittest.TestCase):
    def test_completed_zero_sent_cast_does_not_stop_batch(self):
        import scheduler_service as sched
        acc_a = MagicMock(id="A", name="Cast A", login_id="1", password="p", enabled=True)
        acc_b = MagicMock(id="B", name="Cast B", login_id="2", password="p", enabled=True)

        def fake(account, base_url, **kw):
            if account.id == "A":
                return {"account_id": "A", "name": "Cast A", "ok": True,
                        "status": "completed", "sent": 0}
            return {"account_id": "B", "name": "Cast B", "ok": True,
                    "status": "success", "sent": 2}

        with patch.object(sched, "run_for_account", side_effect=fake), \
             patch.object(sched, "load_settings", return_value=MagicMock(base_url="https://x")), \
             patch.object(sched, "accounts_for_manual_group", return_value=[acc_a, acc_b]), \
             patch.object(sched, "save_last_run_report"), \
             patch.object(sched, "HumanBehavior", return_value=MagicMock()), \
             patch("job_runner.bind_send_progress", create=True), \
             patch("job_runner.clear_send_progress", create=True), \
             patch("job_runner.wait_if_paused", create=True), \
             patch("job_runner.report_job_progress", create=True):
            out = sched.run_send_job(group="all", dry_run=False, job_id=None)

        self.assertEqual(len(out), 2)
        by_id = {r["account_id"]: r for r in out}
        self.assertEqual(by_id["A"]["status"], "completed")
        self.assertEqual(by_id["B"]["status"], "success")
        self.assertEqual(rd.classify_result(by_id["A"]), "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
