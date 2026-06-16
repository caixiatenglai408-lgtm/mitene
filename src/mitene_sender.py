"""姫デコ ミテネ！自動送信（ログイン → 会員へ送信）."""

from __future__ import annotations

import itertools
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse
from datetime import date, datetime
from pathlib import Path
from typing import Any

from human_behavior import HumanBehavior
from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright, sync_playwright

logger = logging.getLogger(__name__)

# 管理画面表示用（送信ルールの識別）
SEND_LOGIC_VERSION = "login-retry-v4.5"

# 会員一覧ホスト（gid=女の子ID をクエリに付与）
SPGIRL_LIST_HOST = "spgirl.cityheaven.net"
# 正規ログインURL（J1Main.php 等ではなく明示的に J1Login.php を開く）
SPGIRL_LOGIN_URL = "https://spgirl.cityheaven.net/J1Login.php"
# ログイン画面を開く前の待機（秒）— 連続アクセスによる IP ブロック防止
LOGIN_PRE_OPEN_WAIT_SEC = (3, 5)
# ログイン送信後の待機（秒）— ページが安定するまでしっかり待つ
LOGIN_POST_SUBMIT_WAIT_SEC = 5
# ログイン失敗時の再試行前待機（ミリ秒）— 通常の再試行
LOGIN_RETRY_WAIT_MS = 30000
# SSL / chrome-error 等の一時ブロック検知時の待機（ミリ秒）
LOGIN_ACCESS_BLOCK_WAIT_MS = 60000
LOGIN_MAX_ATTEMPTS = 3

# タブごとの一覧判定（URL + 見出し + active タブ）
STEP_TAB_MARKERS: dict[str, dict[str, Any]] = {
    "みたよ": {
        "slug": "comeonvisitorlist",
        "headings": ("あなたをみたよした会員", "みたよ会員"),
    },
    "マイガール": {
        "slug": "comeonmygirllist",
        "headings": ("あなたをマイガール登録している会員", "マイガール会員"),
    },
    "キープ": {
        "slug": "comeonkeeplist",
        "headings": ("キープしている会員", "キープ会員", "キープした会員"),
    },
    "マッチ率": {
        "slug": "comeonaimatchinglist",
        "headings": ("AIマッチング", "相性の良い会員", "マッチ率の高い"),
    },
}

# 全角数字・コロン、改行挟み、「20回」「残り回数 : 20 / 20」にも対応
REMAINING_PATTERN = re.compile(
    r"ミテネ残り回数\s*[：:：]?\s*([0-9０-９]+)\s*回?"
    r"|残り回数\s*[：:]\s*([0-9０-９]+)\s*/\s*[0-9０-９]+",
    re.MULTILINE,
)
# ミテネ！Pick Up 画面の横タブ（2枚目の赤枠）
PICKUP_TAB_LABELS = ("みたよ", "マイガール", "口コミ", "キープ", "マッチ率", "ミテネ履歴")
# ミテネ履歴の値に日付・送信済がある = すでに送った会員
MITENE_HISTORY_SENT_VALUE = re.compile(
    r"送信済|送付済|済み|\d{4}[/.\-年]\d{1,2}[/.\-月]?\d{0,2}"
)


def _is_destroyed_context_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "execution context was destroyed",
            "locator.count",
            "most likely because of a navigation",
            "target page",
            "has been closed",
            "context or browser",
            "frame was detached",
            "navigation",
        )
    )


def _normalize_digits(text: str) -> str:
    return text.translate(str.maketrans("０１２３４５６７８９：", "0123456789:"))


@dataclass
class LoginConfig:
    id_placeholder: str
    password_placeholder: str
    submit_text: str


# 姫デコ会員一覧の「ミテネを送る」CTA（DevTools: kitene_send_btn / registComeon）
KITENE_SEND_CTA_SELECTOR = (
    ".kitene_send_btn.active a.kitene_send_btn__text_wrapper, "
    "a.kitene_send_btn__text_wrapper[onclick*='registComeon'], "
    "a[onclick^='registComeon']"
)

# 会員カード検出（固定セレクタ + 送信ボタン/js-regist_comeon から親を辿る）
MEMBER_CARD_HELPERS_JS = """
const MEMBER_CARD_ROOT_SELECTORS = [
    'li.user_ranking_box',
    '.user_ranking_list > li',
    '.user_ranking_list li',
    'ul.user_ranking_list li',
    '.user_ranking_box',
    'li[class*="user_ranking"]',
    '[class*="user_ranking_box"]',
    '.kitene_ranking_list li',
    '.kitene_user_list li',
    'li[class*="u_"]',
    '[class*="kitene_user"]',
];
const MEMBER_CARD_ANCHOR_SELECTORS = [
    '[class*="js-regist_comeon_"]',
    '.kitene_send_btn',
    'a[onclick*="registComeon"]',
    'button[onclick*="registComeon"]',
];
const isExcludedCardContainer = (el) => {
    if (!el || !el.closest) return true;
    if (el.closest('#colorbox, .kitene_ranking ul.tab, ul.tab, .modal, [role="dialog"]')) {
        return true;
    }
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'body' || tag === 'html') return true;
    const cls = String(el.className || '');
    if (/\\bkitene_ranking\\b/.test(cls) && !/user_ranking|u_/.test(cls)) return true;
    return false;
};
const hasMemberMarker = (el) => {
    if (!el || !el.querySelector) return false;
    if (el.querySelector('[class*="js-regist_comeon_"], .kitene_send_btn, [onclick*="registComeon"]')) {
        return true;
    }
    if (el.classList) {
        for (const c of el.classList) {
            if (c.startsWith('u_') && c.length > 2) return true;
        }
    }
    return /\\bu_\\d+\\b/.test(String(el.className || ''));
};
const isLikelyMemberCard = (el) => {
    if (!el || el.nodeType !== 1 || isExcludedCardContainer(el)) return false;
    if (!hasMemberMarker(el)) return false;
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    if (t.length < 6 || t.length > 3200) return false;
    return true;
};
const collectMemberCards = () => {
    const cards = [];
    const seen = new Set();
    const add = (el) => {
        if (!el || seen.has(el) || !isLikelyMemberCard(el)) return;
        seen.add(el);
        cards.push(el);
    };
    for (const sel of MEMBER_CARD_ROOT_SELECTORS) {
        try {
            document.querySelectorAll(sel).forEach(add);
        } catch (_) {}
    }
    for (const sel of MEMBER_CARD_ANCHOR_SELECTORS) {
        let anchors = [];
        try {
            anchors = [...document.querySelectorAll(sel)];
        } catch (_) {
            continue;
        }
        for (const anchor of anchors) {
            if (anchor.closest('.kitene_send_zumi_btn')) continue;
            let node = anchor;
            for (let depth = 0; depth < 14 && node; depth++, node = node.parentElement) {
                if (!node || node === document.body) break;
                if (isLikelyMemberCard(node)) {
                    add(node);
                    break;
                }
            }
        }
    }
    return cards;
};
const memberCardSelectorDebug = () => {
    const cards = collectMemberCards();
    const anchors = document.querySelectorAll(
        '[class*="js-regist_comeon_"], .kitene_send_btn, a[onclick*="registComeon"]'
    );
    const samples = [];
    for (const a of [...anchors].slice(0, 4)) {
        let p = a;
        const chain = [];
        for (let i = 0; i < 7 && p; i++, p = p.parentElement) {
            const cls = String(p.className || '').trim().split(/\\s+/).slice(0, 4).join('.');
            chain.push((p.tagName || '').toLowerCase() + (cls ? '.' + cls : ''));
        }
        samples.push(chain.join(' > '));
    }
    const selectorHits = {};
    for (const sel of MEMBER_CARD_ROOT_SELECTORS) {
        try {
            selectorHits[sel] = document.querySelectorAll(sel).length;
        } catch (_) {
            selectorHits[sel] = -1;
        }
    }
    return {
        cardCount: cards.length,
        anchorCount: anchors.length,
        sendBtnCount: document.querySelectorAll('.kitene_send_btn').length,
        selectorHits,
        parentChains: samples,
    };
};
"""

MEMBER_CARD_COUNT_JS = (
    "() => {"
    + MEMBER_CARD_HELPERS_JS
    + "return collectMemberCards().length;}"
)

MEMBER_CARD_DEBUG_JS = (
    "() => {"
    + MEMBER_CARD_HELPERS_JS
    + "return memberCardSelectorDebug();}"
)

MEMBER_CARD_PARSE_JS = (
    "({ historyLabel }) => {"
    + MEMBER_CARD_HELPERS_JS
    + """
    const extractName = (card) => {
        for (const sel of [
            '.user_name', '.name', '.profile_name',
            '.user_ranking_name', 'a.profile_link', 'strong', 'h3', 'h4'
        ]) {
            const el = card.querySelector(sel);
            if (!el) continue;
            const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            if (t && t.length <= 48 && !/ミテネ|ブロック|マッチ/.test(t)) {
                return t;
            }
        }
        const lines = (card.innerText || '')
            .split('\\n').map(s => s.trim()).filter(Boolean);
        for (const line of lines) {
            if (line.length > 48) continue;
            if (/^\\d+歳/.test(line)) continue;
            if (/ミテネ|ブロック|マッチング|キープ|送信済/.test(line)
                && !line.endsWith('さん')) continue;
            if (line.endsWith('さん') || line.length >= 2) return line;
        }
        return lines[0] || '（名前不明）';
    };
    const extractUid = (card) => {
        for (const c of card.classList) {
            if (c.startsWith('u_') && c.length > 2) {
                return c.slice(2);
            }
        }
        const m = (card.className || '').match(/\\bu_(\\d+)\\b/);
        if (m) return m[1];
        const uidEl = card.querySelector('[class*="u_"]');
        if (uidEl && uidEl.classList) {
            for (const c of uidEl.classList) {
                if (c.startsWith('u_') && c.length > 2) return c.slice(2);
            }
        }
        return '';
    };
    const extractMid = (card) => {
        for (const el of card.querySelectorAll('[class*="js-regist_comeon_"]')) {
            for (const c of el.classList) {
                if (c.startsWith('js-regist_comeon_')) {
                    return c.replace('js-regist_comeon_', '');
                }
            }
        }
        for (const el of card.querySelectorAll(
            '[onclick*="registComeon"], .kitene_send_btn, a, button'
        )) {
            const oc = el.getAttribute('onclick') || '';
            const m = oc.match(/registComeon\\((\\d+)\\)/);
            if (m) return m[1];
        }
        const uid = extractUid(card);
        return uid || '';
    };
    const readHistory = (card) => {
        let historyText = '';
        const box = card.querySelector('.kitene_question')
            || card.querySelector('.kitene_question_box');
        const scope = box || card;
        for (const li of scope.querySelectorAll('li')) {
            const q = (li.querySelector('.question')?.innerText || '').trim();
            if (!q.includes(historyLabel)) continue;
            historyText = (
                li.querySelector('.answer.compatibility')?.innerText
                || li.querySelector('.answer')?.innerText
                || ''
            ).trim();
            break;
        }
        if (!historyText) {
            for (const row of scope.querySelectorAll('li, dl, tr, div')) {
                const t = (row.innerText || '').trim();
                if (!t.startsWith(historyLabel)) continue;
                if (t.length > historyLabel.length + 2) {
                    historyText = t.replace(historyLabel, '').trim();
                    break;
                }
            }
        }
        return historyText;
    };
    const readMatchRate = (card) => {
        const box = card.querySelector('.kitene_question')
            || card.querySelector('.kitene_question_box')
            || card;
        for (const li of box.querySelectorAll('li')) {
            const q = (li.querySelector('.question')?.innerText || '').trim();
            if (!/マッチ/.test(q)) continue;
            return (
                li.querySelector('.answer.compatibility')?.innerText
                || li.querySelector('.answer')?.innerText
                || ''
            ).trim();
        }
        const t = (card.innerText || '');
        const m = t.match(/マッチ(?:ング)?率\\s*[:：]?\\s*(\\d+\\s*%)/);
        return m ? m[1] : '';
    };
    const hasSendButton = (card) => {
        for (const el of card.querySelectorAll(
            '.kitene_send_btn, a, button, [onclick*="registComeon"]'
        )) {
            if (el.closest('.kitene_send_zumi_btn')) continue;
            const raw = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!/ミテネを送る|ミテネする|ミテネ送る/.test(raw)) continue;
            if (raw.length > 60) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 40 || r.height < 16 || !el.offsetParent) continue;
            return true;
        }
        return false;
    };
    const nodes = collectMemberCards();
    const out = [];
    const seen = new Set();
    for (const card of nodes) {
        const cardText = (card.innerText || '').trim();
        const cardHtmlHead = (card.innerHTML || '').slice(0, 500);
        const uid = extractUid(card);
        const mid = extractMid(card);
        const dedupe = mid || uid;
        if (dedupe && seen.has(dedupe)) continue;
        if (dedupe) seen.add(dedupe);
        out.push({
            name: extractName(card),
            uid,
            mid,
            cardText,
            cardHtmlHead,
            historyText: readHistory(card),
            matchRate: readMatchRate(card),
            hasSendButton: hasSendButton(card),
        });
    }
    return out;
}"""
)

# タブ名 → 会員一覧URL（gid は login_id で付与）
TAB_LIST_PATHS: dict[str, str] = {
    "マイガール": "/J10ComeonMyGirlList.php",
    "キープ": "/J10ComeonKeepList.php",
    "マッチ率": "/J10ComeonAiMatchingList.php",
    "みたよ": "/J10ComeonVisitorList.php",
}

def build_tab_list_url(gid: str, tab: str, *, host: str = SPGIRL_LIST_HOST) -> str:
    """女の子ID付きの会員一覧URL（例: gid=39760216）."""
    path = TAB_LIST_PATHS.get(tab, "")
    return build_list_url(gid, path, host=host)


def build_list_url(
    gid: str, list_path: str, *, host: str = SPGIRL_LIST_HOST
) -> str:
    """会員一覧パスと gid から完全URLを組み立てる."""
    gid = (gid or "").strip()
    if not gid or not list_path:
        return ""
    path = list_path if list_path.startswith("/") else f"/{list_path}"
    return f"https://{host}{path}?gid={gid}"


def is_new_member_from_history(history_text: str) -> bool:
    """
    div.kitene_question 内の span.question「ミテネ履歴」に紐づく
    span.answer(.compatibility) に「送信済」がなければ新規会員。
    （MEMBER_CARD_PARSE_JS の readHistory 結果を渡す）
    """
    return "送信済" not in (history_text or "")


@dataclass
class PriorityStep:
    tab: str
    sub_tab: str = ""
    condition: str = "always"  # always | if_new_exists | if_no_match_new
    member_filter: str = "sendable"  # new_only | sent_oldest_first | sendable
    list_path: str = ""
    max_members: int = 0  # 0 = 残りミテネ回数ぶん


# 送信順（config 未設定時の既定）
# ①マイガール(新規) → ②キープ(新規) → ③マッチ率(新規・残り回数) →
# ④みたよ(マッチ率新規0件時のみ) → ⑤マイガール(送信日古い順)
DEFAULT_PRIORITY_STEPS: list[PriorityStep] = [
    PriorityStep(
        tab="マイガール",
        member_filter="new_only",
        list_path="/J10ComeonMyGirlList.php",
    ),
    PriorityStep(
        tab="キープ",
        member_filter="new_only",
        list_path="/J10ComeonKeepList.php",
    ),
    PriorityStep(
        tab="マッチ率",
        member_filter="new_only",
        list_path="/J10ComeonAiMatchingList.php",
    ),
    PriorityStep(
        tab="みたよ",
        condition="if_no_match_new",
        member_filter="sendable",
        list_path="/J10ComeonVisitorList.php",
    ),
    PriorityStep(
        tab="マイガール",
        member_filter="sent_oldest_first",
        list_path="/J10ComeonMyGirlList.php",
    ),
]

# 新規0件フォールバック: 全タブの会員を合算して送信日古い順に送る
AGGREGATE_LIST_TABS: tuple[tuple[str, str], ...] = (
    ("マイガール", "/J10ComeonMyGirlList.php"),
    ("キープ", "/J10ComeonKeepList.php"),
    ("マッチ率", "/J10ComeonAiMatchingList.php"),
    ("みたよ", "/J10ComeonVisitorList.php"),
)

# 一覧URL遷移後に DOM が安定するまで待つセレクタ
LIST_PAGE_READY_SELECTORS = (
    ".kitene_ranking ul.tab",
    ".kitene_ranking",
    "ul.tab",
    "li.user_ranking_box",
    ".user_ranking_list",
)

# 遷移前 window.stop 後の待機（ミリ秒）
PRE_NAV_STOP_MS = 500
# 各 goto 後のページ切替待機（ミリ秒）
LIST_GOTO_SETTLE_MS = 2000
# 一覧URL固定成功後の追加待機（ミリ秒）
LIST_URL_FIXED_EXTRA_MS = 1000
# 一覧URL固定の最大監視時間（ミリ秒）
LIST_URL_FIX_TIMEOUT_MS = 10000
# 最終強制 goto 後の待機（ミリ秒）
FINAL_FORCED_WAIT_MS = 3000
# ログ表示用
LIST_PAGE_STABILIZE_MS = LIST_GOTO_SETTLE_MS + LIST_URL_FIXED_EXTRA_MS
# マイガールタブクリック後の待機（ミリ秒）— 直URL拒否のためキープ経由
MYGIRL_TAB_CLICK_WAIT_MS = 3000
KEEP_LIST_PATH = "/J10ComeonKeepList.php"
MYGIRL_LIST_PATH = "/J10ComeonMyGirlList.php"
# 履歴なし会員のソート用（送信日古い順で最優先グループ）
OLDEST_SORT_DEFAULT_DATE = date(1970, 1, 1)


@dataclass
class MiteneStandardConfig:
    find_members_button: str
    remaining_label: str
    mitene_history_label: str
    priority_steps: list[PriorityStep]
    max_send_per_run: int
    must_use_full_budget: bool
    max_scroll_rounds: int
    member_cooldown_days: int
    max_no_history_sends_per_day: int
    confirm_buttons: list[str]
    skip_special_banners: bool


@dataclass
class MiteneGiftConfig:
    menu_button_text: str
    image_index: int
    image_alt: str
    user_selection: str
    message: str


