"""LOGIN BODY TIMEOUT SAFE RETRY — ユニット/モックテスト.

BODY TIMEOUT ROOT CAUSE AUDIT（_has_login_error の page.inner_text("body") が
45s Playwright TimeoutError を出す経路）に対する最小改修の検証。

REAL SEND なし・Playwright 起動なし・CityHeaven アクセスなし。
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import mitene_sender as ms  # noqa: E402

SECRET_PASSWORD = "S3cr3t!Passw0rd-DO-NOT-LOG"


def make_sender(**attrs):
    s = ms.MiteneSender.__new__(ms.MiteneSender)
    s.login_id = "39760216"
    s.password = SECRET_PASSWORD
    s.login = types.SimpleNamespace(
        id_placeholder="女の子ID or メールアドレス",
        password_placeholder="半角英数字4〜12文字",
        submit_text="ログイン",
    )
    s.human = MagicMock()
    s.screenshot_on_error = True
    s.log_dir = Path("/tmp/does-not-matter")
    s._last_goto_access_block = False
    # _attempt_login が触れるヘルパをすべてダブルにする
    s._fill_login_form = MagicMock()
    s._click_login_submit = MagicMock()
    s._pause_ms = MagicMock()
    s._wait_page_settled = MagicMock()
    s._page_debug_hint = MagicMock(return_value="URL=... snippet")
    s._save_debug_screenshot = MagicMock()
    for k, v in attrs.items():
        setattr(s, k, v)
    return s


def mk_page(url="https://spgirl.cityheaven.net/J1Login.php"):
    p = MagicMock()
    p.url = url
    return p


class Test_A_TimeoutDoesNotImmediatelyRaise(unittest.TestCase):
    """TEST A: attempt1 で _has_login_error が PlaywrightTimeoutError → 例外を投げず
    (False, msg) を返す（＝既存の再試行ループへ委ねられる）."""

    def test_returns_false_not_raise(self):
        s = make_sender()
        s._has_login_error = MagicMock(
            side_effect=ms.PlaywrightTimeoutError(
                'Page.inner_text: Timeout 45000ms exceeded.\nCall log:\n- waiting for locator("body")'
            )
        )
        s._looks_logged_in = MagicMock(return_value=False)
        page = mk_page()

        ok, err = s._attempt_login(page, attempt=1)

        self.assertFalse(ok)
        self.assertIn("タイムアウト", err)
        # 送信フォームは1回だけ（timeout をきっかけに再送信していない）
        s._fill_login_form.assert_called_once()
        s._click_login_submit.assert_called_once()


class Test_B_LoginActuallySucceededAfterTimeout(unittest.TestCase):
    """TEST B: timeout 後、_looks_logged_in=True なら再送信せずログイン成功扱い."""

    def test_looks_logged_in_true_returns_success_without_resubmit(self):
        s = make_sender()
        s._has_login_error = MagicMock(
            side_effect=ms.PlaywrightTimeoutError("Timeout 45000ms exceeded")
        )
        s._looks_logged_in = MagicMock(return_value=True)
        page = mk_page()

        ok, err = s._attempt_login(page, attempt=2)

        self.assertTrue(ok)
        self.assertEqual(err, "")
        s._fill_login_form.assert_called_once()
        s._click_login_submit.assert_called_once()
        s._looks_logged_in.assert_called_once()


class Test_C_AllAttemptsExhausted(unittest.TestCase):
    """TEST C: LOGIN_MAX_ATTEMPTS すべてで timeout/未確認 → 最終的に RuntimeError.
    かつ試行回数は LOGIN_MAX_ATTEMPTS ちょうど（retry 爆発なし）."""

    def test_final_failure_after_max_attempts(self):
        s = make_sender()
        s._check_job_control = MagicMock()
        s._is_transient_access_block = MagicMock(return_value=False)
        s._wait_access_block_cooldown = MagicMock()
        open_calls = []

        def fake_open_login_page(page):
            open_calls.append(1)
            return True

        s._open_login_page = MagicMock(side_effect=fake_open_login_page)
        s._looks_logged_in = MagicMock(return_value=False)  # 常に未ログイン判定
        s._has_login_error = MagicMock(
            side_effect=ms.PlaywrightTimeoutError("Timeout 45000ms exceeded")
        )
        page = mk_page()
        context = MagicMock()

        with self.assertRaises(RuntimeError) as ctx:
            s._ensure_logged_in(page, context)

        self.assertEqual(len(open_calls), ms.LOGIN_MAX_ATTEMPTS)
        self.assertEqual(s._has_login_error.call_count, ms.LOGIN_MAX_ATTEMPTS)
        # 最終エラーメッセージに raw Playwright traceback ではなく安全な文言が使われる
        self.assertIn("タイムアウト", str(ctx.exception))
        self.assertNotIn("Call log", str(ctx.exception))


class Test_D_NormalLoginSuccessUnaffected(unittest.TestCase):
    """TEST D: 通常ログイン成功は従来通り."""

    def test_normal_success(self):
        s = make_sender()
        s._has_login_error = MagicMock(return_value=False)
        s._looks_logged_in = MagicMock(return_value=True)
        page = mk_page()

        ok, err = s._attempt_login(page, attempt=1)

        self.assertTrue(ok)
        self.assertEqual(err, "")


class Test_E_RealCredentialMismatchUnaffected(unittest.TestCase):
    """TEST E: 実際の ID/PW 不一致（_has_login_error=True, 例外なし）は従来通り."""

    def test_credential_mismatch(self):
        s = make_sender()
        s._has_login_error = MagicMock(return_value=True)
        s._looks_logged_in = MagicMock(return_value=False)
        page = mk_page()

        ok, err = s._attempt_login(page, attempt=1)

        self.assertFalse(ok)
        self.assertIn("IDまたはパスワードが正しくありません", err)


class Test_F_OtherExceptionsNotSwallowed(unittest.TestCase):
    """TEST F: PlaywrightTimeoutError 以外は今回の retry 対象にしない（従来通り伝播）."""

    def test_generic_exception_propagates(self):
        s = make_sender()
        s._has_login_error = MagicMock(side_effect=RuntimeError("何か別の異常"))
        s._looks_logged_in = MagicMock(return_value=False)
        page = mk_page()

        with self.assertRaises(RuntimeError):
            s._attempt_login(page, attempt=1)

    def test_destroyed_context_still_handled_by_has_login_error_itself(self):
        # _has_login_error 自身の _safe_inner_text は destroyed-context だけ握りつぶす。
        # これは今回変更していない（別関数）ことの確認。
        s = make_sender()
        page = mk_page()
        page.inner_text = MagicMock(
            side_effect=Exception("Execution context was destroyed")
        )
        # _has_login_error は本物を使う（_safe_inner_text 経由）
        body = s._safe_inner_text(page)
        self.assertEqual(body, "")  # 握りつぶされて空文字


class Test_G_NoSecretsInLog(unittest.TestCase):
    """TEST G: ログに password / cookie / session 等の秘密情報が出ない."""

    def test_warning_log_excludes_secrets(self):
        s = make_sender()
        s._has_login_error = MagicMock(
            side_effect=ms.PlaywrightTimeoutError(
                'Page.inner_text: Timeout 45000ms exceeded.\nCall log:\n- waiting for locator("body")'
            )
        )
        s._looks_logged_in = MagicMock(return_value=False)
        page = mk_page()

        with patch.object(ms.logger, "warning") as mock_warning:
            s._attempt_login(page, attempt=1)

        self.assertTrue(mock_warning.called)
        logged = " ".join(
            " ".join(str(a) for a in call.args) for call in mock_warning.call_args_list
        )
        self.assertNotIn(SECRET_PASSWORD, logged)
        self.assertNotIn("PHPSESSID", logged)
        self.assertNotIn("__lt__sid", logged)
        self.assertNotIn("Cookie", logged)
        self.assertNotIn("storage_state", logged)
        # account identifier / attempt / url / operation / exception は含まれる
        self.assertIn("39760216", logged)
        self.assertIn("login_error_check", logged)

    def test_screenshot_failure_does_not_break_retry_flow(self):
        s = make_sender()
        s._has_login_error = MagicMock(
            side_effect=ms.PlaywrightTimeoutError("Timeout 45000ms exceeded")
        )
        s._looks_logged_in = MagicMock(return_value=False)
        # スクリーンショット保存そのものが失敗しても、_attempt_login は
        # 正常に (False, msg) を返す（新しいエラー原因にならない・§10）。
        s._save_debug_screenshot = MagicMock(side_effect=Exception("screenshot boom"))
        page = mk_page()

        ok, err = s._attempt_login(page, attempt=1)

        self.assertFalse(ok)
        self.assertIn("タイムアウト", err)
        s._save_debug_screenshot.assert_called_once_with(page, "login_timeout")


class Test_H_SendAccountingUntouched(unittest.TestCase):
    """TEST H: login retry 変更が ①〜⑤ / send accounting 側に副作用を及ぼさない."""

    def test_unrelated_pipeline_methods_still_present_and_unmodified_signature(self):
        # _attempt_login のシグネチャ変更以外、送信エンジン側の主要メソッドが
        # そのまま存在すること（誤って削除/リネームしていないことの軽い保証）。
        for name in (
            "_execute_phased_send_loop",
            "_execute_phased_send_pipeline",
            "_finalize_phased_run",
            "_send_one_mitene",
            "_apply_step_member_filter",
            "_read_send_budget",
        ):
            self.assertTrue(hasattr(ms.MiteneSender, name), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
