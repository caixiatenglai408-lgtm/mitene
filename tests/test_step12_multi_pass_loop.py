"""STEP 12 — ①〜⑤ multi-pass remaining loop のユニット/モックテスト.

REAL SEND なし・Playwright 起動なし・実ネットワークなし。
_execute_phased_send_loop の制御（budget 上限・残回数再確認・進捗なし停止・
無限ループ防止）だけを検証する。①〜⑤ pipeline 本体はモック。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import mitene_sender as ms  # noqa: E402


def make_sender():
    s = ms.MiteneSender.__new__(ms.MiteneSender)
    s._multi_pass_count = 0
    s._multi_pass_stop_reason = ""
    s._check_job_control = MagicMock()
    s._check_account_timeout = MagicMock()
    s._mark_progress = MagicMock()
    s._note_remaining_observation = MagicMock()
    # 監視: loop がこれらを呼んで同日/重複ガードを崩していないこと
    s._load_member_send_history = MagicMock()
    s._sent_member_keys = {"comeon-EXISTING"}
    s._sent_today_keys = {"comeon-EXISTING"}
    return s


def pipeline_stub(per_pass_sends):
    """per_pass_sends[i] 件だけ増やす（budget 上限は本テストでは pipeline 内相当でクランプ）。"""
    calls = {"n": 0}

    def _fn(page, budget, sent, sent_by_step, skipped_steps):
        i = calls["n"]
        calls["n"] += 1
        add = per_pass_sends[i] if i < len(per_pass_sends) else 0
        return min(budget, sent + add)

    _fn.calls = calls
    return _fn


class Test1_InitialZeroNeverLoops(unittest.TestCase):
    """TEST 1: budget=0（＝開始時残0は上流の DailyLimitReached で処理）→ loop は pipeline を呼ばない."""

    def test_budget_zero(self):
        s = make_sender()
        s._execute_phased_send_pipeline = MagicMock()
        out = s._execute_phased_send_loop(MagicMock(), budget=0, sent=0,
                                          sent_by_step={}, skipped_steps=[])
        self.assertEqual(out, 0)
        s._execute_phased_send_pipeline.assert_not_called()
        self.assertEqual(s._multi_pass_count, 0)
        self.assertEqual(s._multi_pass_stop_reason, "budget_reached")


class Test2_OnePassCompletes(unittest.TestCase):
    """TEST 2: initial=3, pass1 で 3 件 → budget 到達 → 1 巡で終了."""

    def test_single_pass(self):
        s = make_sender()
        stub = pipeline_stub([3])
        s._execute_phased_send_pipeline = MagicMock(side_effect=stub)
        s._read_remaining_after_phases = MagicMock()
        out = s._execute_phased_send_loop(MagicMock(), budget=3, sent=0,
                                          sent_by_step={}, skipped_steps=[])
        self.assertEqual(out, 3)
        self.assertEqual(stub.calls["n"], 1)
        self.assertEqual(s._multi_pass_count, 1)
        self.assertEqual(s._multi_pass_stop_reason, "budget_reached")
        s._read_remaining_after_phases.assert_not_called()  # budget 到達なら残確認不要


class Test3_TwoPassesUseUpRemaining(unittest.TestCase):
    """TEST 3: initial=5, pass1=3(残2) → pass2=2 → budget 到達. total=5 / 2 巡."""

    def test_two_passes(self):
        s = make_sender()
        stub = pipeline_stub([3, 2])
        s._execute_phased_send_pipeline = MagicMock(side_effect=stub)
        s._read_remaining_after_phases = MagicMock(return_value=2)
        out = s._execute_phased_send_loop(MagicMock(), budget=5, sent=0,
                                          sent_by_step={}, skipped_steps=[])
        self.assertEqual(out, 5)
        self.assertEqual(stub.calls["n"], 2)
        self.assertEqual(s._multi_pass_count, 2)
        self.assertEqual(s._multi_pass_stop_reason, "budget_reached")
        self.assertEqual(s._read_remaining_after_phases.call_count, 1)  # pass1 後のみ


class Test4_NoProgressStopsNoInfiniteLoop(unittest.TestCase):
    """TEST 4: initial=5, pass1=3(残2) → pass2=0(残2) → NO_SENDABLE_CANDIDATES で停止."""

    def test_no_progress(self):
        s = make_sender()
        stub = pipeline_stub([3, 0, 0, 0, 0, 0, 0, 0])
        s._execute_phased_send_pipeline = MagicMock(side_effect=stub)
        s._read_remaining_after_phases = MagicMock(return_value=2)
        out = s._execute_phased_send_loop(MagicMock(), budget=5, sent=0,
                                          sent_by_step={}, skipped_steps=[])
        self.assertEqual(out, 3)
        self.assertEqual(stub.calls["n"], 2)  # pass3 以降は回さない
        self.assertEqual(s._multi_pass_count, 2)
        self.assertEqual(s._multi_pass_stop_reason, "no_sendable_candidates")


class Test5_RemainingReadNoneKeepsGoingThenStops(unittest.TestCase):
    """TEST 5: pass1 で budget 全消化 → 残読取に行かず即終了（remaining None でも ERROR にしない）."""

    def test_full_budget_first_pass(self):
        s = make_sender()
        stub = pipeline_stub([5])
        s._execute_phased_send_pipeline = MagicMock(side_effect=stub)
        s._read_remaining_after_phases = MagicMock(return_value=None)
        out = s._execute_phased_send_loop(MagicMock(), budget=5, sent=0,
                                          sent_by_step={}, skipped_steps=[])
        self.assertEqual(out, 5)
        self.assertEqual(s._multi_pass_count, 1)
        self.assertEqual(s._multi_pass_stop_reason, "budget_reached")
        s._read_remaining_after_phases.assert_not_called()

    def test_remaining_none_but_progress_then_no_progress(self):
        # 残取得は常に None。pass1 で 2 件、pass2 で 0 件 → 進捗なしで停止（無限ループなし）。
        s = make_sender()
        stub = pipeline_stub([2, 0, 0, 0, 0, 0])
        s._execute_phased_send_pipeline = MagicMock(side_effect=stub)
        s._read_remaining_after_phases = MagicMock(return_value=None)
        out = s._execute_phased_send_loop(MagicMock(), budget=9, sent=0,
                                          sent_by_step={}, skipped_steps=[])
        self.assertEqual(out, 2)
        self.assertEqual(s._multi_pass_count, 2)
        self.assertEqual(s._multi_pass_stop_reason, "no_sendable_candidates")


class Test6_DuplicateGuardsUntouchedByLoop(unittest.TestCase):
    """TEST 6: loop は same-day / duplicate ガードの状態を触らない."""

    def test_loop_does_not_reset_guards(self):
        s = make_sender()
        stub = pipeline_stub([1, 1, 0, 0])
        s._execute_phased_send_pipeline = MagicMock(side_effect=stub)
        s._read_remaining_after_phases = MagicMock(return_value=3)
        before_today = set(s._sent_today_keys)
        before_run = set(s._sent_member_keys)
        s._execute_phased_send_loop(MagicMock(), budget=9, sent=0,
                                    sent_by_step={}, skipped_steps=[])
        s._load_member_send_history.assert_not_called()  # 巡ごとに履歴を読み直さない
        self.assertEqual(s._sent_today_keys, before_today)
        self.assertEqual(s._sent_member_keys, before_run)


class Test7_TotalNeverExceedsBudget(unittest.TestCase):
    """TEST 7: locked budget を超えて巡回を始めない / pipeline へ渡す budget は不変."""

    def test_budget_locked(self):
        s = make_sender()
        seen_budgets = []

        def _fn(page, budget, sent, sent_by_step, skipped_steps):
            seen_budgets.append(budget)
            return min(budget, sent + 2)

        s._execute_phased_send_pipeline = MagicMock(side_effect=_fn)
        s._read_remaining_after_phases = MagicMock(return_value=99)  # 残99でも budget は 4
        out = s._execute_phased_send_loop(MagicMock(), budget=4, sent=0,
                                          sent_by_step={}, skipped_steps=[])
        self.assertEqual(out, 4)                 # total_sent == budget、超えない
        self.assertTrue(all(b == 4 for b in seen_budgets))  # 残再取得で budget を上げない
        self.assertEqual(s._multi_pass_count, 2)
        self.assertEqual(s._multi_pass_stop_reason, "budget_reached")

    def test_max_passes_ceiling(self):
        # 残0にならず毎巡1件ずつ（budget 大）。MULTI_PASS_MAX で必ず止まる。
        s = make_sender()
        s._execute_phased_send_pipeline = MagicMock(
            side_effect=lambda page, budget, sent, sbs, sk: sent + 1
        )
        s._read_remaining_after_phases = MagicMock(return_value=50)
        out = s._execute_phased_send_loop(MagicMock(), budget=999, sent=0,
                                          sent_by_step={}, skipped_steps=[])
        self.assertEqual(s._multi_pass_count, ms.MiteneSender.MULTI_PASS_MAX)
        self.assertEqual(s._multi_pass_stop_reason, "max_passes")
        self.assertEqual(out, ms.MiteneSender.MULTI_PASS_MAX)


class Test8_RemainingZeroBetweenPassesStops(unittest.TestCase):
    """残回数が 0 になった時点で（budget 未達でも）巡回終了 = COMPLETED."""

    def test_remaining_zero_stop(self):
        s = make_sender()
        stub = pipeline_stub([2, 0])
        s._execute_phased_send_pipeline = MagicMock(side_effect=stub)
        s._read_remaining_after_phases = MagicMock(return_value=0)
        out = s._execute_phased_send_loop(MagicMock(), budget=9, sent=0,
                                          sent_by_step={}, skipped_steps=[])
        self.assertEqual(out, 2)
        self.assertEqual(stub.calls["n"], 1)     # 残0 → 2 巡目に入らない
        self.assertEqual(s._multi_pass_stop_reason, "remaining_zero")


if __name__ == "__main__":
    unittest.main(verbosity=2)