@dataclass
class BrowserConfig:
    headless: bool
    slow_mo_ms: int
    timeout_ms: int
    viewport_width: int
    viewport_height: int
    is_mobile: bool


class DailyLimitReached(Exception):
    """送信可能回数が残っていない."""


def _normalize_evaluate_rows(raw: Any) -> list[dict[str, Any]]:
    """page.evaluate の戻り値を会員カード dict のリストに正規化."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        if any(k in raw for k in ("cardText", "name", "mid", "uid", "key")):
            return [raw]
        for key in ("items", "members", "results", "cards", "data"):
            inner = raw.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        return [v for v in raw.values() if isinstance(v, dict)]
    if isinstance(raw, str):
        logger.warning(
            "page.evaluate が文字列を返しました: %.120s",
            raw.replace("\n", " "),
        )
    return []


def _member_dicts_only(members: list[Any]) -> list[dict[str, Any]]:
    return [m for m in members if isinstance(m, dict)]


class MiteneSender:
    def __init__(
        self,
        base_url: str,
        login_id: str,
        password: str,
        flow: str,
        login: LoginConfig,
        standard: MiteneStandardConfig,
        gift: MiteneGiftConfig,
        browser: BrowserConfig,
        auth_state_path: Path | None = None,
        log_dir: Path | None = None,
        screenshot_on_error: bool = True,
        dry_run: bool = False,
        human: HumanBehavior | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.login_id = login_id
        self.password = password
        self.flow = flow
        self.login = login
        self.standard = standard
        self.gift = gift
        self.browser_cfg = browser
        self.auth_state_path = auth_state_path
        self.log_dir = log_dir or Path("logs")
        self.screenshot_on_error = screenshot_on_error
        self.dry_run = dry_run
        self.human = human or HumanBehavior({"enabled": False})
        self._sent_log = self.log_dir / "sent_history.jsonl"
        self._member_send_log = self.log_dir / "member_sends.jsonl"
        self._sent_member_keys: set[str] = set()
        self._failed_member_keys: set[str] = set()
        self._member_last_sent: dict[str, date] = {}
        self._send_button_queue: list[str] = []  # comeon-{会員ID}
        self._last_run_report: dict[str, Any] = {}
        self._current_list_path: str = ""
        self._current_step: PriorityStep | None = None
        self._send_target: int = 0
        self._send_done: int = 0
        self._no_history_sent_today: int = 0
        self._match_rate_had_new: bool | None = None
        self._pipeline_had_new_member: bool = False
        self._cached_list_cards: list[dict[str, Any]] | None = None
        self._cached_list_url: str = ""
        self._last_tab_parse_stats: dict[str, Any] = {}
        self._last_goto_access_block = False

    MITENE_ACTION_TEXTS = ("ミテネを送る", "ミテネする", "ミテネ送る")

    def _is_transient_access_block(
        self, page: Page | None = None, error: BaseException | str | None = None
    ) -> bool:
        """ERR_SSL_PROTOCOL_ERROR / chrome-error 等の一時ブロックを検知."""
        if page is not None and self._is_browser_error_page(page):
            return True
        msg = (str(error) if error else "").lower()
        return any(
            token in msg
            for token in (
                "err_ssl_protocol_error",
                "ssl_protocol_error",
                "err_ssl",
                "net::err_",
                "chrome-error",
            )
        )

    def _wait_access_block_cooldown(
        self, page: Page | None, reason: str
    ) -> None:
        wait_sec = LOGIN_ACCESS_BLOCK_WAIT_MS // 1000
        logger.warning(
            "%s — IPブロック防止のため %d 秒待機してからリトライします",
            reason,
            wait_sec,
        )
        try:
            if page is not None:
                page.wait_for_timeout(LOGIN_ACCESS_BLOCK_WAIT_MS)
                return
        except Exception:
            pass
        time.sleep(wait_sec)

    def _wait_before_login_page(self) -> None:
        wait_sec = random.uniform(*LOGIN_PRE_OPEN_WAIT_SEC)
        logger.info("ログイン画面を開く前に %.1f 秒待機", wait_sec)
        time.sleep(wait_sec)

    def _wait_page_settled(self, page: Page, *, quick: bool = False) -> None:
        """画面遷移後に待つ（networkidle は使わない＝ずっと待ち続ける原因になりやすい）."""
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000 if quick else 12000)
        except Exception:
            pass
        page.wait_for_timeout(80 if quick else 200)

    def _is_browser_error_page(self, page: Page) -> bool:
        url = (page.url or "").lower()
        return url.startswith("chrome-error://") or url == "about:blank"

    def _safe_goto(self, page: Page, url: str) -> bool:
        """遷移に失敗したら False（chrome-error / SSL エラーなど）."""
        self._last_goto_access_block = False
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.browser_cfg.timeout_ms)
            self._wait_page_settled(page)
        except Exception as e:
            blocked = self._is_transient_access_block(error=e)
            self._last_goto_access_block = blocked
            if blocked:
                logger.warning(
                    "ページ遷移失敗（一時ブロックの可能性）: %s (%s)", url, e
                )
            else:
                logger.warning("ページ遷移失敗: %s (%s)", url, e)
            return False
        if self._is_browser_error_page(page):
            self._last_goto_access_block = True
            logger.warning("ページを表示できません: %s → %s", url, page.url)
            return False
        return True

    def _gid(self) -> str:
        """女の子ID（gid クエリ。例: 39760216）."""
        return (self.login_id or "").strip()

    def _list_url(self, page: Page, list_path: str) -> str:
        """女の子ID付きの会員一覧URL."""
        gid = self._gid()
        if not gid or not list_path:
            return ""
        url = build_list_url(gid, list_path)
        if url:
            return url
        current = urlparse(page.url)
        base = urlparse(self.base_url if "://" in self.base_url else f"https://{self.base_url}")
        host = current.netloc or base.netloc
        scheme = current.scheme or base.scheme or "https"
        path = list_path if list_path.startswith("/") else f"/{list_path}"
        return urlunparse((scheme, host, path, "", f"gid={gid}", ""))

    def _pickup_list_url(self, page: Page) -> str:
        return self._list_url(page, "/J10ComeonVisitorList.php")

    def _list_path_slug(self, list_path: str) -> str:
        """J10ComeonMyGirlList.php → comeonmygirllist"""
        name = (list_path or "").lower().split("/")[-1]
        return name.replace(".php", "")

    def _is_member_profile_page(self, page: Page) -> bool:
        u = (page.url or "").lower()
        return "j1girluserpage" in u or "girluserpage" in u

    def _step_markers(self, step: PriorityStep) -> dict[str, Any]:
        tab = step.tab
        path = step.list_path or TAB_LIST_PATHS.get(tab, "")
        slug = self._list_path_slug(path)
        base = STEP_TAB_MARKERS.get(tab, {})
        return {
            "tab": tab,
            "slug": slug or base.get("slug", ""),
            "headings": base.get("headings", ()),
        }

    def _is_step_tab_active(self, page: Page, step: PriorityStep) -> bool:
        m = self._step_markers(step)
        try:
            return bool(
                page.evaluate(
                    """({ tabName, slug }) => {
                        for (const a of document.querySelectorAll(
                            '.kitene_ranking .tab a, .kitene_ranking ul.tab a, ul.tab a'
                        )) {
                            const t = (a.innerText || '').trim();
                            const href = (a.getAttribute('href') || '').toLowerCase();
                            const li = a.closest('li');
                            if (!li || !li.classList.contains('active')) continue;
                            if (t === tabName) return true;
                            if (slug && href.includes(slug)) return true;
                        }
                        return false;
                    }""",
                    {"tabName": m["tab"], "slug": m["slug"]},
                )
            )
        except Exception:
            return False

    def _is_step_heading_visible(self, page: Page, step: PriorityStep) -> bool:
        m = self._step_markers(step)
        headings = m.get("headings") or ()
        if not headings:
            return False
        try:
            return bool(
                page.evaluate(
                    """(headings) => {
                        const matchText = (text) => {
                            const t = (text || '').trim();
                            if (!t || t.length > 120) return false;
                            return headings.some(h => t.includes(h));
                        };
                        for (const sel of [
                            'h1', 'h2', 'h3',
                            '.kitene_ranking_title', '.page_title',
                            '.list_title', '.title_text'
                        ]) {
                            for (const el of document.querySelectorAll(sel)) {
                                if (matchText(el.innerText)) return true;
                            }
                        }
                        const ranking = document.querySelector('.kitene_ranking');
                        if (ranking) {
                            const top = (ranking.innerText || '').slice(0, 280);
                            if (headings.some(h => top.includes(h))) return true;
                        }
                        return false;
                    }""",
                    list(headings),
                )
            )
        except Exception:
            return False

    def _verify_step_list(self, page: Page, step: PriorityStep) -> bool:
        """意図したタブの一覧か（URL / active タブ / 見出し）."""
        if self._is_member_profile_page(page):
            return False
        path = step.list_path or TAB_LIST_PATHS.get(step.tab, "")
        if path and self._is_on_step_list_page(page, path):
            return True
        if self._is_step_tab_active(page, step):
            return True
        if self._is_step_heading_visible(page, step):
            return True
        return False

    def _is_on_step_list_page(self, page: Page, list_path: str) -> bool:
        """意図した会員一覧URLにいるか（会員プロフィールは除外）."""
        if self._is_member_profile_page(page):
            return False
        slug = self._list_path_slug(list_path)
        if not slug:
            return self._is_on_comeon_list_page(page)
        u = (page.url or "").lower().replace(".php", "")
        return slug.replace(".php", "") in u

    def _list_url_for_step(self, page: Page, step: PriorityStep) -> str:
        path = step.list_path or TAB_LIST_PATHS.get(step.tab, "")
        return self._list_url(page, path)

    def _is_on_comeon_list_page(self, page: Page) -> bool:
        if self._is_member_profile_page(page):
            return False
        u = (page.url or "").lower()
        return any(
            p in u
            for p in (
                "comeonmygirl",
                "comeonkeep",
                "comeonaimatching",
                "comeonvisitor",
                "mitenepickup",
            )
        )

    def _is_mygirl_list_page(self, page: Page) -> bool:
        u = (page.url or "").lower()
        return "comeonmygirllist" in u or "j10comeonmygirllist" in u

    def _ensure_comeon_context(self, page: Page) -> bool:
        """ミテネ会員一覧（横タブあり）の画面に入る."""
        if self._is_member_profile_page(page):
            logger.info("会員プロフィールからみたよ一覧へ戻ります")
            visitor = self._pickup_list_url(page)
            if visitor and self._safe_goto(page, visitor):
                self._dismiss_optional_popups(page)
                return self._wait_for_member_list(page, timeout_ms=18000)
        if self._is_on_comeon_list_page(page) and self._pickup_tab_bar_visible(page):
            return True
        if self._open_visitor_list_direct(page):
            return True
        try:
            self._open_find_members(page)
            return self._pickup_tab_bar_visible(page) or self._page_has_send_targets(page)
        except RuntimeError as e:
            logger.warning("会員探し画面を開けませんでした: %s", e)
            return False

    def _visitor_list_path(self) -> str:
        return "/J10ComeonVisitorList.php"

    def _ensure_pickup_hub(self, page: Page) -> bool:
        """ミテネ Pick Up の入口（みたよ一覧＋横タブ）。全タブ共通の起点."""
        visitor_path = self._visitor_list_path()
        if self._is_member_profile_page(page):
            logger.info("プロフィールからみたよ一覧へ")
            visitor = self._pickup_list_url(page)
            if not visitor or not self._safe_goto(page, visitor):
                return False
            self._wait_page_settled(page)
            self._dismiss_optional_popups(page)
            return self._wait_for_member_list(page, timeout_ms=18000)

        if self._is_on_step_list_page(page, visitor_path):
            return self._pickup_tab_bar_visible(page) or self._wait_for_member_list(
                page, timeout_ms=12000
            )

        if self._is_on_comeon_list_page(page) and self._pickup_tab_bar_visible(page):
            hub = PriorityStep(tab="みたよ", list_path=visitor_path)
            if self._follow_tab_list_link(page, hub):
                self._wait_page_settled(page)
                if self._is_on_step_list_page(page, visitor_path):
                    return True

        if self._open_visitor_list_direct(page):
            return True
        return self._ensure_comeon_context(page)

    def _ensure_visitor_list_entry(self, page: Page) -> bool:
        """後方互換: _ensure_pickup_hub へ."""
        return self._ensure_pickup_hub(page)

    def _find_tab_list_href(self, page: Page, list_path: str) -> str:
        """横タブ ul.tab 内の一覧リンク href."""
        slug = self._list_path_slug(list_path)
        if not slug:
            return ""
        try:
            href = page.evaluate(
                """(slug) => {
                    for (const a of document.querySelectorAll(
                        '.kitene_ranking .tab a, .kitene_ranking ul.tab a, ul.tab a'
                    )) {
                        const h = (a.getAttribute('href') || '').toLowerCase();
                        if (h.includes(slug)) return a.getAttribute('href') || '';
                    }
                    return '';
                }""",
                slug,
            )
            return str(href or "").strip()
        except Exception:
            return ""

    def _follow_tab_list_link(self, page: Page, step: PriorityStep) -> bool:
        """横タブ ul.tab 内の href へ遷移（マイガールはクリックのみ・直URL禁止）."""
        path = step.list_path or TAB_LIST_PATHS.get(step.tab, "")
        slug = self._list_path_slug(path)
        if not slug:
            return False
        slug_l = slug.lower()
        logger.info("横タブ切替: %s → %s", step.tab, slug_l)

        if step.tab == "マイガール":
            return self._open_mygirl_via_keep_tab(page)

        href = self._find_tab_list_href(page, path)
        if href and not href.lower().startswith("javascript"):
            target = urljoin(page.url, href)
            logger.info("【%s】タブURLへ遷移: %s", step.tab, target)
            if self._safe_goto(page, target):
                self._wait_page_settled(page)
                self._dismiss_optional_popups(page)
                if self._verify_step_list(page, step):
                    logger.info("【%s】一覧表示OK: %s", step.tab, page.url)
                    return True

        target = self._list_url_for_step(page, step)
        if target and self._safe_goto(page, target):
            self._wait_page_settled(page)
            self._dismiss_optional_popups(page)
            if self._verify_step_list(page, step):
                logger.info("【%s】直接URLで一覧表示: %s", step.tab, page.url)
                return True

        selectors = (
            f'.kitene_ranking ul.tab a[href*="{slug_l}"]',
            f'.kitene_ranking .tab a[href*="{slug_l}"]',
            f'ul.tab a[href*="{slug_l}"]',
        )
        for sel in selectors:
            loc = page.locator(sel)
            if self._safe_count(loc) == 0:
                continue
            el = loc.first
            try:
                el.scroll_into_view_if_needed(timeout=5000)
                el.click(timeout=10000)
            except Exception:
                continue
            self._wait_page_settled(page)
            self._dismiss_optional_popups(page)
            if self._verify_step_list(page, step):
                logger.info("【%s】タブクリックで一覧表示: %s", step.tab, page.url)
                return True
        return False

    def _count_mitene_send_buttons_on_surface(self, surface: Page) -> int:
        try:
            return int(
                surface.evaluate(
                    """() => {
                        const isSendable = (el, wrap) => {
                            if (el.closest('.kitene_send_zumi_btn')) return false;
                            const w = wrap || el.closest('.kitene_send_btn');
                            if (w) {
                                const zumi = w.querySelector('.kitene_send_zumi_btn');
                                if (zumi) {
                                    const zs = getComputedStyle(zumi);
                                    if (zs.display !== 'none' && zs.visibility !== 'hidden'
                                        && zumi.offsetParent) return false;
                                }
                                const t = (w.innerText || '').replace(/\\s+/g, ' ').trim();
                                if (t.includes('送信済')) return false;
                            }
                            const r = el.getBoundingClientRect();
                            if (r.width < 60 || r.height < 18 || !el.offsetParent) return false;
                            return true;
                        };
                        const kiteneSel =
                            'a.kitene_send_btn__text_wrapper[onclick*="registComeon"], '
                            + 'a[onclick^="registComeon"], '
                            + '.kitene_send_btn a.kitene_send_btn__text_wrapper, '
                            + '.kitene_send_btn.active a';
                        let nodes = [...document.querySelectorAll(kiteneSel)];
                        if (!nodes.length) {
                            const re = /ミテネを送る|ミテネする/;
                            nodes = [...document.querySelectorAll(
                                'a, button, [role="button"]'
                            )].filter(el => {
                                const raw = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                                return re.test(raw) && raw.length <= 50;
                            });
                        }
                        let n = 0;
                        for (const el of nodes) {
                            if (!isSendable(el, el.closest('.kitene_send_btn'))) continue;
                            n++;
                        }
                        return n;
                    }"""
                )
            )
        except Exception:
            return 0

    def _count_mitene_send_buttons(self, page: Page) -> int:
        total = 0
        for surface in self._iter_surfaces(page):
            total += self._count_mitene_send_buttons_on_surface(surface)
        return total

    def _collect_mitene_send_button_indices(self, page: Page) -> list[int]:
        """画面上の CTA（a.kitene_send_btn__text_wrapper / registComeon）を上から順に."""
        try:
            indices = page.evaluate(
                """() => {
                    const kiteneSel =
                        '.kitene_send_btn.active a.kitene_send_btn__text_wrapper, '
                        + 'a.kitene_send_btn__text_wrapper[onclick*="registComeon"], '
                        + 'a[onclick^="registComeon"]';
                    let nodes = [...document.querySelectorAll(kiteneSel)];
                    if (!nodes.length) {
                        const re = /ミテネを送る|ミテネする/;
                        nodes = [...document.querySelectorAll(
                            'a, button, input, [role="button"], div, span'
                        )].filter(el => {
                            const raw = (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim();
                            return re.test(raw) && raw.length <= 50;
                        });
                    }
                    const found = [];
                    for (const el of nodes) {
                        if (el.closest('.kitene_send_zumi_btn')) continue;
                        const wrap = el.closest('.kitene_send_btn');
                        if (wrap && !wrap.classList.contains('active')) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 80 || r.height < 24 || r.top < 85) continue;
                        if (!el.offsetParent) continue;
                        const st = getComputedStyle(el);
                        if (st.display === 'none' || st.visibility === 'hidden') continue;
                        found.push({ el, top: r.top, area: r.width * r.height });
                    }
                    found.sort((a, b) => a.top - b.top || a.area - b.area);
                    document.querySelectorAll('[data-mitene-auto-idx]').forEach(
                        el => el.removeAttribute('data-mitene-auto-idx')
                    );
                    found.forEach((item, i) => item.el.setAttribute('data-mitene-auto-idx', String(i)));
                    return found.map((_, i) => i);
                }"""
            )
            return list(indices) if indices else []
        except Exception:
            return []

    def _click_mitene_send_button_at_index(self, page: Page, index: int) -> bool:
        try:
            return bool(
                page.evaluate(
                    """(idx) => {
                        const el = document.querySelector(`[data-mitene-auto-idx="${idx}"]`);
                        if (!el) return false;
                        el.scrollIntoView({ block: 'center', inline: 'nearest' });
                        el.click();
                        return true;
                    }""",
                    index,
                )
            )
        except Exception:
            return False

    def _open_visitor_list_direct(self, page: Page) -> bool:
        target = self._pickup_list_url(page)
        if not target:
            return False
        logger.info("会員探し画面を直接開きます: %s", target)
        if not self._safe_goto(page, target):
            return False
        if self.standard.skip_special_banners:
            self._dismiss_optional_popups(page)
        return self._wait_for_member_list(page, timeout_ms=18000)

    def _safe_count(self, locator: Locator) -> int:
        try:
            return locator.count()
        except Exception:
            return 0

    def _safe_count_text(self, page: Page, text: str, *, exact: bool = False) -> int:
        return self._safe_count(page.get_by_text(text, exact=exact))

    def _safe_is_visible(self, locator: Locator) -> bool:
        try:
            return locator.is_visible()
        except Exception:
            return False

    def _attach_page_handlers(self, page: Page) -> None:
        """confirm() 等のネイティブダイアログを自動承認."""

        def _on_dialog(dialog) -> None:
            try:
                logger.debug("ブラウザダイアログ: %s", dialog.message)
                dialog.accept()
            except Exception:
                pass

        page.on("dialog", _on_dialog)

    def _safe_inner_text(self, page: Page) -> str:
        try:
            return page.inner_text("body")
        except Exception as e:
            if _is_destroyed_context_error(e):
                return ""
            raise

    def run(self) -> int:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._last_run_report = {}
        sent = 0

        with sync_playwright() as p:
            browser = self._launch(p)
            page = None
            for attempt in range(2):
                if attempt == 1 and self.auth_state_path and self.auth_state_path.exists():
                    logger.warning("ページを開けないためセッションを破棄して再ログインします")
                    self.auth_state_path.unlink()
                context = self._new_context(browser, use_storage=attempt == 0)
                page = context.new_page()
                page.set_default_timeout(self.browser_cfg.timeout_ms)
                self._attach_page_handlers(page)
                try:
                    self._ensure_logged_in(page, context)
                    if self.flow == "gift":
                        self._send_mitene_gift(page)
                        sent = 0 if self.dry_run else 1
                        if not self.dry_run:
                            self._record_sent(count=1, flow="gift")
                    else:
                        sent = self._send_mitene_standard(page)
                        if not self.dry_run and sent > 0:
                            self._record_sent(count=sent, flow="standard")
                    if self.auth_state_path:
                        self.auth_state_path.parent.mkdir(parents=True, exist_ok=True)
                        context.storage_state(path=str(self.auth_state_path))
                    context.close()
                    break
                except Exception as e:
                    if self.screenshot_on_error and page:
                        self._save_error_screenshot(page)
                    context.close()
                    msg = str(e).lower()
                    blocked = self._is_transient_access_block(page, error=e)
                    retry = attempt == 0 and self.auth_state_path is not None and (
                        blocked
                        or "開けません" in str(e)
                        or "chrome-error" in msg
                        or "横タブ" in str(e)
                    )
                    if retry:
                        if blocked:
                            self._wait_access_block_cooldown(
                                page,
                                "ページ読み込み失敗（SSL/通信エラー）を検知",
                            )
                        continue
                    raise
            browser.close()
        return sent

    def _launch(self, p: Playwright) -> Browser:
        return p.chromium.launch(
            headless=self.browser_cfg.headless,
            slow_mo=self.browser_cfg.slow_mo_ms,
        )

    def _new_context(self, browser: Browser, *, use_storage: bool = True) -> BrowserContext:
        kwargs: dict[str, Any] = {
            "viewport": {
                "width": self.browser_cfg.viewport_width,
                "height": self.browser_cfg.viewport_height,
            },
            "is_mobile": self.browser_cfg.is_mobile,
            "locale": "ja-JP",
            "ignore_https_errors": True,
        }
        if (
            use_storage
            and self.auth_state_path
            and self.auth_state_path.exists()
        ):
            kwargs["storage_state"] = str(self.auth_state_path)
        if self.browser_cfg.is_mobile:
            kwargs["user_agent"] = (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            )
        return browser.new_context(**kwargs)

    def _canonical_login_url(self) -> str:
        """正規ログインURL（J1Login.php を優先）."""
        base = (self.base_url or "").strip()
        if base and "j1login.php" in base.lower():
            return base
        return SPGIRL_LOGIN_URL

    def _open_login_page(self, page: Page) -> bool:
        """J1Login.php を明示的に開く（失敗時のみ base_url にフォールバック）."""
        self._wait_before_login_page()
        login_url = self._canonical_login_url()
        logger.info("ログインURLを開きます: %s", login_url)
        if self._safe_goto(page, login_url) and not self._is_browser_error_page(page):
            return True
        if self._last_goto_access_block:
            return False
        if base := (self.base_url or "").strip():
            if base.rstrip("/").lower() != login_url.rstrip("/").lower():
                logger.warning(
                    "J1Login.php を開けないため base_url を試行: %s", base
                )
                if self._safe_goto(page, base) and not self._is_browser_error_page(
                    page
                ):
                    return True
        return False

    def _attempt_login(self, page: Page) -> tuple[bool, str]:
        """ID/PW 入力 → 送信 → 5秒待機 → DOM で成否判定."""
        logger.info("①ログイン画面: ID・パスワードを入力")
        self._fill_login_form(page)
        self._click_login_submit(page)
        logger.info(
            "ログイン送信後 %d 秒待機（ページ安定まで）",
            LOGIN_POST_SUBMIT_WAIT_SEC,
        )
        time.sleep(LOGIN_POST_SUBMIT_WAIT_SEC)
        self._wait_page_settled(page, quick=True)
        self.human.after_login_pause()

        if self._has_login_error(page):
            return (
                False,
                "女の子IDまたはパスワードが正しくありません。"
                "管理画面の「女の子ログイン」で、スマホと同じID・パスワードか確認してください。",
            )
        if self._looks_logged_in(page):
            return True, ""
        return (
            False,
            f"{self._page_debug_hint(page)} "
            f"ログインURLは {SPGIRL_LOGIN_URL} を使用しています。",
        )

    def _finish_logged_in(self, page: Page) -> None:
        """ログイン成功後のホーム遷移."""
        self.human.after_login_pause()
        if not self._is_on_pickup_member_page(page):
            self._ensure_deco_home(page)

    def _ensure_logged_in(self, page: Page, context: BrowserContext) -> None:
        """セッション切れ時も J1Login.php から最大3回まで安全に再ログイン."""
        last_error = ""

        for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
            logger.info("ログイン試行 %d/%d", attempt, LOGIN_MAX_ATTEMPTS)

            if not self._open_login_page(page):
                last_error = (
                    "ログインページを開けませんでした。"
                    f"{self._page_debug_hint(page)} "
                    "スマホのSafariで同じURLが開くか確認し、"
                    "「女の子ログイン」でURLを登録し直してください。"
                )
                logger.warning("%s", last_error)
            else:
                self.human.action_pause()
                if self._looks_logged_in(page):
                    logger.info("ログイン済み（セッション利用）")
                    self._finish_logged_in(page)
                    return

                ok, err = self._attempt_login(page)
                if ok:
                    logger.info("①ログイン成功 → ②ホームへ")
                    self._finish_logged_in(page)
                    return
                last_error = err
                logger.warning(
                    "ログイン失敗 (%d/%d): %s", attempt, LOGIN_MAX_ATTEMPTS, err
                )

            if attempt < LOGIN_MAX_ATTEMPTS:
                if self._last_goto_access_block or self._is_transient_access_block(
                    page
                ):
                    self._wait_access_block_cooldown(
                        page,
                        "ページ読み込み失敗（SSL/通信エラー）を検知",
                    )
                else:
                    wait_sec = LOGIN_RETRY_WAIT_MS // 1000
                    logger.info(
                        "IPブロック防止のため %d 秒待機してから再ログインします",
                        wait_sec,
                    )
                    page.wait_for_timeout(LOGIN_RETRY_WAIT_MS)

        raise RuntimeError(
            f"ログインに失敗しました（{LOGIN_MAX_ATTEMPTS}回試行）。{last_error}"
        )

    def _has_login_error(self, page: Page) -> bool:
        body = self._safe_inner_text(page)
        if not body:
            return False
        return (
            "IDまたはパスワードが正しくありません" in body
            or "パスワードが正しくありません" in body
        )

    def _fill_login_form(self, page: Page) -> None:
        id_field = page.get_by_placeholder(self.login.id_placeholder)
        pw_field = page.get_by_placeholder(self.login.password_placeholder)

        if self._safe_count(id_field) == 0:
            id_field = page.locator('input[type="text"], input[type="email"]').first
        if self._safe_count(pw_field) == 0:
            pw_field = page.locator('input[type="password"]').first

        id_field.fill(self.login_id)
        pw_field.fill(self.password)

    def _click_login_submit(self, page: Page) -> None:
        btn = page.get_by_role("button", name=self.login.submit_text)
        if self._safe_count(btn) > 0:
            self.human.human_click(page, btn.first)
            return
        self.human.human_click(
            page, page.get_by_text(self.login.submit_text, exact=True).first
        )

    def _looks_logged_in(self, page: Page) -> bool:
        if self._is_browser_error_page(page):
            return False
        if self._page_has_send_targets(page):
            return True
        if self._pickup_tab_bar_visible(page):
            return True
        markers = [
            self.standard.find_members_button,
            "お客様へアプローチ",
            "写メ日記を書く",
            "残り回数",
        ]
        for text in markers:
            if self._safe_count_text(page, text) > 0:
                return True
        try:
            content = page.content()
        except Exception:
            return self._safe_count_text(page, self.standard.find_members_button) > 0
        if self.login.id_placeholder in content and self.login.submit_text in content:
            return False
        return "ログイン" not in content or self.standard.find_members_button in content

    def _is_on_pickup_member_page(self, page: Page) -> bool:
        if self._pickup_tab_bar_visible(page):
            return True
        url = page.url.lower()
        return "comeonvisitorlist" in url or "mitenepickup" in url

    def _page_debug_hint(self, page: Page) -> str:
        if self._is_browser_error_page(page):
            return (
                "URL=chrome-error（ページ読み込み失敗）。"
                "ERR_SSL_PROTOCOL_ERROR 等の一時ブロックの可能性があります。"
                "60秒待機後に再試行します。"
                "スマホで開けるURLを「女の子ログイン」に登録し直してください。"
            )
        try:
            snippet = (page.inner_text("body") or "").replace("\n", " ")[:150]
        except Exception:
            snippet = "(本文取得不可)"
        return f"URL={page.url} … {snippet}"

    def _ensure_pickup_ready(self, page: Page, timeout_ms: int = 15000) -> bool:
        page.evaluate("window.scrollTo(0, 0)")
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._pickup_tab_bar_visible(page):
                return True
            page.wait_for_timeout(500)
        return False

    def _ensure_deco_home(self, page: Page) -> None:
        """姫デコホーム（ミテネCTA・残り回数）が表示されるまで待ち、スクロール."""
        label = self.standard.remaining_label
        button = self.standard.find_members_button
        for selector in (
            page.get_by_text(button, exact=False),
            page.get_by_text(label, exact=False),
            page.get_by_text("お客様へアプローチ", exact=False),
        ):
            try:
                selector.first.wait_for(state="visible", timeout=12000)
                selector.first.scroll_into_view_if_needed(timeout=5000)
                break
            except Exception:
                continue
        self._wait_page_settled(page, quick=True)
        self.human.action_pause()
        for _ in range(3):
            if self._parse_remaining_count(page) is not None:
                return
            page.evaluate("window.scrollBy(0, Math.min(window.innerHeight, 420))")
            page.wait_for_timeout(400)

    def _extract_remaining_from_text(self, text: str) -> int | None:
        normalized = _normalize_digits(text)
        match = REMAINING_PATTERN.search(normalized)
        if not match:
            return None
        for g in match.groups():
            if g is not None:
                return int(g)
        return None

    def _parse_remaining_count(self, page: Page) -> int | None:
        """CTA付近またはページ内の「ミテネ残り回数：N回」を取得."""
        try:
            return self._parse_remaining_count_inner(page)
        except Exception as e:
            if _is_destroyed_context_error(e):
                return None
            raise

    def _parse_remaining_count_inner(self, page: Page) -> int | None:
        near = self._parse_remaining_near_cta(page)
        if near is not None:
            return near

        try:
            loc = page.get_by_text(self.standard.remaining_label, exact=False)
            n = self._safe_count(loc)
            if n > 0:
                for i in range(min(n, 5)):
                    text = loc.nth(i).evaluate(
                        """el => {
                            let node = el;
                            for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
                                const t = (node.innerText || '').trim();
                                if (t.includes('ミテネ残り回数')) return t;
                            }
                            return (el.innerText || '').trim();
                        }"""
                    )
                    parsed = self._extract_remaining_from_text(text)
                    if parsed is not None:
                        return parsed
        except Exception:
            pass

        for source in (
            lambda: page.inner_text("body"),
            lambda: page.content(),
        ):
            try:
                parsed = self._extract_remaining_from_text(source())
                if parsed is not None:
                    return parsed
            except Exception:
                continue
        return None

    def _parse_remaining_near_cta(self, page: Page) -> int | None:
        cta = page.get_by_text(self.standard.find_members_button, exact=False)
        if self._safe_count(cta) == 0:
            return None
        try:
            block_text = cta.first.evaluate(
                """el => {
                    let node = el;
                    for (let i = 0; i < 10 && node; i++, node = node.parentElement) {
                        const t = (node.innerText || '').trim();
                        if (t.includes('ミテネ残り回数')) return t;
                    }
                    const section = el.closest('section, article, li, [class*="approach"], [class*="mitene"], div');
                    return (section?.innerText || el.innerText || '').trim();
                }"""
            )
        except Exception:
            return None
        return self._extract_remaining_from_text(block_text)

    def _read_send_budget(self, page: Page) -> int:
        if not self._is_on_pickup_member_page(page):
            self._ensure_deco_home(page)
        remaining = self._parse_remaining_count(page)
        if remaining is None:
            on_home = (
                self._safe_count_text(page, self.standard.find_members_button) > 0
            )
            if not on_home:
                raise RuntimeError(
                    "姫デコのホーム画面を開けませんでした。"
                    "「女の子ログイン」のURLが古い・期限切れ、またはログインに失敗している可能性があります。"
                    "スマホのSafariで開いているログインページのURLを、PHPSESSID 付きの古いURLではなく"
                    "今開いているURLのまま登録し直してから再実行してください。"
                )
            raise RuntimeError(
                f"「{self.standard.remaining_label}」が見つかりません。"
                "ホーム画面で CTA の下に表示されているか確認してください。"
                "（画面上は20回あっても、自動操作が別ページを見ている場合があります）"
            )
        logger.info("送信予定回数（ミテネ残り回数）: %d", remaining)
        if remaining <= 0:
            raise DailyLimitReached("ミテネ残り回数が 0 です。")
        if self.standard.max_send_per_run > 0:
            remaining = min(remaining, self.standard.max_send_per_run)
        return remaining

    def _load_member_send_history(self) -> None:
        """会員ごとの最終送信日を読み込む."""
        self._member_last_sent.clear()
        first_sent: dict[str, date] = {}
        if not self._member_send_log.exists():
            self._no_history_sent_today = 0
            return
        try:
            with self._member_send_log.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    key = record.get("member_key")
                    raw_date = record.get("date")
                    if not key or not raw_date:
                        continue
                    try:
                        sent_on = date.fromisoformat(str(raw_date)[:10])
                    except ValueError:
                        continue
                    prev = self._member_last_sent.get(key)
                    if prev is None or sent_on > prev:
                        self._member_last_sent[key] = sent_on
                    first = first_sent.get(key)
                    if first is None or sent_on < first:
                        first_sent[key] = sent_on
        except OSError as e:
            logger.warning("会員送信履歴の読み込みに失敗: %s", e)
        today = date.today()
        self._no_history_sent_today = sum(1 for d in first_sent.values() if d == today)

    def _is_member_in_cooldown(self, key: str) -> bool:
        days = self.standard.member_cooldown_days
        if days <= 0:
            return False
        last = self._member_last_sent.get(key)
        if last is None:
            return False
        return (date.today() - last).days < days

    def _remaining_send_needed(self) -> int:
        if self._send_target <= 0:
            return 1
        return max(1, self._send_target - self._send_done)

    def _filter_member_queue(self, keys: list[str]) -> list[str]:
        """送信履歴に基づき会員キューを並べ替え・除外."""
        if not keys:
            return keys
        needed = self._remaining_send_needed()
        days = self.standard.member_cooldown_days
        cap = self.standard.max_no_history_sends_per_day

        no_history = [k for k in keys if k not in self._member_last_sent]
        with_history = [k for k in keys if k in self._member_last_sent]

        if days > 0:
            eligible = [k for k in keys if not self._is_member_in_cooldown(k)]
            in_cooldown = [k for k in keys if self._is_member_in_cooldown(k)]
            if len(eligible) >= needed:
                if in_cooldown:
                    logger.info(
                        "会員クールダウン(%d日): %d 人を除外（候補 %d 人）",
                        days,
                        len(in_cooldown),
                        len(eligible),
                    )
                keys = eligible
                no_history = [k for k in keys if k not in self._member_last_sent]
                with_history = [k for k in keys if k in self._member_last_sent]
            elif keys:
                logger.info(
                    "会員クールダウン(%d日): 候補不足のため条件を緩和",
                    days,
                )
                keys = eligible + in_cooldown
                no_history = [k for k in keys if k not in self._member_last_sent]
                with_history = [k for k in keys if k in self._member_last_sent]

        if cap > 0 and with_history and len(with_history) < needed:
            keys = no_history + with_history
        elif cap > 0 and len(with_history) >= needed:
            remaining_cap = max(0, cap - self._no_history_sent_today)
            if remaining_cap <= 0:
                keys = with_history
            else:
                keys = no_history[:remaining_cap] + with_history
                if len(no_history) > remaining_cap:
                    logger.info(
                        "履歴なし会員: 本日あと %d 人まで（上限 %d 人）",
                        remaining_cap,
                        cap,
                    )
        else:
            keys = no_history + with_history

        if no_history and with_history:
            logger.info(
                "送信履歴なし %d 人を優先（履歴あり %d 人）",
                len([k for k in keys if k not in self._member_last_sent]),
                len([k for k in keys if k in self._member_last_sent]),
            )
        return keys

    def _record_member_sent(self, key: str) -> None:
        today = date.today()
        self._member_last_sent[key] = today
        record = {
            "member_key": key,
            "date": today.isoformat(),
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with self._member_send_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _mark_member_sent(self, key: str) -> None:
        was_new = key not in self._member_last_sent
        self._sent_member_keys.add(key)
        self._invalidate_list_cache()
        if not self.dry_run:
            self._record_member_sent(key)
            if was_new:
                self._no_history_sent_today += 1

    def _goto_girl_domain_if_needed(self, page: Page) -> None:
        """横タブが spgirl で出ないときだけ girl ドメインを試す（失敗時は spgirl に戻す）."""
        if self._pickup_tab_bar_visible(page):
            return
        url = page.url
        if "spgirl." not in url or self._is_browser_error_page(page):
            return
        alt = url.replace("spgirl.", "girl.", 1)
        logger.info("横タブ未検出のため girl ドメインを試します: %s", alt)
        if self._safe_goto(page, alt) and self._pickup_tab_bar_visible(page):
            return
        sp_back = alt.replace("girl.", "spgirl.", 1)
        if sp_back != page.url:
            logger.info("girl で表示できないため spgirl に戻します")
            self._safe_goto(page, sp_back)

    def _wait_for_send_buttons(self, page: Page, timeout_ms: int = 20000) -> bool:
        """「ミテネを送る」ボタンが出るまで待つ."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._page_has_send_targets(page):
                return True
            page.wait_for_timeout(400)
        return False

    def _ensure_member_list_page(self, page: Page) -> None:
        """送信後、③会員一覧（ミテネを送るが並ぶ画面）に戻す."""
        if self._is_member_profile_page(page):
            logger.info("送信後: プロフィールから一覧へ戻る")
            step = self._current_step
            if step:
                self._navigate_to_url_safe(page, step, force_reload=True)
            else:
                self._open_visitor_list_direct(page)
        if self._is_on_comeon_list_page(page) and self._page_has_send_targets(page):
            return
        self._wait_page_settled(page, quick=True)
        if self._page_has_send_targets(page):
            return
        for _ in range(2):
            try:
                page.go_back()
                self._wait_page_settled(page)
                if self._page_has_send_targets(page):
                    return
            except Exception:
                break
        list_url = ""
        if self._current_list_path:
            list_url = self._list_url(page, self._current_list_path)
        if not list_url:
            list_url = self._pickup_list_url(page)
        if list_url:
            logger.info("一覧へ戻る（再読み込み）: %s", list_url)
            if self._safe_goto(page, list_url):
                self._dismiss_optional_popups(page)
                self._wait_for_send_buttons(page, timeout_ms=15000)

    def _open_find_members(self, page: Page) -> None:
        """②ホームの「ミテネできる会員を探す」→ ③会員一覧."""
        if self._page_has_send_targets(page):
            logger.info("③会員一覧（ミテネを送る あり）")
            self._refresh_send_button_queue(page)
            return

        if self._is_browser_error_page(page):
            self._ensure_deco_home(page)

        logger.info("②「%s」をタップ → ③会員一覧", self.standard.find_members_button)
        cta = page.get_by_text(self.standard.find_members_button, exact=False)
        if self._safe_count(cta) == 0:
            self._ensure_deco_home(page)
            cta = page.get_by_text(self.standard.find_members_button, exact=False)
        if self._safe_count(cta) == 0:
            raise RuntimeError(
                f"「{self.standard.find_members_button}」が見つかりません。"
                f"{self._page_debug_hint(page)}"
            )
        href = cta.first.get_attribute("href")
        if href and not href.startswith("javascript"):
            target = urljoin(page.url, href)
            logger.info("会員探しへ移動: %s", target)
            if not self._safe_goto(page, target):
                raise RuntimeError(
                    f"会員探し画面を開けませんでした。{self._page_debug_hint(page)}"
                )
        else:
            self.human.human_click(page, cta.first)
        self._wait_page_settled(page)
        self.human.action_pause()
        if self.standard.skip_special_banners:
            self._dismiss_optional_popups(page)
        if not self._wait_for_send_buttons(page):
            list_url = self._pickup_list_url(page)
            if list_url:
                logger.info("CTAで開けないため会員一覧URLへ: %s", list_url)
                self._safe_goto(page, list_url)
                self._wait_for_send_buttons(page, timeout_ms=15000)
        if not self._page_has_send_targets(page):
            raise RuntimeError(
                "③会員一覧で「ミテネを送る」が見つかりません。"
                f"{self._page_debug_hint(page)}"
            )
        logger.info("③会員一覧を開きました: %s", page.url)
        self._refresh_send_button_queue(page)

    def _refresh_send_button_queue(self, page: Page, *, log_scan: bool = True) -> int:
        """送れる会員IDをキュー化（タブ条件・送信済判定を反映）."""
        self._wait_page_settled(page, quick=True)
        keys = self._scan_unsent_member_keys(page)
        step = self._current_step
        if (
            self.human.shuffle_member_order
            and len(keys) > 1
            and (not step or step.member_filter == "sendable")
        ):
            no_history = [k for k in keys if k not in self._member_last_sent]
            with_history = [k for k in keys if k in self._member_last_sent]
            if no_history:
                random.shuffle(no_history)
            if with_history:
                random.shuffle(with_history)
            keys = no_history + with_history
        self._send_button_queue = list(keys)
        if log_scan:
            label = self._step_label(step) if step else "一覧"
            logger.info(
                "【%s】「ミテネを送る」送信キュー %d 人",
                label,
                len(self._send_button_queue),
            )
        return len(self._send_button_queue)

    def _stop_browser_pending_tasks(self, page: Page) -> None:
        """遷移前に未完了の読込・クリック要求を破棄（window.stop）."""
        logger.info("移動前にブラウザの未完了処理を強制停止します")
        try:
            page.evaluate("window.stop();")
        except Exception:
            pass
        page.wait_for_timeout(PRE_NAV_STOP_MS)

    def _goto_list_target(self, page: Page, url: str) -> bool:
        """一覧URLへ goto（chrome-error 時は False）."""
        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.browser_cfg.timeout_ms,
            )
        except Exception as e:
            logger.warning("ページ遷移失敗: %s (%s)", url, e)
            return False
        if self._is_browser_error_page(page):
            logger.warning("ページを表示できません: %s → %s", url, page.url)
            return False
        return True

    def _is_on_target_list_url(
        self, page: Page, target: str, list_path: str = ""
    ) -> bool:
        """目的の一覧URL（*List.php）に固定されているか."""
        if self._is_member_profile_page(page):
            return False
        u = (page.url or "").lower()
        if "list.php" not in u:
            return False
        if list_path:
            slug = self._list_path_slug(list_path)
            if slug and slug.replace(".php", "") in u.replace(".php", ""):
                return True
        target_l = target.lower()
        return any(
            part in u
            for part in (
                "comeonmygirllist",
                "comeonkeeplist",
                "comeonaimatchinglist",
                "comeonvisitorlist",
            )
            if part in target_l
        )

    def _force_navigate_to_list(
        self,
        page: Page,
        target: str,
        tab_name: str,
        *,
        list_path: str = "",
    ) -> bool:
        """
        裏タスク強制クリア（window.stop）＋一覧URL固定化（最大10秒監視・直打ち直し）.
        """
        self._stop_browser_pending_tasks(page)
        self._send_button_queue.clear()

        deadline = time.monotonic() + LIST_URL_FIX_TIMEOUT_MS / 1000
        attempt = 0

        while time.monotonic() < deadline:
            attempt += 1
            logger.info(
                "一覧URLへ直接遷移を試みます (試行 %d) -> %s",
                attempt,
                target,
            )
            self._stop_browser_pending_tasks(page)
            if not self._goto_list_target(page, target):
                page.wait_for_timeout(400)
                continue

            page.wait_for_timeout(LIST_GOTO_SETTLE_MS)

            if self._is_member_profile_page(page):
                logger.warning(
                    "【%s】プロフィールページへの引き戻しを検知。再度リトライします: %s",
                    tab_name,
                    page.url,
                )
                continue

            if self._is_on_target_list_url(page, target, list_path):
                logger.info(
                    "【%s】目的の一覧ページへの固定に成功しました: %s",
                    tab_name,
                    page.url,
                )
                page.wait_for_timeout(LIST_URL_FIXED_EXTRA_MS)
                logger.info("【%s】ページの固定を確認。解析を開始します", tab_name)
                return True

            if attempt >= LIST_NAV_ATTEMPTS:
                logger.warning(
                    "【%s】一覧URL不一致 (試行%d): %s",
                    tab_name,
                    attempt,
                    page.url,
                )

        logger.warning(
            "【%s】URLの固定に失敗したため、強制的に目的URLを再度開きます",
            tab_name,
        )
        self._stop_browser_pending_tasks(page)
        self._goto_list_target(page, target)
        page.wait_for_timeout(FINAL_FORCED_WAIT_MS)

        if self._is_on_target_list_url(page, target, list_path):
            logger.info("【%s】最終再打診で一覧固定成功: %s", tab_name, page.url)
            return True

        logger.warning(
            "【%s】一覧URLの固定に最終的に失敗: %s",
            tab_name,
            page.url,
        )
        return False

    def _navigate_to_url_safe(
        self, page: Page, step: PriorityStep, *, force_reload: bool = False
    ) -> bool:
        """URL直打ち遷移（マイガールはキープ経由タブクリック）."""
        if step.tab == "マイガール":
            return self._open_mygirl_via_keep_tab(page)

        path = step.list_path or TAB_LIST_PATHS.get(step.tab, "")
        target = build_list_url(self._gid(), path) or self._list_url_for_step(
            page, step
        )
        if not target:
            logger.warning("タブ「%s」のURLを組み立てられません", step.tab)
            return False

        if (
            not force_reload
            and not self._is_member_profile_page(page)
            and (
                self._verify_step_list(page, step)
                or (path and self._is_on_step_list_page(page, path))
            )
        ):
            logger.info("【%s】一覧表示済み: %s", step.tab, page.url)
            return True

        if not self._force_navigate_to_list(
            page, target, step.tab, list_path=path
        ):
            return False

        for sel in LIST_PAGE_READY_SELECTORS:
            try:
                page.wait_for_selector(sel, state="visible", timeout=8000)
                logger.info("【%s】一覧DOM検出: %s", step.tab, sel)
                break
            except Exception:
                continue

        if self._is_member_profile_page(page):
            return False

        self._dismiss_optional_popups(page)

        if path and self._is_on_step_list_page(page, path):
            logger.info("【%s】URL直打ち成功: %s", step.tab, page.url)
            return True
        if self._verify_step_list(page, step):
            logger.info("【%s】URL直打ち成功: %s", step.tab, page.url)
            return True
        if self._is_on_target_list_url(page, target, path):
            logger.info("【%s】一覧URL固定確認: %s", step.tab, page.url)
            return True

        logger.warning("【%s】一覧DOM検証失敗: %s", step.tab, page.url)
        return False

    def _open_keep_list_hub(self, page: Page) -> bool:
        """キープ一覧へ直打ち（マイガール遷移の起点・確実に開けるURL）."""
        keep_target = build_list_url(self._gid(), KEEP_LIST_PATH)
        if not keep_target:
            return False
        return self._force_navigate_to_list(
            page,
            keep_target,
            "キープ",
            list_path=KEEP_LIST_PATH,
        )

    def _click_mygirl_tab(self, page: Page) -> bool:
        """kitene_ranking / ul.tab 内の「マイガール」タブをクリック（直URLは使わない）."""
        logger.info("【マイガール】タブメニューの「マイガール」リンクをクリック")
        clicked = False
        for surface in self._iter_surfaces(page):
            if self._click_pickup_list_tab(
                surface, "マイガール", list_path=MYGIRL_LIST_PATH
            ):
                clicked = True
                break
        if not clicked:
            for sel in (
                '.kitene_ranking ul.tab a[href*="comeonmygirllist"]',
                '.kitene_ranking .tab a[href*="comeonmygirllist"]',
                'ul.tab a[href*="comeonmygirllist"]',
            ):
                loc = page.locator(sel)
                if self._safe_count(loc) == 0:
                    continue
                try:
                    el = loc.first
                    el.scroll_into_view_if_needed(timeout=5000)
                    el.click(timeout=10000)
                    clicked = True
                    break
                except Exception:
                    continue
        if not clicked:
            loc = page.get_by_text("マイガール", exact=True)
            for i in range(self._safe_count(loc)):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    tag = el.evaluate("el => el.tagName.toLowerCase()")
                    if tag != "a":
                        continue
                    href = (el.get_attribute("href") or "").lower()
                    if "comeonmygirl" not in href:
                        continue
                    el.click(timeout=10000)
                    clicked = True
                    break
                except Exception:
                    continue
        return clicked

    def _open_mygirl_via_keep_tab(self, page: Page) -> bool:
        """
        マイガール一覧: キープ直打ち → タブクリック（gid直URLは拒否されるため）.
        """
        self._stop_browser_pending_tasks(page)
        self._send_button_queue.clear()
        logger.info(
            "【マイガール】キープ一覧を経由してタブクリックで遷移（直URL不可）"
        )
        if not self._open_keep_list_hub(page):
            logger.warning("【マイガール】起点のキープ一覧を開けません")
            return False

        if not self._click_mygirl_tab(page):
            logger.warning("【マイガール】タブクリックに失敗")
            return False

        logger.info(
            "【マイガール】タブクリック後 %dms 待機",
            MYGIRL_TAB_CLICK_WAIT_MS,
        )
        page.wait_for_timeout(MYGIRL_TAB_CLICK_WAIT_MS)

        if self._is_member_profile_page(page):
            logger.warning(
                "【マイガール】タブクリック後もプロフィール: %s",
                page.url,
            )
            return False

        mygirl_step = PriorityStep(tab="マイガール", list_path=MYGIRL_LIST_PATH)
        if (
            self._is_on_step_list_page(page, MYGIRL_LIST_PATH)
            or self._verify_step_list(page, mygirl_step)
            or self._is_step_tab_active(page, mygirl_step)
        ):
            self._current_list_path = MYGIRL_LIST_PATH
            logger.info("【マイガール】一覧表示成功: %s", page.url)
            return True

        logger.warning("【マイガール】一覧確認失敗: %s", page.url)
        return False

    def _goto_step_list_direct(self, page: Page, step: PriorityStep) -> bool:
        """会員一覧URLへ直接遷移（マイガールはキープ経由タブクリック）."""
        return self._navigate_to_url_safe(page, step, force_reload=True)

    def _fetch_tab_members(
        self, page: Page, step: PriorityStep
    ) -> list[dict[str, Any]]:
        """window.stop → URL固定 → スクロール → 会員カード解析."""
        self._invalidate_list_cache()
        self._send_button_queue.clear()
        self._current_step = step
        path = step.list_path or TAB_LIST_PATHS.get(step.tab, "")
        self._current_list_path = path

        if step.tab == "マイガール":
            opened = self._open_mygirl_via_keep_tab(page)
        else:
            opened = self._navigate_to_url_safe(page, step, force_reload=True)
        if not opened:
            logger.warning("【%s】一覧取得失敗（gid=%s）", step.tab, self._gid())
            return []

        if self._is_member_profile_page(page):
            logger.warning("【%s】プロフィールのため解析スキップ: %s", step.tab, page.url)
            return []

        self._scroll_member_list_to_end(page)
        cards = self._parse_list_page_cards(page, step.tab)
        sendable = sum(
            1 for c in cards if isinstance(c, dict) and c.get("has_send_button")
        )
        new_n = sum(
            1
            for c in cards
            if isinstance(c, dict)
            and c.get("has_send_button")
            and not c.get("sent_history")
        )
        logger.info(
            "【%s】会員パース完了: 全 %d 件 / 送信ボタン %d / 新規 %d 件",
            step.tab,
            len(cards),
            sendable,
            new_n,
        )
        return [c for c in cards if isinstance(c, dict)]

    def _filter_new_sendable_cards(
        self, cards: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """新規会員（ミテネ履歴に「送信済」なし）かつ送信ボタンあり."""
        out: list[dict[str, Any]] = []
        for card in cards:
            key = str(card.get("key") or "")
            if not key or not card.get("has_send_button"):
                continue
            if card.get("sent_history"):
                continue
            if key in self._sent_member_keys or key in self._failed_member_keys:
                continue
            out.append(card)
        return out

    def _filter_sendable_cards(
        self, cards: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """送信ボタンがあり未送信キューに入れられる会員."""
        out: list[dict[str, Any]] = []
        for card in cards:
            key = str(card.get("key") or "")
            if not key or not card.get("has_send_button"):
                continue
            if key in self._sent_member_keys or key in self._failed_member_keys:
                continue
            out.append(card)
        return out

    def _sort_all_members_oldest_first_keys(
        self, members: list[dict[str, Any]]
    ) -> list[str]:
        """全会員を送信日古い順（同日ランダム）でキー列にする."""
        pool = [
            m
            for m in members
            if isinstance(m, dict)
            and m.get("key")
            and m.get("has_send_button")
            and m["key"] not in self._sent_member_keys
            and m["key"] not in self._failed_member_keys
        ]
        if not pool:
            return []
        random.shuffle(pool)
        pool.sort(
            key=lambda m: m.get("history_date") or OLDEST_SORT_DEFAULT_DATE  # type: ignore[arg-type]
        )
        ordered: list[dict[str, Any]] = []
        for _, grp in itertools.groupby(
            pool,
            key=lambda m: m.get("history_date") or OLDEST_SORT_DEFAULT_DATE,  # type: ignore[arg-type]
        ):
            batch = list(grp)
            if len(batch) > 1:
                random.shuffle(batch)
            ordered.extend(batch)
        return [str(m["key"]) for m in ordered]

    def _send_member_keys_phase(
        self,
        page: Page,
        label: str,
        keys: list[str],
        budget: int,
        sent: int,
        sent_by_step: dict[str, int],
    ) -> int:
        """キーリストをキューに載せて残り回数ぶん送信."""
        if sent >= budget or not keys:
            return sent
        self._send_button_queue = [k for k in keys if k not in self._sent_member_keys]
        step_limit = min(len(self._send_button_queue), budget - sent)
        if step_limit <= 0:
            return sent
        logger.info("%s: %d 人へ送信開始", label, step_limit)
        return self._send_loop_for_step(
            page, label, budget, sent, sent_by_step, step_limit
        )

    def _merge_parsed_lists(
        self, parsed_lists: dict[str, list[dict[str, Any]]]
    ) -> dict[str, dict[str, Any]]:
        """複数タブの会員を key で合算（古い送信日を優先）."""
        merged: dict[str, dict[str, Any]] = {}
        for cards in parsed_lists.values():
            for card in cards:
                if not isinstance(card, dict) or not card.get("has_send_button"):
                    continue
                if card.get("key") in self._sent_member_keys:
                    continue
                self._merge_member_card(merged, card)
        return merged

    def _execute_phased_send_pipeline(
        self,
        page: Page,
        budget: int,
        sent: int,
        sent_by_step: dict[str, int],
        skipped_steps: list[str],
    ) -> int:
        """フェーズ1（新規優先巡回）→ フェーズ2（全件合算バックアップ）."""
        gid = self._gid()
        logger.info("=== フェーズ1: 新規会員優先巡回（gid=%s）===", gid)

        parsed_lists: dict[str, list[dict[str, Any]]] = {}
        has_any_new_member = False
        new_matchings: list[dict[str, Any]] = []

        # ① キープ直打ち → マイガールタブクリック → 新規送信
        step1 = PriorityStep(
            tab="マイガール",
            member_filter="new_only",
            list_path=MYGIRL_LIST_PATH,
        )
        mygirl_cards = self._fetch_tab_members(page, step1)
        parsed_lists["マイガール"] = mygirl_cards
        new_mygirls = self._filter_new_sendable_cards(mygirl_cards)
        if new_mygirls:
            has_any_new_member = True
            logger.info("【1】新規マイガール %d 件 → 送信", len(new_mygirls))
            keys = [str(m["key"]) for m in new_mygirls]
            sent = self._send_member_keys_phase(
                page, "①マイガール（新規）", keys, budget, sent, sent_by_step
            )
        else:
            logger.info("【1】新規マイガール 0 件")

        if sent >= budget:
            return sent

        # ② キープ一覧へ直打ち → 新規送信
        step2 = PriorityStep(
            tab="キープ",
            member_filter="new_only",
            list_path=KEEP_LIST_PATH,
        )
        keep_cards = self._fetch_tab_members(page, step2)
        parsed_lists["キープ"] = keep_cards
        new_keeps = self._filter_new_sendable_cards(keep_cards)
        if new_keeps:
            has_any_new_member = True
            logger.info("【2】新規キープ %d 件 → 送信", len(new_keeps))
            keys = [str(m["key"]) for m in new_keeps]
            sent = self._send_member_keys_phase(
                page, "②キープ（新規）", keys, budget, sent, sent_by_step
            )
        else:
            logger.info("【2】新規キープ 0 件")

        if sent >= budget:
            return sent

        # ③ マッチ率（新規・残り回数ぶん）
        step3 = PriorityStep(
            tab="マッチ率",
            member_filter="new_only",
            list_path="/J10ComeonAiMatchingList.php",
        )
        match_cards = self._fetch_tab_members(page, step3)
        parsed_lists["マッチ率"] = match_cards
        new_matchings = self._filter_new_sendable_cards(match_cards)
        self._match_rate_had_new = len(new_matchings) > 0
        if new_matchings:
            has_any_new_member = True
            limit = budget - sent
            batch = new_matchings[:limit]
            logger.info(
                "【3】新規マッチ率 %d 件 → 残り %d 回分送信",
                len(new_matchings),
                len(batch),
            )
            keys = [str(m["key"]) for m in batch]
            sent = self._send_member_keys_phase(
                page, "③マッチ率（新規）", keys, budget, sent, sent_by_step
            )
        else:
            logger.info("【3】新規マッチ率 0 件")

        if sent >= budget:
            return sent

        # ④ みたよ（③で新規マッチ率0件のときのみ全員送信）
        step4 = PriorityStep(
            tab="みたよ",
            member_filter="sendable",
            list_path="/J10ComeonVisitorList.php",
        )
        visitor_cards = self._fetch_tab_members(page, step4)
        parsed_lists["みたよ"] = visitor_cards
        if not new_matchings:
            sendable = self._filter_sendable_cards(visitor_cards)
            if sendable:
                logger.info("【4】みたよ全員 %d 件 → 送信", len(sendable))
                keys = [str(m["key"]) for m in sendable]
                sent = self._send_member_keys_phase(
                    page, "④みたよ（全員）", keys, budget, sent, sent_by_step
                )
            else:
                logger.info("【4】みたよ送信対象 0 件")
        else:
            logger.info("【SKIP】マッチ率に新規あり → みたよはスキップ")

        if sent >= budget:
            return sent

        # ⑤ マイガール全会員（送信日古い順）— 新規が1人でもいた場合のみ
        if has_any_new_member:
            logger.info("【5】マイガール全会員を送信日古い順に送信")
            step5 = PriorityStep(
                tab="マイガール",
                member_filter="sent_oldest_first",
                list_path=MYGIRL_LIST_PATH,
            )
            mygirl_all = self._fetch_tab_members(page, step5)
            parsed_lists["マイガール"] = mygirl_all
            keys = self._sort_all_members_oldest_first_keys(mygirl_all)
            sent = self._send_member_keys_phase(
                page, "⑤マイガール（古い順）", keys, budget, sent, sent_by_step
            )
        else:
            logger.info("【5】新規0件のためスキップ（フェーズ2バックアップへ）")
            skipped_steps.append("⑤マイガール（古い順）（バックアップへ）")

        # フェーズ2: 新規が全タブで0件 → 4URL合算・送信日古い順
        if not has_any_new_member and sent < budget:
            logger.warning(
                "全タブに新規会員0件 — バックアップ（全URL合算・古い順）へ切替"
            )
            merged = self._merge_parsed_lists(parsed_lists)
            if not merged:
                for tab_name, list_path in AGGREGATE_LIST_TABS:
                    step = PriorityStep(
                        tab=tab_name,
                        list_path=list_path,
                        member_filter="sendable",
                    )
                    cards = self._fetch_tab_members(page, step)
                    parsed_lists[tab_name] = cards
                merged = self._merge_parsed_lists(parsed_lists)

            keys = self._sort_all_members_oldest_first_keys(list(merged.values()))
            sent = self._send_member_keys_phase(
                page,
                "★全タブ合算（古い順）",
                keys,
                budget,
                sent,
                sent_by_step,
            )
            if not keys:
                skipped_steps.append("★全タブ合算（対象なし）")

        logger.info("本日の送信巡回ルート完了（送信 %d / 目標 %d）", sent, budget)
        return sent

    def _navigate_to_step_list(self, page: Page, step: PriorityStep) -> bool:
        """全タブ共通: 一覧URL直打ち優先（横タブは最後の手段）."""
        if self._goto_step_list_direct(page, step):
            return True

        if self._is_member_profile_page(page):
            logger.info("【%s】プロフィール上のため直打ちを再試行", step.tab)
            if self._goto_step_list_direct(page, step):
                return True

        logger.info("【%s】直打ち失敗 → 横タブ切替を試行", step.tab)
        if self._ensure_pickup_hub(page) and self._follow_tab_list_link(page, step):
            if (
                not self._is_member_profile_page(page)
                and self._verify_step_list(page, step)
            ):
                return True

        logger.warning(
            "【%s】一覧未到達 URL=%s active=%s heading=%s",
            step.tab,
            page.url,
            self._is_step_tab_active(page, step),
            self._is_step_heading_visible(page, step),
        )
        return False

    def _step_queue_summary(self, step: PriorityStep, members: list[dict[str, Any]]) -> str:
        mode = step.member_filter or "sendable"
        members = _member_dicts_only(members)
        if mode == "new_only":
            n = sum(1 for m in members if not m.get("sent_history"))
            return f"未送信 {n} 人"
        if mode == "sent_oldest_first":
            n = sum(1 for m in members if m.get("sent_history"))
            return f"送信履歴あり {n} 人"
        return f"送信可 {len(members)} 人"

    def _open_step_list(self, page: Page, step: PriorityStep) -> bool:
        """マイガール／キープ／マッチ率／みたよ — 全タブ同じ遷移方式."""
        self._invalidate_list_cache()
        self._current_step = step
        path = step.list_path or TAB_LIST_PATHS.get(step.tab, "")
        self._current_list_path = path
        url = self._list_url_for_step(page, step)
        if not url:
            logger.warning("タブ「%s」のURLを組み立てられません", step.tab)
            return False
        logger.info("③【%s】一覧を開く: %s", step.tab, url)

        def _ready() -> bool:
            if self._is_member_profile_page(page):
                return False
            if not (
                self._verify_step_list(page, step)
                or (path and self._is_on_step_list_page(page, path))
            ):
                return False
            self._scroll_member_list_to_end(page)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(300)
            return True

        if not self._navigate_to_url_safe(page, step, force_reload=True):
            n = self._count_mitene_send_buttons(page)
            logger.warning(
                "【%s】一覧を開けません（検出 %d 件）URL=%s active=%s heading=%s",
                step.tab,
                n,
                page.url,
                self._is_step_tab_active(page, step),
                self._is_step_heading_visible(page, step),
            )
            self._save_debug_screenshot(page, f"no-nav-{step.tab}")
            return False

        if not _ready():
            n = self._count_mitene_send_buttons(page)
            logger.warning(
                "【%s】に「ミテネを送る」が見つかりません（検出 %d 件）URL=%s",
                step.tab,
                n,
                page.url,
            )
            self._save_debug_screenshot(page, f"no-buttons-{step.tab}")
            return False

        if step.sub_tab:
            self._activate_sub_tab(page, step.sub_tab)
            self._wait_page_settled(page)

        self._send_button_queue.clear()
        self._refresh_send_button_queue(page)
        members = self._scan_member_cards(page)
        new_n = sum(
            1
            for m in members
            if isinstance(m, dict)
            and m.get("has_send_button")
            and not m.get("sent_history")
        )
        if new_n > 0:
            self._pipeline_had_new_member = True
            logger.info("【%s】新規会員 %d 人を検出", step.tab, new_n)
        logger.info(
            "【%s】一覧を開きました: %s（%s）",
            step.tab,
            page.url,
            self._step_queue_summary(step, members),
        )
        return True

    def _recover_more_send_buttons(self, page: Page) -> bool:
        """③一覧でボタンが足りないとき、スクロール・再読み込みで追加取得."""
        self._ensure_member_list_page(page)
        for _ in range(2):
            self._scroll_member_list(page)
            if self._refresh_send_button_queue(page) > 0:
                return True
        step = self._current_step
        if step:
            logger.info("【%s】一覧を再表示して会員を追加取得", step.tab)
            if self._navigate_to_url_safe(page, step, force_reload=True):
                self._wait_for_send_buttons(page, timeout_ms=15000)
                return self._refresh_send_button_queue(page) > 0
        list_url = self._pickup_list_url(page)
        if list_url:
            logger.info("送る会員を追加取得のためみたよ一覧へ: %s", list_url)
            if self._safe_goto(page, list_url):
                self._wait_for_send_buttons(page, timeout_ms=15000)
                return self._refresh_send_button_queue(page) > 0
        return False

    def _iter_surfaces(self, page: Page):
        yield page
        for frame in page.frames:
            if frame != page.main_frame:
                yield frame

    def _wait_for_member_list(self, page: Page, timeout_ms: int = 20000) -> bool:
        """会員探し画面（Pick Up・横タブ）が出るまで待つ."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for surface in self._iter_surfaces(page):
                if self._pickup_tab_bar_visible(surface):
                    return True
            page.wait_for_timeout(400)
        snippet = ""
        try:
            snippet = (page.inner_text("body") or "")[:400]
        except Exception:
            pass
        logger.warning("会員一覧タブ未検出。画面抜粋: %s", snippet.replace("\n", " "))
        return False

    def _pickup_tab_bar_visible(self, page: Page) -> bool:
        try:
            return bool(
                page.evaluate(
                    """(labels) => {
                        const body = (document.body?.innerText || '');
                        let hits = 0;
                        for (const l of labels) if (body.includes(l)) hits++;
                        return hits >= 4;
                    }""",
                    list(PICKUP_TAB_LABELS),
                )
            )
        except Exception:
            return False

    def _click_pickup_list_tab(
        self,
        page: Page,
        tab_name: str,
        list_path: str = "",
        *,
        dry_run: bool = False,
    ) -> bool:
        """ミテネ Pick Up の横タブ（ul.tab 内の a[href]）をクリック."""
        path_slug = self._list_path_slug(list_path)
        try:
            return bool(
                page.evaluate(
                    """({ tabName, tabLabels, pathSlug, dryRun }) => {
                        const clickA = (el) => {
                            if (!dryRun) {
                                el.scrollIntoView({ inline: 'center', block: 'nearest' });
                                el.click();
                            }
                        };
                        const isTabAnchor = (a) => {
                            const href = (a.getAttribute('href') || '').toLowerCase();
                            if (!href.includes('comeon')) return false;
                            const r = a.getBoundingClientRect();
                            if (r.width < 20 || r.height < 12 || r.top > window.innerHeight * 0.35) {
                                return false;
                            }
                            return true;
                        };
                        // 1) href が一致する ul.tab / .kitene_ranking 内の <a>
                        if (pathSlug) {
                            for (const a of document.querySelectorAll(
                                '.kitene_ranking .tab a, ul.tab a, .kitene_ranking ul a'
                            )) {
                                const href = (a.getAttribute('href') || '').toLowerCase();
                                if (href.includes(pathSlug) && isTabAnchor(a)) {
                                    clickA(a);
                                    return true;
                                }
                            }
                        }
                        // 2) タブ行の <a> でラベル一致（会員バッジの span は除外）
                        for (const row of document.querySelectorAll(
                            '.kitene_ranking ul.tab, ul.tab, .kitene_ranking .tab'
                        )) {
                            for (const a of row.querySelectorAll('a')) {
                                const text = (a.innerText || '').trim();
                                if (text !== tabName || !isTabAnchor(a)) continue;
                                clickA(a);
                                return true;
                            }
                        }
                        // 3) 横タブ行（4ラベル以上）内の <a> のみ
                        for (const row of document.querySelectorAll(
                            'ul, ol, nav, .kitene_ranking'
                        )) {
                            const anchors = [...row.querySelectorAll('a')].filter(a => {
                                const t = (a.innerText || '').trim();
                                return tabLabels.includes(t);
                            });
                            const names = [...new Set(anchors.map(a => (a.innerText||'').trim()))];
                            if (names.length < 4) continue;
                            const hit = anchors.find(a => (a.innerText||'').trim() === tabName);
                            if (hit && isTabAnchor(hit)) {
                                clickA(hit);
                                return true;
                            }
                        }
                        return false;
                    }""",
                    {
                        "tabName": tab_name,
                        "tabLabels": list(PICKUP_TAB_LABELS),
                        "pathSlug": path_slug,
                        "dryRun": dry_run,
                    },
                )
            )
        except Exception:
            return False

    def _click_tab_via_js(
        self,
        page: Page,
        tab_name: str,
        *,
        dry_run: bool = False,
        top_ratio: float = 0.65,
    ) -> bool:
        """サブタブ（新規など）用の簡易クリック."""
        try:
            return bool(
                page.evaluate(
                    """({ tabName, dryRun, topRatio }) => {
                        for (const el of document.querySelectorAll(
                            'a, button, li, span, [role="tab"]'
                        )) {
                            const t = (el.innerText || '').trim();
                            if (t !== tabName || t.length > 12) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width < 8 || r.height < 8) continue;
                            if (r.top > window.innerHeight * topRatio) continue;
                            if (!dryRun) {
                                el.scrollIntoView({ inline: 'center', block: 'nearest' });
                                el.click();
                            }
                            return true;
                        }
                        return false;
                    }""",
                    {"tabName": tab_name, "dryRun": dry_run, "topRatio": top_ratio},
                )
            )
        except Exception:
            return False

    def _scroll_tab_bar(self, page: Page) -> None:
        try:
            page.evaluate(
                """(tabLabels) => {
                    for (const el of document.querySelectorAll('*')) {
                        const t = (el.innerText || '');
                        const n = tabLabels.filter(l => t.includes(l)).length;
                        if (n < 4) continue;
                        if (el.scrollWidth > el.clientWidth + 16) {
                            el.scrollLeft = 0;
                        }
                    }
                }""",
                list(PICKUP_TAB_LABELS),
            )
        except Exception:
            pass

    def _is_active_tab(self, page: Page, tab_name: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """({ tabName, tabLabels }) => {
                        const candidates = [];
                        for (const el of document.querySelectorAll(
                            'a, button, li, span, div, label'
                        )) {
                            const t = (el.innerText || '').trim();
                            if (t !== tabName) continue;
                            let row = el.parentElement;
                            for (let i = 0; i < 8 && row; i++, row = row.parentElement) {
                                const n = tabLabels.filter(l => (row.innerText||'').includes(l)).length;
                                if (n >= 4) {
                                    const style = window.getComputedStyle(el);
                                    const bg = style.backgroundColor || '';
                                    const cls = (el.className || '') + (el.parentElement?.className || '');
                                    const active = /active|selected|current|on|pink/i.test(cls)
                                        || bg.includes('233') || bg.includes('217') || bg.includes('225');
                                    if (active) return true;
                                    candidates.push(el);
                                    break;
                                }
                            }
                        }
                        return false;
                    }""",
                    {"tabName": tab_name, "tabLabels": list(PICKUP_TAB_LABELS)},
                )
            )
        except Exception:
            return False

    def _step_send_limit(self, step: PriorityStep, initial_budget: int, sent: int) -> int:
        """このタブで送れる最大件数（残り回数・タブ上限の小さい方）."""
        remaining = max(0, initial_budget - sent)
        if step.max_members > 0:
            return min(step.max_members, remaining)
        return remaining

    def _tab_has_increase(self, page: Page, tab_name: str) -> bool:
        """タブ付近に増加・新着・数字バッジがあるか."""
        try:
            return bool(
                page.evaluate(
                    """(tabName) => {
                        const tabLabels = ['みたよ','マイガール','口コミ','キープ','マッチ率','ミテネ履歴'];
                        for (const row of document.querySelectorAll(
                            'ul, ol, nav, div, [class*="tab"], [class*="nav"]'
                        )) {
                            const items = [...row.querySelectorAll('a, li, span, button, div')];
                            const tabs = items.filter(el => {
                                const t = (el.innerText || '').trim();
                                return tabLabels.includes(t) || t.startsWith(tabName);
                            });
                            if (tabs.length < 3) continue;
                            for (const el of items) {
                                const t = (el.innerText || '').trim();
                                if (!t.includes(tabName)) continue;
                                if (/新着|NEW|増|↑|\\+/.test(t)) return true;
                                const nums = t.match(/\\d+/g);
                                if (nums && nums.some(n => parseInt(n, 10) > 0)) return true;
                                const sib = el.parentElement;
                                if (sib) {
                                    const block = (sib.innerText || '').trim();
                                    if (block.includes(tabName) && /\\d+/.test(block)) return true;
                                }
                            }
                        }
                        for (const el of document.querySelectorAll('a, button, li, span, [role="tab"]')) {
                            const t = (el.innerText || '').trim();
                            if (!t.includes(tabName)) continue;
                            if (/新着|NEW|増|↑|\\+/.test(t)) return true;
                            const nums = t.match(/\\d+/g);
                            if (nums && nums.some(n => parseInt(n, 10) > 0)) return true;
                        }
                        return false;
                    }""",
                    tab_name,
                )
            )
        except Exception:
            return False

    def _step_should_run(
        self, page: Page, step: PriorityStep, *, after_open: bool = False
    ) -> bool:
        cond = step.condition or "always"
        if cond == "always":
            return True
        if cond == "if_new_exists":
            if after_open:
                n = self._count_new_members_on_page(page)
            else:
                if not self._open_step_list(page, step):
                    return False
                n = self._count_new_members_on_page(page)
            if n > 0:
                logger.info("【%s】未送信会員 %d 人 → 送信対象", step.tab, n)
                return True
            logger.info("【%s】未送信会員なしのためスキップ", step.tab)
            return False
        if cond == "if_no_match_new":
            if self._match_rate_had_new is None:
                logger.info("【%s】マッチ率の新規判定前のためスキップ", step.tab)
                return False
            if self._match_rate_had_new:
                logger.info(
                    "【%s】マッチ率に新規があるためスキップ", step.tab
                )
                return False
            logger.info("【%s】マッチ率新規なし → みたよを対象", step.tab)
            return True
        if cond == "increased":
            if self._tab_has_increase(page, step.tab):
                return True
            logger.info("タブ「%s」に増加表示がないためスキップ", step.tab)
            return False
        return True

    def _switch_tab(
        self, page: Page, tab_name: str, *, list_path: str = ""
    ) -> bool:
        logger.info("タブ切替（Pick Up横タブ）: %s", tab_name)
        page.evaluate("window.scrollTo(0, 0)")
        self._wait_page_settled(page)
        path = list_path or TAB_LIST_PATHS.get(tab_name, "")
        if self._is_on_step_list_page(page, path):
            logger.info("タブ「%s」は既に一覧表示中", tab_name)
            return True
        if self._is_active_tab(page, tab_name) and self._is_on_step_list_page(page, path):
            logger.info("タブ「%s」は既に選択中", tab_name)
            return True

        self._scroll_tab_bar(page)
        for surface in self._iter_surfaces(page):
            if self._click_pickup_list_tab(
                surface, tab_name, list_path=path
            ):
                self._wait_page_settled(page)
                self.human.tab_switch_pause()
                self.human.browse_list(page)
                if self._is_member_profile_page(page):
                    logger.warning(
                        "タブ「%s」クリック後に会員プロフィールへ遷移: %s",
                        tab_name,
                        page.url,
                    )
                    return False
                logger.info("タブ「%s」を切り替えました: %s", tab_name, page.url)
                return True
            loc = surface.get_by_text(tab_name, exact=True)
            for i in range(self._safe_count(loc)):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    tag = el.evaluate("el => el.tagName.toLowerCase()")
                    if tag != "a":
                        continue
                    href = el.get_attribute("href") or ""
                    if path and self._list_path_slug(path) not in href.lower():
                        continue
                    box = el.bounding_box()
                    vh = (page.viewport_size or {}).get("height", 844)
                    if box and box["y"] > vh * 0.35:
                        continue
                    el.scroll_into_view_if_needed(timeout=3000)
                    el.click(timeout=5000)
                    self._wait_page_settled(page)
                    self.human.tab_switch_pause()
                    if self._is_member_profile_page(page):
                        return False
                    logger.info("タブ「%s」をPlaywrightでクリック", tab_name)
                    return True
                except Exception:
                    continue

        logger.warning(
            "タブ「%s」が見つかりません: %s",
            tab_name,
            self._page_debug_hint(page),
        )
        self._save_debug_screenshot(page, f"tab-{tab_name}")
        return False

    def _activate_sub_tab(self, page: Page, sub_name: str) -> bool:
        """マッチ率内の「新規」などサブタブを開く."""
        logger.info("サブタブ: %s", sub_name)
        for surface in self._iter_surfaces(page):
            tablist = surface.locator('[role="tablist"]')
            if tablist.count() > 0:
                sub = tablist.last.get_by_text(sub_name, exact=True)
                if sub.count() > 0:
                    self.human.human_click(page, sub.first)
                    self.human.action_pause()
                    return True
            sub_pat = re.compile(rf"^{re.escape(sub_name)}$")
            for loc in (
                surface.get_by_role("tab", name=sub_pat),
                surface.locator('[class*="tab"], nav').get_by_text(sub_name, exact=True),
                surface.get_by_text(sub_name, exact=True),
            ):
                if loc.count() == 0:
                    continue
                for i in range(loc.count()):
                    el = loc.nth(i)
                    try:
                        label = (el.inner_text(timeout=500) or "").strip()
                    except Exception:
                        label = ""
                    if len(label) > 12:
                        continue
                    self.human.human_click(page, el)
                    self.human.action_pause()
                    return True
            if self._click_tab_via_js(surface, sub_name, top_ratio=0.72):
                self.human.action_pause()
                return True
        logger.warning("サブタブ「%s」が見つかりません", sub_name)
        return False

    def _step_label(self, step: PriorityStep) -> str:
        base = step.tab
        mode = step.member_filter or "sendable"
        if mode == "new_only":
            base += "（未送信）"
        elif mode == "sent_oldest_first":
            base += "（履歴あり・古い順）"
        if step.sub_tab:
            return f"{base}/{step.sub_tab}"
        return base

    def zero_send_message(self) -> str:
        r = self._last_run_report
        parts: list[str] = ["送信0件（ミテネ回数は減りません）。"]
        if r.get("budget"):
            parts.append(f"残り回数: {r['budget']}回")
        if r.get("note"):
            parts.append(r["note"])
        return " ".join(parts)

    def _send_loop_for_step(
        self,
        page: Page,
        label: str,
        initial_budget: int,
        sent: int,
        sent_by_step: dict[str, int],
        step_limit: int,
    ) -> int:
        sent_by_step.setdefault(label, 0)
        if step_limit <= 0:
            return sent
        step_sent = 0
        empty_streak = 0
        logger.info(
            "%s: 表示会員へミテネ（残り回数あと %d 回まで）",
            label,
            step_limit,
        )
        scroll_rounds = 0
        stall = 0
        failed_attempts = 0
        max_failed = max(step_limit * 3, 15)
        while sent < initial_budget and step_sent < step_limit:
            if failed_attempts >= max_failed:
                logger.warning(
                    "%s: 失敗が %d 回に達したため中断",
                    label,
                    failed_attempts,
                )
                break
            if not self._send_button_queue:
                scroll_rounds += 1
                if scroll_rounds > self.standard.max_scroll_rounds:
                    break
                self._scroll_member_list(page)
                if self._refresh_send_button_queue(page) == 0:
                    stall += 1
                    if stall >= 3:
                        logger.info("%s: これ以上「ミテネを送る」がありません", label)
                        break
                    continue
                stall = 0
                continue
            if not self._send_one_mitene(page):
                failed_attempts += 1
                stall += 1
                if stall >= 5:
                    self._scroll_member_list(page)
                    self._refresh_send_button_queue(page, log_scan=False)
                    stall = 0
                continue
            failed_attempts = 0
            stall = 0
            scroll_rounds = 0
            sent += 1
            step_sent += 1
            sent_by_step[label] += 1
            self._send_done = sent
            logger.info(
                "1件送信完了（%d / %d・%s %d/%d）",
                sent,
                initial_budget,
                label,
                step_sent,
                step_limit,
            )
            self.human.after_send_pause()
            if sent < initial_budget and step_sent < step_limit:
                self.human.between_members_pause()
        return sent

    def _page_has_send_targets(self, page: Page) -> bool:
        if self._count_mitene_send_buttons(page) > 0:
            return True
        return len(self._scan_member_cards(page)) > 0

    def _wait_for_step_members(
        self, page: Page, step: PriorityStep | None, *, timeout_ms: int = 15000
    ) -> bool:
        """ステップ条件に合う会員が DOM に出るまで待つ."""
        deadline = time.monotonic() + timeout_ms / 1000
        mode = (step.member_filter if step else "sendable") or "sendable"
        while time.monotonic() < deadline:
            members = _member_dicts_only(self._scan_member_cards(page))
            if members:
                if mode == "new_only":
                    if any(not m.get("sent_history") for m in members):
                        return True
                    return True
                if mode == "sent_oldest_first":
                    if any(m.get("sent_history") for m in members):
                        return True
                    return True
                return True
            if self._count_mitene_send_buttons(page) > 0:
                return True
            page.wait_for_timeout(400)
        return False

    def _kitene_send_wait_ms(self) -> int:
        return 3500 if self.human.fast_send else 5000

    def _kitene_button_locator(self, page: Page, member_id: str) -> Locator:
        return page.locator(
            f".js-regist_comeon_{member_id} a, "
            f".js-regist_comeon_{member_id}, "
            f".u_{member_id} .kitene_send_btn a, "
            f".u_{member_id} a.kitene_send_btn__text_wrapper, "
            f'a[onclick*="registComeon({member_id})"]'
        )

    def _history_has_sent_date(self, history_text: str) -> bool:
        """ミテネ履歴の値に送信日・送信済がある."""
        text = (history_text or "").strip()
        if not text:
            return False
        return bool(MITENE_HISTORY_SENT_VALUE.search(text))

    def _parse_history_date(self, history_text: str) -> date | None:
        """ミテネ履歴から送信日を抽出（古い順ソート用）."""
        text = _normalize_digits(history_text or "")
        m = re.search(
            r"(\d{4})[/.\-年](\d{1,2})[/.\-月]?(\d{1,2})?",
            text,
        )
        if not m:
            return None
        y, mo = int(m.group(1)), int(m.group(2))
        d = int(m.group(3) or 1)
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    def _invalidate_list_cache(self) -> None:
        self._cached_list_cards = None
        self._cached_list_url = ""

    def _count_member_cards_on_surface(self, surface: Any) -> int:
        try:
            return int(surface.evaluate(MEMBER_CARD_COUNT_JS))
        except Exception:
            return 0

    def _member_card_surface(self, page: Page) -> Any:
        """会員カードが最も多く見つかる frame / page を返す."""
        best = page
        best_n = self._count_member_cards_on_surface(page)
        for surface in self._iter_surfaces(page):
            if surface is page:
                continue
            n = self._count_member_cards_on_surface(surface)
            if n > best_n:
                best_n = n
                best = surface
        return best

    def _log_member_card_selector_debug(
        self, surface: Any, tab_name: str
    ) -> None:
        try:
            info = surface.evaluate(MEMBER_CARD_DEBUG_JS)
        except Exception as e:
            logger.warning("【%s一覧】カードセレクタ診断失敗: %s", tab_name, e)
            return
        if not isinstance(info, dict):
            return
        logger.warning(
            "【%s一覧】会員カード 0 件 — anchor=%s sendBtn=%s hits=%s",
            tab_name,
            info.get("anchorCount"),
            info.get("sendBtnCount"),
            info.get("selectorHits"),
        )
        for chain in info.get("parentChains") or []:
            logger.warning("【%s一覧】親要素チェーン: %s", tab_name, chain)

    def _tab_name_from_page(self, page: Page) -> str:
        u = (page.url or "").lower()
        if "comeonmygirllist" in u:
            return "マイガール"
        if "comeonkeeplist" in u:
            return "キープ"
        if "comeonaimatchinglist" in u:
            return "マッチ率"
        if "comeonvisitorlist" in u:
            return "みたよ"
        if self._current_step:
            return self._current_step.tab
        return "一覧"

    def _member_has_sent_mitene(self, history_text: str) -> bool:
        """
        kitene_question 内の「ミテネ履歴」answer に「送信済」があれば送信済み。
        例: 2026/05/15 送信済
        """
        return not is_new_member_from_history(history_text)

    def _card_has_sent_history(
        self, card_text: str, history_text: str = ""
    ) -> bool:
        """一覧DOMのミテネ履歴（readHistory）で送信済み判定."""
        ht = (history_text or "").strip()
        if ht:
            return self._member_has_sent_mitene(ht)
        label = self.standard.mitene_history_label
        if label in (card_text or "") and "送信済" in (card_text or ""):
            return True
        return False

    def _log_send_pipeline_info(self) -> None:
        logger.info("=== ミテネ送信パイプライン ===")
        logger.info("対象抽出: _parse_list_page_cards → _apply_step_member_filter")
        logger.info(
            "送信順決定: フェーズ1（①〜⑤）→ フェーズ2（全件合算）"
            " / マイガール=キープ経由タブクリック"
        )
        logger.info(
            "新規会員判定: div.kitene_question > span.question「ミテネ履歴」"
            "の span.answer に「送信済」なし"
        )
        logger.info(
            "gid=%s キープ起点=%s",
            self._gid(),
            build_list_url(self._gid(), KEEP_LIST_PATH),
        )
        logger.info("ロジック版: %s", SEND_LOGIC_VERSION)

    def _member_send_verdict(
        self, sent_history: bool, *, step: PriorityStep | None
    ) -> str:
        mode = (step.member_filter if step else "sendable") or "sendable"
        if mode == "sent_oldest_first":
            return "送信対象" if sent_history else "除外"
        if mode == "new_only":
            return "除外" if sent_history else "送信対象"
        return "送信対象"

    def _log_list_cards_debug(
        self,
        cards: list[dict[str, Any]],
        tab_name: str,
        step: PriorityStep | None,
    ) -> None:
        logger.info(
            "【%s一覧】カード解析 %d 件（一覧DOM・LazyLoad後）",
            tab_name,
            len(cards),
        )
        for card in cards:
            if not isinstance(card, dict):
                continue
            sent_history = bool(card.get("sent_history"))
            verdict = self._member_send_verdict(sent_history, step=step)
            hist_label = "履歴あり" if sent_history else "履歴なし"
            logger.info("[%s]", tab_name)
            logger.info("%s", card.get("name") or "（名前不明）")
            logger.info("uid=%s", card.get("uid") or card.get("mid") or "-")
            logger.info("%s", hist_label)
            logger.info("%s", verdict)
            logger.info("innerText=%s", card.get("card_text") or "")
            logger.info(
                "innerHTML(先頭500)=%s",
                card.get("card_html_head") or "",
            )

    def _log_tab_parse_summary(
        self,
        tab_name: str,
        cards: list[dict[str, Any]],
        step: PriorityStep | None,
    ) -> None:
        total = len(cards)
        unsent = sum(
            1 for c in cards if isinstance(c, dict) and not c.get("sent_history")
        )
        sent_hist = sum(
            1 for c in cards if isinstance(c, dict) and c.get("sent_history")
        )
        with_btn = sum(
            1 for c in cards if isinstance(c, dict) and c.get("has_send_button")
        )
        targets = 0
        for c in cards:
            if not isinstance(c, dict) or not c.get("has_send_button"):
                continue
            if self._member_send_verdict(
                bool(c.get("sent_history")), step=step
            ) == "送信対象":
                targets += 1
        stats = {
            "tab": tab_name,
            "total": total,
            "unsent": unsent,
            "sent_history": sent_hist,
            "with_send_button": with_btn,
            "send_targets": targets,
        }
        self._last_tab_parse_stats = stats
        logger.info(
            "【%s】取得 %d 件 / 未送信 %d / 送信履歴あり %d / "
            "送信ボタン %d / 送信対象 %d 件",
            tab_name,
            total,
            unsent,
            sent_hist,
            with_btn,
            targets,
        )

    def _parse_list_page_cards(
        self, page: Page, tab_name: str
    ) -> list[dict[str, Any]]:
        """会員一覧ページ上で全カード解析（プロフィールへ遷移しない）."""
        if self._is_member_profile_page(page):
            logger.warning(
                "プロフィールページのため一覧解析をスキップ: %s",
                page.url,
            )
            return []

        url = page.url or ""
        if self._cached_list_url == url and self._cached_list_cards is not None:
            return [
                c for c in self._cached_list_cards if isinstance(c, dict)
            ]

        self._scroll_member_list_to_end(page)
        history_label = self.standard.mitene_history_label
        step = self._current_step
        surface = self._member_card_surface(page)

        try:
            raw = surface.evaluate(
                MEMBER_CARD_PARSE_JS,
                {"historyLabel": history_label},
            )
        except Exception as e:
            logger.warning("【%s一覧】カード解析失敗: %s", tab_name, e)
            return []

        rows = _normalize_evaluate_rows(raw)
        if not rows and raw is not None:
            logger.warning(
                "【%s一覧】evaluate 戻り値が不正: type=%s",
                tab_name,
                type(raw).__name__,
            )

        parsed: list[dict[str, Any]] = []
        for item in rows:
            card_text = str(item.get("cardText") or "")
            history_text = str(item.get("historyText") or "").strip()
            sent_history = self._card_has_sent_history(card_text, history_text)
            mid = str(item.get("mid") or "").strip()
            uid = str(item.get("uid") or "").strip()
            member_id = mid or uid
            parsed.append(
                {
                    "name": str(item.get("name") or "（名前不明）"),
                    "uid": uid,
                    "mid": member_id,
                    "key": f"comeon-{member_id}" if member_id else "",
                    "card_text": card_text,
                    "card_html_head": str(item.get("cardHtmlHead") or ""),
                    "history_text": history_text,
                    "match_rate": str(item.get("matchRate") or "").strip(),
                    "sent_history": sent_history,
                    "has_send_button": bool(item.get("hasSendButton")),
                    "is_new": not sent_history,
                    "has_history_row": sent_history,
                    "history_date": (
                        self._parse_history_date(history_text)
                        or self._parse_history_date(card_text)
                        if sent_history
                        else None
                    ),
                }
            )

        self._log_list_cards_debug(parsed, tab_name, step)
        self._log_tab_parse_summary(parsed, tab_name, step)
        if not parsed:
            self._log_member_card_selector_debug(surface, tab_name)
        self._cached_list_cards = parsed
        self._cached_list_url = url
        return parsed

    def _cards_to_member_queue(
        self, cards: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """解析済みカード → 送信キュー用メンバー dict."""
        out: list[dict[str, Any]] = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            key = str(card.get("key") or "")
            if not key.startswith("comeon-"):
                continue
            if key in self._sent_member_keys or key in self._failed_member_keys:
                continue
            if not card.get("has_send_button"):
                continue
            out.append(dict(card))
        return out

    def _scan_member_cards(self, page: Page) -> list[dict[str, Any]]:
        """会員カードを走査（一覧DOM・送信済=「送信済」in card_text）."""
        if self._is_member_profile_page(page):
            return []
        if self._is_on_comeon_list_page(page):
            tab = self._tab_name_from_page(page)
            cards = self._parse_list_page_cards(page, tab)
            members = self._cards_to_member_queue(cards)
            if members:
                return members
        return self._scan_send_buttons_fallback(page)

    def _scan_send_buttons_fallback(self, page: Page) -> list[dict[str, Any]]:
        """カード構造で取れないとき registComeon / js-regist_comeon から ID 取得."""
        surface = self._member_card_surface(page)
        try:
            raw = surface.evaluate(
                """() => {
                    const out = [];
                    const seen = new Set();
                    const add = (mid) => {
                        if (!mid || seen.has(mid)) return;
                        seen.add(mid);
                        out.push({
                            key: 'comeon-' + mid,
                            mid: String(mid),
                            historyText: '',
                            hasHistoryRow: false,
                        });
                    };
                    for (const el of document.querySelectorAll('[class*="js-regist_comeon_"]')) {
                        for (const c of el.classList) {
                            if (c.startsWith('js-regist_comeon_')) {
                                add(c.replace('js-regist_comeon_', ''));
                            }
                        }
                    }
                    for (const el of document.querySelectorAll(
                        '[onclick*="registComeon"], a, button, .kitene_send_btn'
                    )) {
                        const t = (el.innerText || '').replace(/\\s+/g, ' ');
                        if (!/ミテネを送る|ミテネする/.test(t)) continue;
                        if (el.closest('.kitene_send_zumi_btn')) continue;
                        const wrap = el.closest('.kitene_send_btn');
                        if (wrap && (wrap.innerText || '').includes('送信済')) continue;
                        const oc = el.getAttribute('onclick')
                            || wrap?.getAttribute('onclick') || '';
                        const m = oc.match(/registComeon\\((\\d+)\\)/);
                        if (m) add(m[1]);
                    }
                    return out;
                }"""
            )
        except Exception:
            return []
        result: list[dict[str, Any]] = []
        for item in _normalize_evaluate_rows(raw):
            key = str(item.get("key", ""))
            if not key.startswith("comeon-") or key in self._sent_member_keys:
                continue
            result.append(
                {
                    "key": key,
                    "mid": str(item.get("mid", key[7:])),
                    "history_text": "",
                    "has_history_row": False,
                    "sent_history": False,
                    "history_date": None,
                    "is_new": True,
                    "has_send_button": True,
                }
            )
        if result:
            logger.info("カード走査フォールバック: %d 人検出", len(result))
        return result

    def _sort_members_oldest_first_keys(
        self, members: list[dict[str, Any]]
    ) -> list[str]:
        """後方互換: 全会員を送信日古い順でキー列にする."""
        return self._sort_all_members_oldest_first_keys(members)

    def _apply_step_member_filter(
        self, members: list[dict[str, Any]], step: PriorityStep
    ) -> list[str]:
        """タブごとの会員条件（未送信のみ / 送信日古い順 / 全員）."""
        members = [m for m in members if isinstance(m, dict)]
        mode = step.member_filter or "sendable"
        if mode == "new_only":
            filtered = [
                m for m in members if not m.get("sent_history")
            ]
            logger.info(
                "【%s】新規会員（ミテネ履歴に「送信済」なし）: %d / %d 人",
                step.tab,
                len(filtered),
                len(members),
            )
            if not filtered and members:
                logger.info("【%s】未送信会員 0 人", step.tab)
            return [m["key"] for m in filtered]
        if mode == "sent_oldest_first":
            keys = self._sort_all_members_oldest_first_keys(members)
            logger.info(
                "【%s】全会員（古い順・同日ランダム）: %d / %d 人",
                step.tab,
                len(keys),
                len(members),
            )
            return keys
        return [m["key"] for m in members]

    def _merge_member_card(
        self, merged: dict[str, dict[str, Any]], card: dict[str, Any]
    ) -> None:
        """合算キュー用: 同一会員はより古い送信日のカードを優先."""
        key = str(card.get("key") or "")
        if not key:
            return
        existing = merged.get(key)
        if not existing:
            merged[key] = card
            return
        d_new = card.get("history_date") or OLDEST_SORT_DEFAULT_DATE  # type: ignore[assignment]
        d_old = existing.get("history_date") or OLDEST_SORT_DEFAULT_DATE  # type: ignore[assignment]
        if d_new < d_old:
            merged[key] = card

    def _send_aggregated_oldest_fallback(
        self,
        page: Page,
        budget: int,
        sent: int,
        sent_by_step: dict[str, int],
        skipped_steps: list[str],
    ) -> int:
        """新規0件時: 全タブの会員を合算し送信日古い順にミテネ送信."""
        label = "全タブ合算（送信日古い順）"
        if sent >= budget:
            return sent

        logger.info(
            "★ 新規会員が全タブに存在しないため %s（gid=%s）",
            label,
            self._gid(),
        )
        merged: dict[str, dict[str, Any]] = {}
        for tab_name, list_path in AGGREGATE_LIST_TABS:
            step = PriorityStep(
                tab=tab_name,
                list_path=list_path,
                member_filter="sendable",
            )
            cards = self._fetch_tab_members(page, step)
            if not cards:
                logger.warning("【%s】合算取得をスキップ", tab_name)
                continue
            for card in cards:
                if not isinstance(card, dict) or not card.get("has_send_button"):
                    continue
                if card.get("key") in self._sent_member_keys:
                    continue
                self._merge_member_card(merged, card)

        keys = self._sort_all_members_oldest_first_keys(list(merged.values()))
        if not keys:
            logger.info("合算フォールバック: 送信対象なし")
            skipped_steps.append(f"{label}（対象なし）")
            return sent

        logger.info("合算フォールバック: %d 人 → 送信開始", len(keys))
        return self._send_member_keys_phase(
            page, label, keys, budget, sent, sent_by_step
        )

    def _count_new_members_on_page(self, page: Page) -> int:
        """未送信会員数（一覧DOM・送信ボタンあり）."""
        if self._is_on_comeon_list_page(page) and not self._is_member_profile_page(
            page
        ):
            tab = self._tab_name_from_page(page)
            cards = self._parse_list_page_cards(page, tab)
            return sum(
                1
                for c in cards
                if isinstance(c, dict)
                and not c.get("sent_history")
                and c.get("has_send_button")
            )
        return sum(
            1
            for m in _member_dicts_only(self._scan_member_cards(page))
            if not m.get("sent_history")
        )

    def _scan_unsent_member_keys(self, page: Page) -> list[str]:
        """一覧を走査し、現在ステップ条件に合う会員IDリストを返す."""
        members = _member_dicts_only(self._scan_member_cards(page))
        step = self._current_step
        if step and step.member_filter != "sendable":
            keys = self._apply_step_member_filter(members, step)
        else:
            keys = [m["key"] for m in members if m.get("key")]
        if not self.standard.priority_steps:
            keys = self._filter_member_queue(keys)
        return keys

    def _kitene_member_send_state(self, page: Page, member_id: str) -> str:
        """デバッグ用: 会員カードの送信ボタン状態."""
        try:
            return (
                page.evaluate(
                    """(mid) => {
                        const wrap = document.querySelector(
                            '.js-regist_comeon_' + mid + ', .kitene_send_btn.js-regist_comeon_' + mid
                        );
                        if (!wrap) return 'wrapなし';
                        const t = (wrap.innerText || '').replace(/\\s+/g, ' ').trim();
                        const zumi = wrap.querySelector('.kitene_send_zumi_btn');
                        const zs = zumi ? getComputedStyle(zumi) : null;
                        const zumiOn = zumi && zs && zs.display !== 'none' && zs.visibility !== 'hidden';
                        return [
                            wrap.classList.contains('active') ? 'active' : 'no-active',
                            zumiOn ? 'zumi表示' : 'zumi非表示',
                            t.includes('送信済') ? '送信済テキスト' : '',
                        ].filter(Boolean).join(',') || '不明';
                    }""",
                    member_id,
                )
                or "不明"
            )
        except Exception:
            return "取得失敗"

    def _click_overlay_confirm(self, page: Page) -> str | None:
        """ポップアップ内の確認ボタン（モーダル内の kitene_send_btn も押す）."""
        labels = list(self.standard.confirm_buttons) + ["送信", "はい"]
        try:
            return page.evaluate(
                """(labels) => {
                    const modalSel =
                        '#colorbox, #cboxContent, #cboxLoadedContent, #TB_window, #TB_ajaxContent, '
                        + '.remodal-wrapper, .popup, [class*="modal"], [class*="popup"], [role="dialog"]';
                    const skipListOnly = (el) => !!el.closest(
                        '.user_ranking_box, li.user_ranking_box, .user_ranking_list'
                    );
                    const roots = [...document.querySelectorAll(modalSel)];
                    if (!roots.length) return null;
                    for (const root of roots) {
                        for (const el of root.querySelectorAll(
                            'a, button, input[type="button"], input[type="submit"], '
                            + '[role="button"], .kitene_send_btn__text_wrapper, .kitene_send_btn a'
                        )) {
                            if (skipListOnly(el)) continue;
                            const t = (el.innerText || el.value || '').trim();
                            if (!labels.some(l => t === l || t.startsWith(l))) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width < 36 || r.height < 18 || !el.offsetParent) continue;
                            el.click();
                            return t;
                        }
                    }
                    return null;
                }""",
                labels,
            )
        except Exception:
            return None

    def _wait_confirm_layer(self, page: Page, timeout_ms: int = 5000) -> bool:
        for sel in (
            "#colorbox",
            "#cboxContent",
            "#TB_window",
            ".remodal-wrapper",
            '[role="dialog"]',
        ):
            try:
                page.wait_for_selector(sel, state="visible", timeout=timeout_ms)
                return True
            except Exception:
                continue
        return False

    def _tap_mitene_cta(self, page: Page, member_id: str) -> bool:
        """一覧CTAをタップ（Playwright click → カード内 click → registComeon）."""
        btn = self._kitene_button_locator(page, member_id)
        if self._safe_count(btn) > 0:
            el = btn.first
            try:
                el.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            try:
                el.click(timeout=12000)
                return True
            except Exception:
                pass
        try:
            return bool(
                page.evaluate(
                    """(mid) => {
                        const roots = [
                            document.querySelector('.js-regist_comeon_' + mid),
                            document.querySelector('[class*="js-regist_comeon_' + mid + '"]'),
                            document.querySelector('.u_' + mid),
                        ].filter(Boolean);
                        for (const root of roots) {
                            const candidates = [
                                ...root.querySelectorAll(
                                    'a.kitene_send_btn__text_wrapper, .kitene_send_btn a, a, button'
                                ),
                                root,
                            ];
                            for (const el of candidates) {
                                const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                                if (t && !/ミテネを送る|ミテネする/.test(t)) continue;
                                if (el.closest && el.closest('.kitene_send_zumi_btn')) continue;
                                el.scrollIntoView?.({ block: 'center', inline: 'nearest' });
                                if (typeof el.click === 'function') {
                                    el.click();
                                    return true;
                                }
                            }
                        }
                        if (typeof registComeon === 'function') {
                            registComeon(Number(mid));
                            return true;
                        }
                        return false;
                    }""",
                    member_id,
                )
            )
        except Exception:
            return False

    def _send_mitene_to_member(self, page: Page, key: str) -> bool:
        """会員1人へミテネ送信（クリック → 確認 → 残り回数 or 送信済み表示）."""
        if not key.startswith("comeon-"):
            return False
        member_id = key[7:]
        wait_ms = max(self._kitene_send_wait_ms(), 6000)
        remaining_before = self._parse_remaining_count(page)
        btn = self._kitene_button_locator(page, member_id)
        try:
            if self._safe_count(btn) > 0 and self._safe_is_visible(btn.first):
                btn.first.scroll_into_view_if_needed(timeout=5000)
                btn.first.click(timeout=10000, force=True)
            else:
                page.evaluate(
                    """(mid) => {
                        if (typeof registComeon === 'function') registComeon(Number(mid));
                    }""",
                    member_id,
                )
            page.wait_for_timeout(600)
            overlay = self._click_overlay_confirm(page)
            if overlay:
                logger.debug("確認ポップアップ: %s", overlay)
            else:
                self._confirm_send_dialog(page)
            page.wait_for_timeout(400)
            self._click_overlay_confirm(page)

            remaining_after = self._parse_remaining_count(page)
            if (
                remaining_before is not None
                and remaining_after is not None
                and remaining_after < remaining_before
            ):
                logger.info(
                    "送信成功 %s（残り %d → %d）",
                    key,
                    remaining_before,
                    remaining_after,
                )
                return True

            if self._wait_kitene_send_result(page, member_id, timeout_ms=wait_ms):
                return True

            logger.info(
                "送信未完了 %s（状態: %s・残り %s→%s）",
                key,
                self._kitene_member_send_state(page, member_id),
                remaining_before,
                remaining_after,
            )
        except Exception as e:
            logger.debug("送信例外 %s: %s", e)
        return False

    def _send_mitene_standard(self, page: Page) -> int:
        logger.info("②ホームでミテネ残り回数を取得")
        budget = self._read_send_budget(page)
        steps = self.standard.priority_steps or list(DEFAULT_PRIORITY_STEPS)
        skipped_steps: list[str] = []

        if self.dry_run:
            if steps:
                for step in steps:
                    cap = step.max_members if step.max_members > 0 else "残り全部"
                    logger.info("ドライラン: 【%s】最大%s人", step.tab, cap)
                    self._open_step_list(page, step)
            else:
                self._open_find_members(page)
            return 0

        self._sent_member_keys.clear()
        self._failed_member_keys.clear()
        self._send_button_queue.clear()
        self._load_member_send_history()
        self._send_target = 0
        self._send_done = 0
        self._match_rate_had_new = None
        self._pipeline_had_new_member = False
        self._current_step = None
        sent = 0
        sent_by_step: dict[str, int] = {}
        self._dismiss_optional_popups(page)

        if steps:
            self._send_target = budget
            self._log_send_pipeline_info()
            logger.info("送信予算: %d 回（gid=%s）", budget, self._gid())
            sent = self._execute_phased_send_pipeline(
                page, budget, sent, sent_by_step, skipped_steps
            )
            target = budget
            self._send_target = budget
        else:
            target = budget
            self._open_find_members(page)
            list_remaining = self._parse_remaining_count(page)
            if list_remaining is not None:
                logger.info("一覧画面のミテネ残り回数: %d", list_remaining)
                if list_remaining <= 0:
                    raise DailyLimitReached("ミテネ残り回数が 0 です。")
                target = min(target, list_remaining)
            self._send_target = target
            queue_n = self._refresh_send_button_queue(page)
            if queue_n == 0:
                raise RuntimeError(
                    "会員一覧に「ミテネを送る」がありません。"
                    "本日分はすでに送った会員ばかりか、残り回数が0の可能性があります。"
                    f"{self._page_debug_hint(page)}"
                )
            logger.info(
                "③「ミテネを送る」を %d 回送り切るまで実行（送れる会員 %d 人）",
                target,
                queue_n,
            )
            scroll_rounds = 0
            stall = 0
            failed_attempts = 0
            max_failed = min(target + 15, 35)
            while sent < target:
                if failed_attempts >= max_failed:
                    logger.warning(
                        "送信失敗が %d 回に達したため中断（%d/%d 件）",
                        failed_attempts,
                        sent,
                        target,
                    )
                    break
                if not self._send_button_queue:
                    scroll_rounds += 1
                    if scroll_rounds > self.standard.max_scroll_rounds:
                        break
                    if not self._recover_more_send_buttons(page):
                        stall += 1
                        if stall >= 3:
                            break
                        continue
                    stall = 0
                    continue
                if not self._send_one_mitene(page):
                    failed_attempts += 1
                    stall += 1
                    if stall >= 5 and scroll_rounds <= self.standard.max_scroll_rounds:
                        scroll_rounds += 1
                        self._recover_more_send_buttons(page)
                        stall = 0
                    continue
                # キューが空になったら軽く補充（全62件ログは出さない）
                if len(self._send_button_queue) < 3:
                    self._refresh_send_button_queue(page, log_scan=False)
                failed_attempts = 0
                stall = 0
                scroll_rounds = 0
                sent += 1
                self._send_done = sent
                logger.info("1件送信完了（%d / %d）", sent, target)
                self.human.after_send_pause()
                try:
                    left_now = self._parse_remaining_count(page)
                    if left_now is not None:
                        logger.info("ミテネ残り回数（送信直後）: %d", left_now)
                except Exception:
                    pass
                if sent < target:
                    self.human.between_members_pause()
                if sent % 5 == 0 or sent >= target:
                    try:
                        left = self._parse_remaining_count(page)
                        if left is not None and left <= 0:
                            break
                    except Exception as e:
                        if not _is_destroyed_context_error(e):
                            raise

        note = ""
        if sent < budget:
            left = None
            try:
                left = self._parse_remaining_count(page)
            except Exception:
                pass
            if left is not None and left <= 0 and sent > 0:
                logger.info("ミテネ残り回数が 0 のため終了（%d 件送信）", sent)
                self._last_run_report = {
                    "budget": budget,
                    "sent": sent,
                    "sent_by_step": sent_by_step,
                    "skipped_steps": skipped_steps,
                    "note": f"{sent} 件送信し、残り回数を使い切りました。",
                }
                return sent
            note = (
                f"目標 {budget} 回のうち {sent} 回しか送れませんでした。"
                "送れる会員が足りない・本日すでに送済み・サイト側で拒否された可能性があります。"
            )
            if self._failed_member_keys:
                note += f"（送信できなかった会員: {len(self._failed_member_keys)} 人）"
            if self.standard.must_use_full_budget:
                self._last_run_report = {
                    "budget": budget,
                    "sent": sent,
                    "sent_by_step": sent_by_step,
                    "skipped_steps": skipped_steps,
                    "note": note,
                }
                raise RuntimeError(f"{note} {self._page_debug_hint(page)}")

        if sent == 0 and not note:
            hint = (
                "送れる会員が見つからないか、送信がすべて失敗しました。"
                "姫デコで残り回数と「ミテネできる会員を探す」一覧を確認してください。"
            )
            if skipped_steps:
                hint += f" スキップ: {', '.join(skipped_steps)}。"
            note = f"{hint}{self._page_debug_hint(page)}"

        self._last_run_report = {
            "budget": budget,
            "sent": sent,
            "sent_by_step": sent_by_step,
            "skipped_steps": skipped_steps,
            "note": note or f"{sent} 回送信しました。",
        }
        for label, count in sent_by_step.items():
            if count:
                logger.info("[%s]: %d 件", label, count)
        logger.info("合計 %d 件送信（目標 %d 回）", sent, budget)
        return sent

    def _dismiss_optional_popups(self, page: Page) -> None:
        for label in (
            "閉じる",
            "×",
            "キャンセル",
            "後で",
            "OK",
            "了解",
            "確認",
            "とじる",
        ):
            loc = page.get_by_text(label, exact=False)
            if self._safe_count(loc) > 0:
                try:
                    loc.first.click(timeout=2000)
                    page.wait_for_timeout(300)
                except Exception:
                    pass

    def _member_key(self, locator: Locator) -> str:
        try:
            return (locator.inner_text(timeout=1500) or "").strip()[:80]
        except Exception:
            return f"idx-{id(locator)}"

    def _looks_like_member_card(self, profile_text: str) -> bool:
        """会員プロフィール（タブバー全体のテキストと区別）."""
        if "マッチング率" not in profile_text:
            return False
        return "さん" in profile_text or "代・" in profile_text or "代" in profile_text

    def _locator_profile_text(self, locator: Locator) -> str:
        """「ミテネを送る」付近の会員カード（いちばん小さい親要素）."""
        try:
            return locator.evaluate(
                """el => {
                    let best = '';
                    let node = el;
                    for (let i = 0; i < 16 && node; i++, node = node.parentElement) {
                        const t = (node.innerText || '').trim();
                        if (!t.includes('ミテネ履歴') || !t.includes('マッチング率')) continue;
                        if (t.length > 2200) continue;
                        if (!t.includes('さん') && !t.includes('代')) continue;
                        if (!best || t.length < best.length) best = t;
                    }
                    if (best) return best;
                    const row = el.closest(
                        'li, article, tr, section, [class*="member"], [class*="user"], [class*="card"]'
                    );
                    return (row?.innerText || el.innerText || '').trim();
                }"""
            )
        except Exception:
            return ""

    def _mitene_history_value(self, profile_text: str) -> str:
        """会員カード内の「ミテネ履歴」値（空=未送信）。タブ名のミテネ履歴は無視."""
        label = self.standard.mitene_history_label
        if not self._looks_like_member_card(profile_text):
            return ""
        # カード内で「ミテネ履歴」の直後〜マッチング率まで（最大120文字）
        pattern = rf"{re.escape(label)}\s*([\s\S]{{0,160}}?)(?=マッチング率)"
        matches = list(re.finditer(pattern, profile_text))
        if matches:
            return matches[-1].group(1).strip()
        lines = [ln.strip() for ln in profile_text.splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            if label not in line:
                continue
            rest = line.replace(label, "").strip()
            if rest and not rest.startswith(("マッチ", "好き", "よく")):
                return rest
            if i + 1 < len(lines):
                nxt = lines[i + 1]
                if not any(
                    nxt.startswith(w)
                    for w in ("マッチング率", "好きなタイプ", "よく遊ぶ", "ミテネ")
                ):
                    return nxt
        return ""

    def _profile_already_sent_mitene(self, profile_text: str) -> bool:
        """会員カードにミテネ履歴の行があるか（行があれば履歴あり会員）."""
        label = self.standard.mitene_history_label
        if not self._looks_like_member_card(profile_text):
            return False
        if label not in profile_text:
            return False
        value = self._mitene_history_value(profile_text)
        # ラベルだけあって値が無い場合も「欄あり」とみなす
        return bool(value) or bool(
            re.search(rf"{re.escape(label)}\s*\n", profile_text)
        )

    def _member_key_from_profile(self, profile_text: str) -> str:
        for line in profile_text.splitlines():
            line = line.strip()
            if not line or line in self.MITENE_ACTION_TEXTS:
                continue
            if "さん" in line:
                return line[:80]
            if "代・" in line or re.search(r"\d+代", line):
                return line[:80]
        return profile_text[:80].replace("\n", " ")

    def _member_keys_match(self, a: str, b: str) -> bool:
        a, b = a.strip(), b.strip()
        if not a or not b:
            return False
        return a == b or a in b or b in a

    def _mitene_send_button_locator(self, page: Page) -> Locator:
        """姫デコ CTA: ♡ミテネを送る / registComeon."""
        return page.locator(
            ".kitene_send_btn a, "
            "a.kitene_send_btn__text_wrapper, "
            'a[onclick*="registComeon"], '
            'a:has-text("ミテネを送る"), '
            'button:has-text("ミテネを送る")'
        )

    def _has_mitene_history(self, locator: Locator) -> bool:
        text = self._locator_profile_text(locator)
        return self._profile_already_sent_mitene(text)

    def _scroll_member_list_to_end(self, page: Page, *, max_rounds: int = 50) -> int:
        """一覧を最下部までスクロールし、遅延読込分も DOM に載せる."""
        surface = self._member_card_surface(page)
        prev_count = 0
        stable = 0
        final_count = 0
        for _ in range(max_rounds):
            try:
                final_count = self._count_member_cards_on_surface(surface)
                page.evaluate(
                    "window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight))"
                )
                page.wait_for_timeout(550)
                if final_count == prev_count:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                prev_count = final_count
            except Exception:
                break
        try:
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(350)
        except Exception:
            pass
        logger.info("一覧を最下部までスクロール完了（会員カード %d 件）", final_count)
        return final_count

    def _scroll_member_list(self, page: Page) -> None:
        for _ in range(2):
            try:
                page.evaluate("window.scrollBy(0, window.innerHeight * 0.55)")
                page.wait_for_timeout(350)
            except Exception:
                break

    def _label_should_skip(self, label: str, active_context: str) -> bool:
        tab_nav_words = (
            "マイガール",
            "キープ",
            "マッチ率",
            "口コミ",
            "みたよ",
            "ランキング",
            "ミテネ履歴",
        )
        skip_words = (
            self.standard.find_members_button,
            "ミテネギフト",
            "ミテネ残り",
            "探す",
            "できる会員",
        )
        if any(w in label for w in skip_words):
            return True
        if any(w in label for w in tab_nav_words if w not in active_context):
            return True
        if label.strip() in tab_nav_words:
            return True
        return False

    def _visible_send_button_indices(self, page: Page) -> list[int]:
        indices: list[int] = []
        loc = self._mitene_send_button_locator(page)
        total = self._safe_count(loc)
        for i in range(total):
            try:
                btn = loc.nth(i)
                if not self._safe_is_visible(btn):
                    continue
                if btn.evaluate(
                    "el => !el.closest('.kitene_send_zumi_btn')"
                ):
                    indices.append(i)
            except Exception as e:
                if _is_destroyed_context_error(e):
                    break
                continue

        return indices

    def _mitene_send_succeeded(self, page: Page) -> bool:
        """送信完了トースト／ダイアログ用（一覧全体の「送信済」は見ない）."""
        for text in ("送信しました", "送りました", "送信完了"):
            if self._safe_count(page.get_by_text(text, exact=False)) > 0:
                return True
        return False

    def _wait_kitene_send_result(
        self, page: Page, member_id: str | None, *, timeout_ms: int = 4000
    ) -> bool:
        """registComeon クリック後、ボタンが送信済み表示になるまで待つ."""
        poll_ms = 120 if self.human.fast_send else 250
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if member_id:
                try:
                    done = page.evaluate(
                        """(mid) => {
                            const wrap = document.querySelector(
                                '.js-regist_comeon_' + mid + ', .kitene_send_btn.js-regist_comeon_' + mid
                            );
                            if (!wrap) return null;
                            const t = (wrap.innerText || '');
                            if (t.includes('送信済')) return true;
                            const zumi = wrap.querySelector('.kitene_send_zumi_btn');
                            if (zumi) {
                                const zs = getComputedStyle(zumi);
                                if (zs.display !== 'none' && zs.visibility !== 'hidden'
                                    && zumi.offsetParent) return true;
                            }
                            if (!wrap.classList.contains('active')) return true;
                            return false;
                        }""",
                        member_id,
                    )
                    if done is True:
                        return True
                except Exception:
                    pass
            elif self._mitene_send_succeeded(page):
                return True
            page.wait_for_timeout(poll_ms)
        return False

    def _advance_to_next_member(self, page: Page) -> None:
        try:
            page.evaluate("window.scrollBy(0, Math.min(window.innerHeight * 0.55, 380))")
            page.wait_for_timeout(180 if self.human.fast_send else 500)
        except Exception:
            pass

    def _send_one_mitene(self, page: Page) -> bool:
        """③ピンク「ミテネを送る」→ 確認ポップアップ → 残り回数が減るまで."""
        if not self._send_button_queue:
            keys = self._scan_unsent_member_keys(page)
            if not keys:
                return False
            key = keys[0]
        else:
            key = self._send_button_queue.pop(0)
        if key in self._sent_member_keys or key in self._failed_member_keys:
            return False
        if not key.startswith("comeon-"):
            return False
        member_id = key[7:]
        try:
            if self._is_member_profile_page(page):
                logger.warning(
                    "送信前にプロフィール検出 — 一覧へ戻してから再試行: %s",
                    page.url,
                )
                step = self._current_step
                if not step or not self._navigate_to_url_safe(
                    page, step, force_reload=True
                ):
                    return False
            remaining_before = self._parse_remaining_count(page)
            btn = self._kitene_button_locator(page, member_id)
            if self._safe_count(btn) == 0 or not self._safe_is_visible(btn.first):
                step = self._current_step
                if step and self._current_list_path:
                    self._navigate_to_url_safe(page, step, force_reload=True)
                    self._refresh_send_button_queue(page, log_scan=False)
                btn = self._kitene_button_locator(page, member_id)
            logger.info(
                "ミテネ送信 %s (残りキュー %d)",
                key,
                len(self._send_button_queue),
            )
            if not self._tap_mitene_cta(page, member_id):
                return False
            self.human.action_pause()
            page.wait_for_timeout(600)
            self._wait_confirm_layer(page, timeout_ms=4000)
            for _ in range(4):
                self._confirm_send_dialog(page)
                page.wait_for_timeout(800)
                remaining_after = self._parse_remaining_count(page)
                if (
                    remaining_before is not None
                    and remaining_after is not None
                    and remaining_after < remaining_before
                ):
                    logger.info(
                        "送信成功 %s（残り %d → %d）",
                        key,
                        remaining_before,
                        remaining_after,
                    )
                    self._mark_member_sent(key)
                    self._ensure_member_list_page(page)
                    return True
                if self._wait_kitene_send_result(page, member_id, timeout_ms=1200):
                    self._mark_member_sent(key)
                    self._ensure_member_list_page(page)
                    return True
            remaining_after = self._parse_remaining_count(page)
            if (
                remaining_before is not None
                and remaining_after is not None
                and remaining_after < remaining_before
            ):
                logger.info(
                    "送信成功 %s（残り %d → %d）",
                    key,
                    remaining_before,
                    remaining_after,
                )
                self._mark_member_sent(key)
                self._ensure_member_list_page(page)
                return True
            if self._wait_kitene_send_result(page, member_id, timeout_ms=5000):
                self._mark_member_sent(key)
                self._ensure_member_list_page(page)
                return True
            self._failed_member_keys.add(key)
            if len(self._failed_member_keys) <= 2:
                self._save_debug_screenshot(page, f"send-fail-{member_id}")
            logger.info(
                "送信未完了 %s（状態: %s・残り %s→%s）",
                key,
                self._kitene_member_send_state(page, member_id),
                remaining_before,
                remaining_after,
            )
            logger.info("送信できなかったため次へ (%s)", key)
        except Exception as e:
            if _is_destroyed_context_error(e):
                self._wait_page_settled(page, quick=True)
                self._ensure_member_list_page(page)
            logger.debug("タップ失敗: %s", e)
            self._failed_member_keys.add(key)
        return False

    def _click_send_on_detail(self, page: Page) -> bool:
        for text in self.MITENE_ACTION_TEXTS:
            btn = page.get_by_text(text, exact=False)
            if self._safe_count(btn) > 0:
                self.human.human_click(page, btn.first)
                self._wait_page_settled(page)
                return self._confirm_send_dialog(page)
        return False

    def _confirm_send_dialog(self, page: Page) -> bool:
        """確認ポップアップの「ミテネを送る」「送る」（モーダル内は kitene_send_btn も可）."""
        overlay = self._click_overlay_confirm(page)
        if overlay:
            logger.debug("確認ポップアップ: %s", overlay)
            return True
        for label in self.standard.confirm_buttons:
            if self._click_confirm_in_modal(page, label):
                logger.debug("確認ダイアログ: %s", label)
                return True
        for label in ("送る", "OK", "はい"):
            loc = page.locator(
                "#colorbox .kitene_send_btn a, #colorbox a, #colorbox button, "
                "#TB_window a, #TB_window button, "
                '[role="dialog"] a, [role="dialog"] button'
            ).get_by_text(label, exact=False)
            if self._safe_count(loc) > 0:
                try:
                    loc.last.click(timeout=8000)
                    return True
                except Exception:
                    pass
        return self._mitene_send_succeeded(page)

    def _click_confirm_in_modal(self, page: Page, label: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """(label) => {
                        const modalSel =
                            '#colorbox, #cboxContent, #cboxLoadedContent, #TB_window, #TB_ajaxContent, '
                            + '.remodal-wrapper, [class*="modal"], [role="dialog"]';
                        const skipListOnly = (el) => !!el.closest(
                            '.user_ranking_box, li.user_ranking_box, .user_ranking_list'
                        );
                        const roots = [...document.querySelectorAll(modalSel)];
                        if (!roots.length) return false;
                        for (const root of roots) {
                            for (const el of root.querySelectorAll(
                                'a, button, input[type="button"], input[type="submit"], '
                                + '.kitene_send_btn__text_wrapper, .kitene_send_btn a, span, div'
                            )) {
                                const t = (el.innerText || el.value || '').trim();
                                if (t !== label && !t.startsWith(label)) continue;
                                if (skipListOnly(el)) continue;
                                const r = el.getBoundingClientRect();
                                if (r.width < 36 || r.height < 18 || !el.offsetParent) continue;
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }""",
                    label,
                )
            )
        except Exception:
            return False

    def _send_mitene_gift(self, page: Page) -> None:
        g = self.gift
        logger.info("ミテネギフト送信手順: %s", g.menu_button_text)
        page.get_by_text(g.menu_button_text, exact=False).first.click()

        if g.image_alt:
            page.get_by_role("img", name=re.compile(g.image_alt)).first.click()
        else:
            page.locator("a img, button img").nth(g.image_index).click()

        if g.user_selection == "unsent_only":
            page.get_by_text("未送信ユーザーのみ選択", exact=False).first.click()
        elif g.user_selection == "bulk":
            page.get_by_text("一括選択", exact=False).first.click()

        page.get_by_text("次へ", exact=False).first.click()
        msg = g.message[:20]
        textarea = page.locator("textarea").first
        if textarea.count() > 0:
            textarea.fill(msg)
        page.get_by_text("プレビューを見る", exact=False).first.click()

        if self.dry_run:
            logger.info("ドライラン: ミテネギフトは送信しません")
            return

        page.get_by_text("ミテネギフトを送る", exact=False).first.click()
        page.wait_for_load_state("networkidle")

    def _record_sent(self, count: int, flow: str) -> None:
        record = {
            "date": date.today().isoformat(),
            "time": datetime.now().isoformat(timespec="seconds"),
            "status": "sent",
            "flow": flow,
            "count": count,
        }
        with self._sent_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _save_debug_screenshot(self, page: Page, tag: str) -> None:
        if not self.screenshot_on_error:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.log_dir / f"{tag}_{stamp}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            logger.info("デバッグ用スクリーンショット: %s", path)
        except Exception as e:
            logger.debug("スクリーンショット保存失敗: %s", e)

    def _save_error_screenshot(self, page: Page) -> None:
        self._save_debug_screenshot(page, "error")
