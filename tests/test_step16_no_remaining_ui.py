"""STEP 16 — UI 表示のみ: initial remaining 0（内部 status=no_remaining）を
「未完了・エラー」グループに、詳細「ミテネ残り回数が0です」で表示する.

内部 status / ok / reason / batch 挙動は一切変更しないことを合わせて確認。
REAL SEND なし・mock/unit のみ。
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import result_display as rd  # noqa: E402


def no_rem_result(name="広瀬 りり"):
    # runner.run_for_account の DailyLimitReached 経路が返す形
    return {"account_id": "X", "name": name, "ok": True, "sent": 0,
            "status": "no_remaining", "message": "ミテネ残り回数が 0 です。",
            "dry_run": False}


class Test_Classify(unittest.TestCase):
    def test_no_remaining_kind_unchanged(self):
        # classify_result の kind は "no_remaining" のまま（内部分類は不変）。
        # UI グループ分けだけ build_run_display 側で errors へ回す。
        self.assertEqual(rd.classify_result(no_rem_result()), "no_remaining")

    def test_classify_does_not_mutate_internal_status(self):
        r = no_rem_result()
        before = copy.deepcopy(r)
        rd.classify_result(r)
        self.assertEqual(r, before)
        self.assertEqual(r["status"], "no_remaining")  # 内部 status は不変
        self.assertTrue(r["ok"])                        # ok も不変

    def test_success_still_success(self):
        self.assertEqual(rd.classify_result({"status": "success", "sent": 3}), "success")

    def test_completed_still_completed(self):
        self.assertEqual(rd.classify_result({"status": "completed", "sent": 0}), "completed")

    def test_no_sendable_still_error(self):
        self.assertEqual(
            rd.classify_result({"status": "error", "reason": "NO_SENDABLE_CANDIDATES",
                                "sent": 1, "error": "x"}), "error")

    def test_max_passes_still_error(self):
        self.assertEqual(
            rd.classify_result({"status": "error", "reason": "MAX_PASSES_REACHED",
                                "sent": 0, "error": "x"}), "error")


class Test_Detail(unittest.TestCase):
    def test_no_remaining_error_detail(self):
        self.assertEqual(rd._detail_for_error(no_rem_result()), "ミテネ残り回数が0です")

    def test_generic_error_detail_unchanged(self):
        self.assertEqual(
            rd._detail_for_error({"status": "error", "error": "回線エラー"}), "回線エラー")


class Test_BuildRunDisplay(unittest.TestCase):
    def test_no_remaining_goes_to_errors_group(self):
        disp = rd.build_run_display([no_rem_result("広瀬 りり")])
        err_names = [e["name"] for e in disp["errors"]]
        comp_names = [c["name"] for c in disp["completed"]]
        self.assertIn("広瀬 りり", err_names)
        self.assertNotIn("広瀬 りり", comp_names)
        self.assertTrue(disp["has_issues"])
        detail = next(e["detail"] for e in disp["errors"] if e["name"] == "広瀬 りり")
        self.assertEqual(detail, "ミテネ残り回数が0です")

    def test_mixed_batch_grouping(self):
        results = [
            no_rem_result("広瀬 りり"),
            {"name": "西野 はな", "status": "success", "ok": True, "sent": 50},
            {"name": "A子", "status": "completed", "ok": True, "sent": 0},
            {"name": "B子", "status": "error", "reason": "NO_SENDABLE_CANDIDATES",
             "sent": 2, "error": "残りありだが対象なし"},
        ]
        disp = rd.build_run_display(results)
        err = {e["name"] for e in disp["errors"]}
        comp = {c["name"] for c in disp["completed"]}
        self.assertEqual(err, {"広瀬 りり", "B子"})
        self.assertEqual(comp, {"西野 はな", "A子"})
        self.assertTrue(disp["has_issues"])

    def test_no_remaining_result_dict_not_mutated_by_build(self):
        r = no_rem_result()
        before = copy.deepcopy(r)
        rd.build_run_display([r])
        self.assertEqual(r, before)  # 内部データ（status/ok/message）は不変


class Test_BatchAutoNext_Unchanged(unittest.TestCase):
    """no_remaining キャストがあってもバッチは次キャストへ（内部 status も不変）."""

    def test_no_remaining_cast_does_not_stop_batch(self):
        import scheduler_service as sched
        a = MagicMock(id="A", name="広瀬 りり", login_id="1", password="p", enabled=True)
        b = MagicMock(id="B", name="西野 はな", login_id="2", password="p", enabled=True)

        def fake(account, base_url, **kw):
            if account.id == "A":
                return no_rem_result("広瀬 りり") | {"account_id": "A"}
            return {"account_id": "B", "name": "西野 はな", "ok": True,
                    "status": "success", "sent": 50}

        with patch.object(sched, "run_for_account", side_effect=fake), \
             patch.object(sched, "load_settings", return_value=MagicMock(base_url="https://x")), \
             patch.object(sched, "accounts_for_manual_group", return_value=[a, b]), \
             patch.object(sched, "save_last_run_report"), \
             patch.object(sched, "HumanBehavior", return_value=MagicMock()), \
             patch("job_runner.bind_send_progress", create=True), \
             patch("job_runner.clear_send_progress", create=True), \
             patch("job_runner.wait_if_paused", create=True), \
             patch("job_runner.report_job_progress", create=True):
            out = sched.run_send_job(group="all", dry_run=False, job_id=None)

        self.assertEqual(len(out), 2)
        by_id = {r["account_id"]: r for r in out}
        # 内部 status / ok は不変（no_remaining のまま）
        self.assertEqual(by_id["A"]["status"], "no_remaining")
        self.assertTrue(by_id["A"]["ok"])
        self.assertEqual(by_id["B"]["status"], "success")
        # UI グループ分け: A は「未完了・エラー」側、B は「送信完了」側
        disp = rd.build_run_display(out)
        self.assertIn("広瀬 りり", [e["name"] for e in disp["errors"]])
        self.assertIn("西野 はな", [c["name"] for c in disp["completed"]])


class Test_JsFallbackFileContent(unittest.TestCase):
    """run_ui.js の client-side fallback も no_remaining を errors 側へ回している."""

    def test_run_ui_js_routes_no_remaining_to_errors(self):
        js = (ROOT / "web/static/run_ui.js").read_text(encoding="utf-8")
        idx = js.index('r.status === "no_remaining"')
        snippet = js[idx:idx + 200]
        self.assertIn("errors.push", snippet)
        self.assertIn("ミテネ残り回数が0です", snippet)
        self.assertNotIn("completed.push", snippet.split("else if")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
