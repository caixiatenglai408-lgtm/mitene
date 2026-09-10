"""STEP 9.6 — MATCH_RECOMMEND_REFRESH のユニット/モックテスト.

本番送信は一切行わない（Playwright を起動しない・実ネットワークなし）。
既存の回帰スイートは本リポジトリに存在しないため、ここでは今回の変更に必要な
CASE のみを新規テストとして検証する。

実行:
    python -m unittest tests.test_step9_match_recommend -v
    （または pytest tests/test_step9_match_recommend.py）
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import mitene_sender as ms  # noqa: E402

MATCH_URL = "https://spgirl.cityheaven.net/J10ComeonAiMatchingList.php?gid=39760216"
DATA_KEY = "COME-ON-RECOMMEND-DATA_39760216"
SCORE_KEY = "COME-ON-RECOMMEND-SCORE_39760216"


def make_sender(**attrs):
    """__init__ を経由せず、テストに必要な属性だけ持つ MiteneSender を作る."""
    s = ms.MiteneSender.__new__(ms.MiteneSender)
    s.login_id = "39760216"
    s._match_recommend_attempted = False
    s._match_recommend_ok = False
    s._last_list_render_status = ""
    s.browser_cfg = types.SimpleNamespace(timeout_ms=45000)
    s.auth_state_path = None
    for k, v in attrs.items():
        setattr(s, k, v)
    return s


def mk_page(url=MATCH_URL, meta=None, timeout=False):
    """_refresh_match_recommend_once 用のモック page."""
    page = MagicMock()
    page.url = url
    rec: dict = {}

    def _ev(expr, arg=None):
        if "removeItem" in expr:
            rec["removed"] = arg
            return None
        if "recommend_list" in expr:  # 当日 DATA 検証プローブ
            return meta
        return None

    page.evaluate.side_effect = _ev
    cm = MagicMock()
    cm.__enter__.return_value = MagicMock()
    if timeout:
        cm.__exit__.side_effect = ms.PlaywrightTimeoutError("timeout")
    else:
        cm.__exit__.return_value = False
    page.expect_response.return_value = cm
    page._rec = rec
    return page


class Case01_StaleRemovedAndRefreshed(unittest.TestCase):
    """CASE 1: stale DATA あり → Match 開始時に当該キー削除 → refresh."""

    def test_removes_recommend_keys_and_returns_true(self):
        s = make_sender()
        s._wait_match_list_ready = MagicMock(return_value=(100, True))
        page = mk_page(meta={"n": 100, "expire": int(time.time()) + 3600})

        ok = s._refresh_match_recommend_once(page)

        self.assertTrue(ok)
        self.assertEqual(page._rec["removed"], [DATA_KEY, SCORE_KEY])
        # reload（storage 無しルート）を通すための再 goto が行われている
        page.expect_response.assert_called_once()
        page.goto.assert_called()


class Case02_StorageStateStripsRecommend(unittest.TestCase):
    """CASE 2: DATA/SCORE が storage_state にあっても保存時に除外される."""

    def test_strip_helper_keeps_everything_else(self):
        state = {
            "cookies": [{"name": "unique_id", "value": "keepme"}],
            "origins": [
                {
                    "origin": "https://spgirl.cityheaven.net",
                    "localStorage": [
                        {"name": DATA_KEY, "value": "x"},
                        {"name": SCORE_KEY, "value": "y"},
                        {"name": "unique_id", "value": "z"},
                        {"name": "__lt__cid", "value": "w"},
                    ],
                }
            ],
        }
        ms._strip_match_recommend_ls(state)
        names = [kv["name"] for kv in state["origins"][0]["localStorage"]]
        self.assertEqual(names, ["unique_id", "__lt__cid"])
        self.assertEqual(state["cookies"], [{"name": "unique_id", "value": "keepme"}])

    def test_write_storage_state_filtered_drops_only_recommend(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "auth.json"
            s = make_sender(auth_state_path=tmp)
            ctx = MagicMock()
            ctx.storage_state.return_value = {
                "cookies": [{"name": "PHPSESSID"}],
                "origins": [
                    {
                        "origin": "o",
                        "localStorage": [
                            {"name": DATA_KEY, "value": "x"},
                            {"name": SCORE_KEY, "value": "y"},
                            {"name": "keep", "value": "1"},
                        ],
                    }
                ],
            }
            s._write_storage_state_filtered(ctx)
            saved = json.loads(tmp.read_text(encoding="utf-8"))
            self.assertEqual(
                [kv["name"] for kv in saved["origins"][0]["localStorage"]], ["keep"]
            )
            self.assertEqual(saved["cookies"], [{"name": "PHPSESSID"}])
            ctx.storage_state.assert_called_once_with()  # path 直書きではなく dict 経由


class Case03_RefreshSuccessAllowsCollect(unittest.TestCase):
    """CASE 3: refresh 成功 → 当日 DATA 検証成功 → collect 可能（0 を返さない）."""

    def test_prepare_proceeds_to_ready_wait(self):
        s = make_sender()
        s._ensure_list_from_profile = MagicMock()
        s._wait_match_list_ready = MagicMock(return_value=(100, True))
        s._refresh_match_recommend_once = MagicMock(return_value=True)

        n = s._prepare_list_page_before_collect(MagicMock(), "マッチ率")

        self.assertEqual(n, 100)
        self.assertTrue(s._match_recommend_attempted)
        self.assertTrue(s._match_recommend_ok)
        s._wait_match_list_ready.assert_called_once()


class Case04_RefreshTimeoutSkipsFourFive(unittest.TestCase):
    """CASE 4: recommend timeout → ④⑤ SKIP."""

    def test_refresh_once_returns_false_on_timeout(self):
        s = make_sender()
        s._wait_match_list_ready = MagicMock(return_value=(0, True))
        page = mk_page(timeout=True)
        self.assertFalse(s._refresh_match_recommend_once(page))

    def test_prepare_returns_zero_and_marks_unavailable(self):
        s = make_sender()
        s._ensure_list_from_profile = MagicMock()
        s._wait_match_list_ready = MagicMock()
        s._refresh_match_recommend_once = MagicMock(return_value=False)

        n = s._prepare_list_page_before_collect(MagicMock(), "マッチ率")

        self.assertEqual(n, 0)
        self.assertEqual(
            s._last_list_render_status, "match_recommend_unavailable"
        )
        s._wait_match_list_ready.assert_not_called()

    def test_phase_pipeline_skips_phase5_when_refresh_failed(self):
        s = _pipeline_sender()

        def fake_stream(page, step, label, budget, sent, sbs):
            if step.tab == "マッチ率":
                s._match_recommend_attempted = True
                s._match_recommend_ok = False
            return sent

        s._stream_send_new_phase = MagicMock(side_effect=fake_stream)
        s._fetch_tab_members = MagicMock(return_value=([], {}))
        skipped: list[str] = []

        out = s._execute_phased_send_pipeline(
            MagicMock(), budget=5, sent=0, sent_by_step={}, skipped_steps=skipped
        )

        self.assertEqual(out, 0)
        s._fetch_tab_members.assert_not_called()  # ⑤ は fetch すらしない
        self.assertTrue(
            any("MATCH_TODAY_DATA_UNAVAILABLE" in x for x in skipped)
        )


class Case05_RefreshFailureNoSendEvenWithStaleDom(unittest.TestCase):
    """CASE 5: recommend failure → stale DOM が存在しても送信しない（parse しない）."""

    def test_stream_phase_bails_before_parsing_match(self):
        s = make_sender()
        s._match_recommend_attempted = True
        s._match_recommend_ok = False
        s._check_job_control = MagicMock()
        s._invalidate_list_cache = MagicMock()
        s._navigate_to_url_safe = MagicMock(return_value=True)
        s._navigate_to_step_list = MagicMock(return_value=True)
        s._prepare_list_page_before_collect = MagicMock(return_value=0)
        s._member_card_surface = MagicMock()
        s._evaluate_member_cards = MagicMock(
            side_effect=AssertionError("stale DOM を parse してはいけない")
        )
        step = ms.PriorityStep(
            tab="マッチ率",
            member_filter="new_only",
            list_path="/J10ComeonAiMatchingList.php",
        )

        out = s._stream_send_new_phase(
            MagicMock(), step, "④マッチ率（新規）", budget=5, sent=0, sent_by_step={}
        )

        self.assertEqual(out, 0)
        s._evaluate_member_cards.assert_not_called()


class Case06_RefreshOncePerRun(unittest.TestCase):
    """CASE 6: ④→⑤ で refresh は 1 回だけ."""

    def test_maybe_refresh_calls_underlying_once(self):
        s = make_sender()
        s._refresh_match_recommend_once = MagicMock(return_value=True)
        s._maybe_refresh_match_recommend(MagicMock())
        s._maybe_refresh_match_recommend(MagicMock())
        self.assertEqual(s._refresh_match_recommend_once.call_count, 1)
        self.assertTrue(s._match_recommend_attempted)

    def test_prepare_called_twice_still_one_refresh(self):
        s = make_sender()
        s._ensure_list_from_profile = MagicMock()
        s._wait_match_list_ready = MagicMock(return_value=(100, True))
        s._refresh_match_recommend_once = MagicMock(return_value=True)
        s._prepare_list_page_before_collect(MagicMock(), "マッチ率")  # ④
        s._prepare_list_page_before_collect(MagicMock(), "マッチ率")  # ⑤
        self.assertEqual(s._refresh_match_recommend_once.call_count, 1)


class Case07_NoMatchRefreshWhenBudgetDoneEarly(unittest.TestCase):
    """CASE 7: ①②③ で budget 終了 → Match refresh しない."""

    def test_pipeline_stops_before_match(self):
        s = _pipeline_sender()
        s._maybe_refresh_match_recommend = MagicMock()
        s._fetch_tab_members = MagicMock(return_value=([], {}))
        visited: list[str] = []

        def fake_stream(page, step, label, budget, sent, sbs):
            visited.append(step.tab)
            return budget  # ① で budget 到達

        s._stream_send_new_phase = MagicMock(side_effect=fake_stream)

        out = s._execute_phased_send_pipeline(
            MagicMock(), budget=5, sent=0, sent_by_step={}, skipped_steps=[]
        )

        self.assertEqual(out, 5)
        self.assertEqual(visited, ["マイガール"])  # ②③④に進まない
        s._maybe_refresh_match_recommend.assert_not_called()
        s._fetch_tab_members.assert_not_called()


class Case08_EmptyHistoryIsNew(unittest.TestCase):
    """CASE 8: history 空欄 → NEW（既存判定・不変）."""

    def test_empty_history_new(self):
        s = make_sender()
        self.assertTrue(ms.is_new_member_from_history(""))
        self.assertFalse(s._history_text_is_sent(""))
        self.assertFalse(s._history_text_is_sent("   "))


class Case09_DateSentIsHistory(unittest.TestCase):
    """CASE 9: 日付 + 送信済 → HISTORY（既存判定・不変）."""

    def test_date_sent_history(self):
        s = make_sender()
        self.assertTrue(s._history_text_is_sent("2026/09/08 送信済"))
        self.assertFalse(ms.is_new_member_from_history("2026/09/08 送信済"))


class Case10_PhantomCtaOnlyExcludedForMatch(unittest.TestCase):
    """CASE 10: Match の phantom（.kitene_question なし CTA 単体）は candidate にならない."""

    def _row(self, **over):
        base = {
            "mid": "60522934",
            "uid": "",
            "name": "x",
            "cardText": "",
            "hasSendButton": True,
            "historyText": "",
            "hasQuestionBox": False,
        }
        base.update(over)
        return base

    def test_match_phantom_excluded(self):
        s = make_sender()
        pe: dict = {}
        self.assertIsNone(
            s._card_eval_row_to_dict(self._row(), "マッチ率", pe)
        )
        self.assertEqual(pe.get("マッチ率_質問枠なし"), 1)

    def test_match_real_card_kept(self):
        s = make_sender()
        pe: dict = {}
        out = s._card_eval_row_to_dict(
            self._row(hasQuestionBox=True), "マッチ率", pe
        )
        self.assertIsNotNone(out)
        self.assertEqual(out["member_id"], "60522934")

    def test_other_tabs_parser_unchanged(self):
        s = make_sender()
        pe: dict = {}
        out = s._card_eval_row_to_dict(self._row(), "マイガール", pe)
        self.assertIsNotNone(out)  # ①②③ は除外しない
        self.assertNotIn("マッチ率_質問枠なし", pe)


class Case11_TodaySentNotNew(unittest.TestCase):
    """CASE 11: 本日ミテネ済（当日日付 + 送信済）→ NEW 扱いにしない（④から除外）."""

    def test_today_send_classified_history(self):
        s = make_sender()
        today = time.strftime("%Y/%m/%d")
        self.assertTrue(s._history_text_is_sent(f"{today} 送信済"))
        self.assertFalse(ms.is_new_member_from_history(f"{today} 送信済"))


class Case12_RemainingZeroNoNavRefreshSend(unittest.TestCase):
    """CASE 12: remaining=0（budget=0）→ Match navigation / refresh / send = 0."""

    def test_pipeline_returns_immediately(self):
        s = _pipeline_sender()
        s._maybe_refresh_match_recommend = MagicMock()
        s._stream_send_new_phase = MagicMock()
        s._fetch_tab_members = MagicMock()

        out = s._execute_phased_send_pipeline(
            MagicMock(), budget=0, sent=0, sent_by_step={}, skipped_steps=[]
        )

        self.assertEqual(out, 0)
        s._stream_send_new_phase.assert_not_called()
        s._maybe_refresh_match_recommend.assert_not_called()
        s._fetch_tab_members.assert_not_called()


def _pipeline_sender():
    s = make_sender()
    s._check_job_control = MagicMock()
    s._check_account_timeout = MagicMock()
    s._mark_progress = MagicMock()
    s._gid = MagicMock(return_value="39760216")
    s._apply_step_member_filter = MagicMock(return_value=[])
    s._members_to_keys = MagicMock(return_value=[])
    return s


if __name__ == "__main__":
    unittest.main(verbosity=2)
