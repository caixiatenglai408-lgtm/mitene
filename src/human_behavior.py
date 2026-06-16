"""手動操作に近づけるランダム待機・入力・クリック."""

from __future__ import annotations

import logging
import random
import time

from playwright.sync_api import Locator, Page

logger = logging.getLogger(__name__)


class HumanBehavior:
    def __init__(self, cfg: dict | None = None) -> None:
        c = cfg or {}
        self.enabled = bool(c.get("enabled", True))
        self.fast_send = bool(c.get("fast_send", False))
        self.typing_delay_ms = tuple(c.get("typing_delay_ms", [70, 200]))
        self.action_delay_ms = tuple(c.get("action_delay_ms", [500, 1400]))
        self.between_members_ms = tuple(c.get("between_members_ms", [9000, 32000]))
        self.between_accounts_ms = tuple(c.get("between_accounts_ms", [180000, 600000]))
        self.tab_switch_delay_ms = tuple(c.get("tab_switch_delay_ms", [1800, 4500]))
        self.after_login_delay_ms = tuple(c.get("after_login_delay_ms", [2500, 6000]))
        self.after_send_delay_ms = tuple(c.get("after_send_delay_ms", [2000, 5500]))
        self.scroll_probability = float(c.get("scroll_probability", 0.45))
        self.idle_pause_probability = float(c.get("idle_pause_probability", 0.25))
        self.idle_pause_ms = tuple(c.get("idle_pause_ms", [2500, 9000]))
        self.shuffle_member_order = bool(c.get("shuffle_member_order", True))
        self.shuffle_tab_order = bool(c.get("shuffle_tab_order", False))
        self.shuffle_account_order = bool(c.get("shuffle_account_order", True))

    def pause(self, min_ms: int, max_ms: int) -> None:
        if not self.enabled:
            time.sleep(min_ms / 1000)
            return
        delay = random.randint(min_ms, max_ms) / 1000
        time.sleep(delay)

    def action_pause(self) -> None:
        self.pause(*self.action_delay_ms)

    def between_members_pause(self) -> None:
        """ミテネを送ると次を送るまでの間隔（規制対策で手動風に）."""
        ms = random.randint(*self.between_members_ms)
        if ms >= 1500:
            logger.info("次のミテネまで %.1f 秒待機", ms / 1000)
        self.pause(ms, ms)

    def between_accounts_pause(self) -> None:
        ms = random.randint(*self.between_accounts_ms)
        if ms >= 60000:
            logger.info("次の女の子まで %.1f 分待機", ms / 60000)
        else:
            logger.info("次の女の子まで %.1f 秒待機", ms / 1000)
        self.pause(ms, ms)

    def tab_switch_pause(self) -> None:
        self.pause(*self.tab_switch_delay_ms)

    def after_login_pause(self) -> None:
        self.pause(*self.after_login_delay_ms)

    def after_send_pause(self) -> None:
        if self.fast_send:
            self.pause(60, 150)
            return
        self.pause(*self.after_send_delay_ms)

    def send_click(self, page: Page, locator: Locator) -> None:
        """ミテネ送信ボタン用（fast_send 時は待ち少なめの直接クリック）."""
        if self.fast_send:
            self.pause(80, 200)
            try:
                locator.scroll_into_view_if_needed(timeout=4000)
            except Exception:
                pass
            locator.click(timeout=12000)
            return
        self.human_click(page, locator)

    def maybe_idle_pause(self) -> None:
        if not self.enabled or random.random() > self.idle_pause_probability:
            return
        self.pause(*self.idle_pause_ms)
        logger.debug("一覧を眺める待機")

    def human_type(self, locator: Locator, text: str) -> None:
        locator.click()
        self.pause(200, 600)
        if not self.enabled:
            locator.fill(text)
            return
        try:
            locator.fill("")
        except Exception:
            pass
        delay = random.randint(*self.typing_delay_ms)
        locator.press_sequentially(text, delay=delay)

    def human_click(self, page: Page, locator: Locator) -> None:
        self.action_pause()
        try:
            locator.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        self.maybe_scroll(page)
        if not self.enabled:
            locator.click()
            return
        box = locator.bounding_box()
        if box and box.get("width") and box.get("height"):
            x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
            y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            page.mouse.move(x, y, steps=random.randint(8, 20))
            self.pause(80, 280)
            page.mouse.click(x, y)
        else:
            locator.click()

    def maybe_scroll(self, page: Page) -> None:
        if not self.enabled or random.random() > self.scroll_probability:
            return
        delta = random.randint(60, 220) * random.choice([-1, 1])
        try:
            page.mouse.wheel(0, delta)
            self.pause(300, 900)
        except Exception:
            pass

    def browse_list(self, page: Page) -> None:
        """会員一覧を眺めているようなスクロール."""
        if not self.enabled:
            return
        times = random.randint(1, 3)
        for _ in range(times):
            self.maybe_scroll(page)
            self.pause(400, 1200)
