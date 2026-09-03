"""姫デコ ミテネ！自動送信（ログイン → 会員へ送信）."""

from __future__ import annotations

import itertools
import json
import logging
import os
import random
import re
import time
import traceback
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from human_behavior import HumanBehavior
from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright, sync_playwright

logger = logging.getLogger(__name__)

# 管理画面表示用（送信ルールの識別）
SEND_LOGIC_VERSION = "cta-click-modal-confirm"

# 会員一覧ホスト（gid=女の子ID をクエリに付与）
SPGIRL_LIST_HOST = "spgirl.cityheaven.net"
# 正規ログインURL（J1Main.php 等ではなく明示的に J1Login.php を開く）
SPGIRL_LOGIN_URL = "https://spgirl.cityheaven.net/J1Login.php"
# ログイン画面を開く前の待機（秒）
LOGIN_PRE_OPEN_WAIT_SEC = (2, 2)
# ログイン送信後の待機（秒）— ページが安定するまで
LOGIN_POST_SUBMIT_WAIT_SEC = 2
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
        "headings": (
            "あなたをキープした会員",
            "キープしている会員",
            "キープ会員",
            "キープした会員",
        ),
    },
    "マッチ率": {
        "slug": "comeonaimatchinglist",
        "headings": (
            "マッチ率が高い会員",
            "マッチ率の高い",
            "AIマッチング",
            "相性の良い会員",
        ),
    },
}

# 全角数字・コロン、改行挟み、「20回」にも対応（本文全体では findall + max で誤った 0 を避ける）
MITENE_REMAINING_PATTERN = re.compile(
    r"ミテネ残り回数\s*[：:：]?\s*([0-9０-９]+)\s*回?",
    re.MULTILINE,
)
# CTA 付近のブロック内のみ。「残り回数 : 20 / 20」形式のフォールバック用
REMAINING_SLASH_PATTERN = re.compile(
    r"残り回数\s*[：:]\s*([0-9０-９]+)\s*/\s*([0-9０-９]+)",
    re.MULTILINE,
)
# 後方互換（他モジュールから参照される場合）
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
    return pruneNestedCards(cards);
};
const pruneNestedCards = (cards) => {
    if (cards.length < 2) return cards;
    return cards.filter((card, i) => {
        for (let j = 0; j < cards.length; j++) {
            if (i === j) continue;
            try {
                if (cards[j] !== card && cards[j].contains(card)) return false;
            } catch (_) {}
        }
        return true;
    });
};
const countSelectorHits = () => {
    const hits = {};
    let total = 0;
    for (const sel of MEMBER_CARD_ROOT_SELECTORS) {
        try {
            const n = document.querySelectorAll(sel).length;
            hits[sel] = n;
            total += n;
        } catch (_) {
            hits[sel] = -1;
        }
    }
    return { hits, total };
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
        const wrap = card.querySelector('.kitene_send_btn');
        if (wrap && !wrap.classList.contains('kitene_send_zumi_btn')) {
            const zumi = wrap.querySelector('.kitene_send_zumi_btn');
            let zumiVisible = false;
            if (zumi) {
                const zs = getComputedStyle(zumi);
                zumiVisible = zs.display !== 'none' && zs.visibility !== 'hidden'
                    && zumi.offsetParent;
            }
            const wt = (wrap.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!zumiVisible && /ミテネ/.test(wt) && !wt.includes('送信済')) {
                const r = wrap.getBoundingClientRect();
                if (r.width >= 40 && r.height >= 16 && wrap.offsetParent) return true;
            }
        }
        for (const el of card.querySelectorAll(
            '.kitene_send_btn, a, button, [onclick*="registComeon"]'
        )) {
            if (el.closest('.kitene_send_zumi_btn')) continue;
            const raw = (el.innerText || el.getAttribute('value')
                || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
            if (!/ミテネを送る|ミテネする|ミテネ送る|ミテネ/.test(raw)) continue;
            if (raw.includes('送信済')) continue;
            if (raw.length > 80) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 40 || r.height < 16 || !el.offsetParent) continue;
            return true;
        }
        return false;
    };
    const extractMidDeep = (card) => {
        const tryRoot = (root) => {
            if (!root) return '';
            for (const el of root.querySelectorAll(
                '[class*="js-regist_comeon_"], [onclick*="registComeon"]'
            )) {
                if (el.classList) {
                    for (const c of el.classList) {
                        if (c.startsWith('js-regist_comeon_')) {
                            return c.replace('js-regist_comeon_', '');
                        }
                    }
                }
                const oc = el.getAttribute('onclick') || '';
                const m = oc.match(/registComeon\\((\\d+)\\)/);
                if (m) return m[1];
            }
            return extractUid(root);
        };
        let found = tryRoot(card);
        if (found) return found;
        let node = card.parentElement;
        for (let d = 0; d < 4 && node && node !== document.body; d++, node = node.parentElement) {
            found = tryRoot(node);
            if (found) return found;
        }
        return '';
    };
    const cardDataScore = (data) => {
        let score = (data.historyText || '').length * 10;
        score += (data.name || '').length;
        if (data.hasSendButton) score += 50;
        score += (data.cardText || '').length;
        if (data.mid || data.uid) score += 100;
        return score;
    };
    const buildCardData = (card) => {
        const uid = extractUid(card);
        const mid = extractMidDeep(card);
        return {
            name: extractName(card),
            uid,
            mid,
            cardText: (card.innerText || '').trim(),
            cardHtmlHead: (card.innerHTML || '').slice(0, 500),
            cardOuterHtmlHead: (card.outerHTML || '').slice(0, 500),
            historyText: readHistory(card),
            matchRate: readMatchRate(card),
            hasSendButton: hasSendButton(card),
        };
    };
    const selectorInfo = countSelectorHits();
    const rawBeforePrune = (() => {
        const cards = [];
        const seen = new Set();
        const add = (el) => {
            if (!el || seen.has(el) || !isLikelyMemberCard(el)) return;
            seen.add(el);
            cards.push(el);
        };
        for (const sel of MEMBER_CARD_ROOT_SELECTORS) {
            try { document.querySelectorAll(sel).forEach(add); } catch (_) {}
        }
        for (const sel of MEMBER_CARD_ANCHOR_SELECTORS) {
            let anchors = [];
            try { anchors = [...document.querySelectorAll(sel)]; } catch (_) { continue; }
            for (const anchor of anchors) {
                if (anchor.closest('.kitene_send_zumi_btn')) continue;
                let node = anchor;
                for (let depth = 0; depth < 14 && node; depth++, node = node.parentElement) {
                    if (!node || node === document.body) break;
                    if (isLikelyMemberCard(node)) { add(node); break; }
                }
            }
        }
        return cards;
    })();
    const nodes = pruneNestedCards(rawBeforePrune);
    const stats = {
        selectorHits: selectorInfo.hits,
        selectorHitsTotal: selectorInfo.total,
        querySelectorAllNodes: selectorInfo.total,
        rawNodesBeforePrune: rawBeforePrune.length,
        nodesAfterPrune: nodes.length,
        nestedPruned: Math.max(0, rawBeforePrune.length - nodes.length),
        duplicateIdMerged: 0,
        noMemberId: 0,
        nameMissing: 0,
        historyMissing: 0,
        recoveredFromParent: 0,
    };
    const byId = new Map();
    const noIdCards = [];
    for (const card of nodes) {
        const data = buildCardData(card);
        const dedupe = data.mid || data.uid;
        if (!dedupe) {
            stats.noMemberId++;
            noIdCards.push({ card, data });
            continue;
        }
        if (!data.name || data.name === '（名前不明）') stats.nameMissing++;
        if (!data.historyText) stats.historyMissing++;
        const existing = byId.get(dedupe);
        if (existing) {
            stats.duplicateIdMerged++;
            if (cardDataScore(data) > cardDataScore(existing)) {
                byId.set(dedupe, data);
            }
        } else {
            byId.set(dedupe, data);
        }
    }
    for (const item of noIdCards) {
        let node = item.card.parentElement;
        for (let d = 0; d < 5 && node && node !== document.body; d++, node = node.parentElement) {
            const recovered = buildCardData(node);
            const dedupe = recovered.mid || recovered.uid;
            if (!dedupe) continue;
            stats.recoveredFromParent++;
            stats.noMemberId = Math.max(0, stats.noMemberId - 1);
            const existing = byId.get(dedupe);
            if (!existing || cardDataScore(recovered) > cardDataScore(existing)) {
                byId.set(dedupe, recovered);
            }
            break;
        }
    }
    const out = [...byId.values()];
    stats.uniqueMemberIds = out.length;
    stats.finalCards = out.length;
    return { cards: out, stats };
}"""
)

# キープ / マッチ率専用（Pick Up 横タブ・プロフィール型 tab=4/5 対応）
# ※ みたよ用の user_ranking 一括走査は使わない

# タブ名 → 会員一覧URL（gid は login_id で付与）
TAB_LIST_PATHS: dict[str, str] = {
    "マイガール": "/J10ComeonMyGirlList.php",
    "キープ": "/J10ComeonKeepList.php",
    "マッチ率": "/J10ComeonAiMatchingList.php",
    "みたよ": "/J10ComeonVisitorList.php",
}



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


def member_queue_key(member_id: str) -> str:
    return f"comeon-{(member_id or '').strip()}"


@dataclass
class Member:
    """送信対象抽出結果（Locator / ElementHandle は保持しない）."""

    member_id: str
    name: str
    has_send_button: bool
    sent_history: bool = False
    last_sent: date | None = None


@dataclass
class SendPhaseRecord:
    """1送信フェーズ分のキューと試行結果（照合用）."""

    label: str
    queued_ids: list[str]
    index: int = 0
    success: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    attempted: list[str] = field(default_factory=list)


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
# ④マイガール（古い順）→ ⑤みたよ(マッチ率新規0件時のみ) → ⑥キープ → ⑦マッチ率（古い順）
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
        tab="マイガール",
        member_filter="sent_oldest_first",
        list_path="/J10ComeonMyGirlList.php",
    ),
    PriorityStep(
        tab="みたよ",
        condition="if_no_match_new",
        member_filter="sendable",
        list_path="/J10ComeonVisitorList.php",
    ),
    PriorityStep(
        tab="キープ",
        member_filter="sent_oldest_first",
        list_path="/J10ComeonKeepList.php",
    ),
    PriorityStep(
        tab="マッチ率",
        member_filter="sent_oldest_first",
        list_path="/J10ComeonAiMatchingList.php",
    ),
]

# ⑥〜⑦: キープ・マッチ率を送信日古い順に巡回（④マイガールはフェーズ1後に個別実行）
OLDEST_FIRST_PHASE_TABS: tuple[tuple[str, str, str], ...] = (
    ("⑥キープ（古い順）", "キープ", "/J10ComeonKeepList.php"),
    ("⑦マッチ率（古い順）", "マッチ率", "/J10ComeonAiMatchingList.php"),
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
# 直打ちリトライ回数（ループ内）
LIST_NAV_ATTEMPTS = 3
# 一覧 Ajax 遅延読込待ち（ミリ秒）
LIST_AJAX_LOAD_WAIT_MS = (2000, 3000)
# scrollHeight / カード数 / uniqueMemberIds が変化しない連続回数
LIST_SCROLL_STABLE_ROUNDS = 4
# スクロール→解析→マージの最大ループ回数
LIST_SCROLL_PARSE_MAX_ROUNDS = 150

# 一覧到達後の会員カード表示待機（全タブ共通・Ajax 遅延読込対策）
MEMBER_CARD_WAIT_SELECTORS: tuple[str, ...] = (
    "li.user_ranking_box",
    ".user_ranking_list > li",
    ".user_ranking_list li",
    "ul.user_ranking_list li",
    ".user_ranking_box",
)
LIST_RENDER_POLL_MS = 500
LIST_RENDER_WAIT_MAX_MS = 8000
LIST_ZERO_RETRY_WAIT_MS = 3000


LIST_LOADING_GONE_JS = """
() => {
    const loadingSelectors = [
        '.loading', '#loading', '[class*="loading"]', '[class*="Loading"]',
        '.loader', '#loader', '[class*="loader"]',
        '.now_loading', '#now_loading', '.kitene_loading',
        '.spinner', '[class*="spinner"]', '[id*="loading"]',
    ];
    for (const sel of loadingSelectors) {
        let nodes;
        try { nodes = document.querySelectorAll(sel); } catch (_) { continue; }
        for (const el of nodes) {
            const s = getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden') continue;
            const r = el.getBoundingClientRect();
            if (r.width >= 10 && r.height >= 10
                && (el.offsetParent || s.position === 'fixed')) {
                return false;
            }
        }
    }
    return true;
}
"""

TAB_MEMBER_TOTAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "マイガール": re.compile(r"現在のマイガール数\s*[:：]?\s*([\d,，]+)"),
    "キープ": re.compile(r"現在のキープ数\s*[:：]?\s*([\d,，]+)"),
    "みたよ": re.compile(r"現在のみたよ数\s*[:：]?\s*([\d,，]+)"),
}
# ログ表示用
LIST_PAGE_STABILIZE_MS = LIST_GOTO_SETTLE_MS + LIST_URL_FIXED_EXTRA_MS
# マイガールタブクリック後の待機（ミリ秒）— 直URL拒否のためキープ経由
MYGIRL_TAB_CLICK_WAIT_MS = 3000
KEEP_LIST_PATH = "/J10ComeonKeepList.php"
MYGIRL_LIST_PATH = "/J10ComeonMyGirlList.php"
# List.php 直打ち後に J1GirlUserPage?tab=N へ飛ぶアカウント向け
STEP_PROFILE_TAB: dict[str, str] = {
    "マイガール": "3",
    "キープ": "4",
    "マッチ率": "5",
}
MAX_PROFILE_TAB_MEMBERS = 50
PROFILE_MEMBER_PARSE_JS = """
(historyLabel) => {
    const uid = new URL(location.href).searchParams.get('uid') || '';
    let mid = uid;
    for (const el of document.querySelectorAll(
        '[class*="js-regist_comeon_"], [onclick*="registComeon"]'
    )) {
        for (const c of el.classList || []) {
            if (c.startsWith('js-regist_comeon_')) {
                mid = c.replace('js-regist_comeon_', '');
                break;
            }
        }
        const oc = el.getAttribute('onclick') || '';
        const m = oc.match(/registComeon\\((\\d+)\\)/);
        if (m) { mid = m[1]; break; }
    }
    let historyText = '';
    const box = document.querySelector('.kitene_question')
        || document.querySelector('.kitene_question_box');
    const scope = box || document.body;
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
    let hasSendButton = false;
    for (const el of document.querySelectorAll(
        '.kitene_send_btn, a, button, [onclick*="registComeon"]'
    )) {
        if (el.closest('.kitene_send_zumi_btn')) continue;
        const raw = (el.innerText || '').replace(/\\s+/g, ' ').trim();
        if (!/ミテネを送る|ミテネする|ミテネ送る/.test(raw)) continue;
        if (raw.length > 60) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 40 || r.height < 16 || !el.offsetParent) continue;
        hasSendButton = true;
        break;
    }
    const cardText = (scope.innerText || document.body.innerText || '').slice(0, 2500);
    let matchRate = '';
    const mr = cardText.match(/マッチ(?:ング)?率\\s*[:：]?\\s*(\\d+\\s*%)/);
    if (mr) matchRate = mr[1];
    return { uid, mid, historyText, hasSendButton, cardText, matchRate };
}
"""
PROFILE_UID_COLLECT_JS = """
(tabParam) => {
    const ids = [];
    const seen = new Set();
    const add = (id) => {
        id = String(id || '').trim();
        if (!id || seen.has(id)) return;
        seen.add(id);
        ids.push(id);
    };
    add(new URL(location.href).searchParams.get('uid'));
    for (const el of document.querySelectorAll('[onclick*="registComeon"]')) {
        const oc = el.getAttribute('onclick') || '';
        const m = oc.match(/registComeon\\((\\d+)\\)/);
        if (m) add(m[1]);
    }
    for (const el of document.querySelectorAll('[class*="js-regist_comeon_"]')) {
        for (const c of el.classList || []) {
            if (c.startsWith('js-regist_comeon_')) {
                add(c.replace('js-regist_comeon_', ''));
            }
        }
    }
    for (const a of document.querySelectorAll(
        'a[href*="girluserpage"], a[href*="GirlUserPage"], a[href*="uid="]'
    )) {
        const href = (a.getAttribute('href') || '').toLowerCase();
        if (tabParam && href.includes('tab=') && !href.includes('tab=' + tabParam)) {
            continue;
        }
        const m = href.match(/[?&]uid=(\\d+)/);
        if (m) add(m[1]);
    }
    for (const el of document.querySelectorAll('[class*="u_"]')) {
        for (const c of el.classList || []) {
            if (c.startsWith('u_') && c.length > 2) add(c.slice(2));
        }
    }
    return ids;
}
"""
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
    member_extraction_debug: bool = False
    # 調査用暫定: スクロール走査マージ解析（通常は false = 単回解析）
    member_scroll_merge_parse: bool = False


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
    """送信可能回数が残っていない（budget == 0 のときのみ）."""


BUDGET_READ_FAILED_PREFIX = "ミテネ残り回数取得失敗"


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


def _extract_card_parse_result(
    raw: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """MEMBER_CARD_PARSE_JS の { cards, stats } または配列を正規化."""
    if isinstance(raw, dict) and isinstance(raw.get("cards"), list):
        stats = raw.get("stats")
        cards = [x for x in raw["cards"] if isinstance(x, dict)]
        return cards, stats if isinstance(stats, dict) else {}
    return _normalize_evaluate_rows(raw), {}


def _card_dict_richness(card: dict[str, Any]) -> int:
    score = len(str(card.get("history_text") or "")) * 10
    score += len(str(card.get("name") or ""))
    if card.get("has_send_button"):
        score += 50
    score += len(str(card.get("inner_text") or ""))
    return score


def _merge_parse_stats(
    agg: dict[str, Any], stats: dict[str, Any], *, scroll_pass: int
) -> None:
    if not stats:
        return
    passes = agg.setdefault("scroll_passes", [])
    passes.append({"pass": scroll_pass, **stats})
    for key in (
        "nestedPruned",
        "duplicateIdMerged",
        "noMemberId",
        "nameMissing",
        "historyMissing",
        "recoveredFromParent",
    ):
        if key in stats:
            agg[key] = int(agg.get(key, 0)) + int(stats[key] or 0)
    if "selectorHits" in stats and not agg.get("selectorHits"):
        agg["selectorHits"] = stats["selectorHits"]
    for field in (
        "selectorHitsTotal",
        "querySelectorAllNodes",
        "rawNodesBeforePrune",
        "nodesAfterPrune",
    ):
        if field in stats:
            agg[field] = max(int(agg.get(field, 0)), int(stats[field] or 0))
    agg["uniqueMemberIds"] = max(
        int(agg.get("uniqueMemberIds", 0)),
        int(stats.get("uniqueMemberIds") or stats.get("finalCards") or 0),
    )


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
        progress_callback: Callable[[int, int], None] | None = None,
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
        self._progress_callback = progress_callback
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
        self._locked_send_budget: int | None = None
        self._no_history_sent_today: int = 0
        self._match_rate_had_new: bool | None = None
        self._pipeline_had_new_member: bool = False
        self._cached_list_cards: list[dict[str, Any]] | None = None
        self._cached_list_url: str = ""
        self._profile_uid_by_key: dict[str, str] = {}
        self._last_tab_parse_stats: dict[str, Any] = {}
        self._debug_collect_duplicates: dict[str, list[Member]] = {}
        self._debug_member_dom: dict[str, dict[str, str]] = {}
        self._debug_member_names: dict[str, str] = {}
        self._debug_run_sent_success: list[str] = []
        self._debug_run_sent_failed: list[str] = []
        self._debug_run_excluded: list[str] = []
        self._debug_exclusion_logged: set[str] = set()
        self._debug_parse_id_fail_count: int = 0
        self._send_run_phases: list[SendPhaseRecord] = []
        self._send_phase_tracker: SendPhaseRecord | None = None
        self._last_send_attempt: dict[str, str] | None = None
        self._last_goto_access_block = False
        self._last_nav_action: str = ""

    def _set_nav_debug_action(self, action: str) -> None:
        """framenavigated ログと突き合わせる直前操作の記録."""
        self._last_nav_action = action

    def _emit_send_progress(self, send_done: int, send_budget: int | None = None) -> None:
        budget = send_budget if send_budget is not None else self._send_target
        cb = self._progress_callback
        if not cb or budget <= 0:
            return
        try:
            cb(send_done, budget)
        except Exception:
            logger.debug("送信進捗コールバック失敗", exc_info=True)

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
                self._pause_ms(LOGIN_ACCESS_BLOCK_WAIT_MS)
                return
        except Exception:
            pass
        self._pause_ms(int(wait_sec * 1000))

    def _wait_before_login_page(self) -> None:
        wait_sec = random.uniform(*LOGIN_PRE_OPEN_WAIT_SEC)
        logger.info("ログイン画面を開く前に %.1f 秒待機", wait_sec)
        self._pause_ms(int(wait_sec * 1000))

    def _wait_page_settled(self, page: Page, *, quick: bool = False) -> None:
        """画面遷移後に待つ（networkidle は使わない＝ずっと待ち続ける原因になりやすい）."""
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000 if quick else 12000)
        except Exception:
            pass
        self._pause_ms(80 if quick else 200)

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

    def _url_query_param(self, page: Page, name: str) -> str:
        try:
            qs = parse_qs(urlparse(page.url or "").query)
            return (qs.get(name, [""])[0] or "").strip()
        except Exception:
            return ""

    def _is_step_profile_page(self, page: Page, step: PriorityStep) -> bool:
        """J1GirlUserPage?tab=N が意図したタブ（キープ/マッチ率等）か."""
        if not self._is_member_profile_page(page):
            return False
        expected = STEP_PROFILE_TAB.get(step.tab, "")
        tab = self._url_query_param(page, "tab")
        if expected and tab == expected:
            return True
        return self._is_step_tab_active(page, step)

    def _goto_profile_tab(
        self, page: Page, uid: str, step: PriorityStep
    ) -> bool:
        uid = (uid or "").strip()
        if not uid:
            return False
        parsed = urlparse(page.url or "")
        host = parsed.netloc or SPGIRL_LIST_HOST
        scheme = parsed.scheme or "https"
        path = parsed.path if "girluserpage" in parsed.path.lower() else "/J1GirlUserPage.php"
        qs_parts = [f"uid={uid}"]
        tab = STEP_PROFILE_TAB.get(step.tab, "")
        if tab:
            qs_parts.append(f"tab={tab}")
        gid = self._gid()
        if gid:
            qs_parts.append(f"gid={gid}")
        target = urlunparse(
            (scheme, host, path, "", "&".join(qs_parts), "")
        )
        return self._safe_goto(page, target)

    def _navigate_to_profile_member(
        self, page: Page, member_id: str, step: PriorityStep
    ) -> bool:
        key = f"comeon-{member_id}"
        uid = self._profile_uid_by_key.get(key) or member_id
        current_uid = self._url_query_param(page, "uid")
        btn = self._kitene_button_locator(page, member_id)
        if (
            current_uid == uid
            and self._safe_count(btn) > 0
            and self._safe_is_visible(btn.first)
        ):
            return True
        if not self._goto_profile_tab(page, uid, step):
            return False
        self._pause_ms(700)
        btn = self._kitene_button_locator(page, member_id)
        return self._safe_count(btn) > 0 and self._safe_is_visible(btn.first)

    def _collect_profile_tab_uids(
        self, page: Page, step: PriorityStep
    ) -> list[str]:
        tab = STEP_PROFILE_TAB.get(step.tab, "")
        try:
            raw = page.evaluate(PROFILE_UID_COLLECT_JS, tab)
        except Exception:
            return []
        if not isinstance(raw, list):
            return []
        return [str(u).strip() for u in raw if str(u).strip()]

    def _parse_single_profile_member(
        self, page: Page
    ) -> dict[str, Any] | None:
        try:
            raw = page.evaluate(
                PROFILE_MEMBER_PARSE_JS,
                {"historyLabel": self.standard.mitene_history_label},
            )
        except Exception as e:
            logger.warning("プロフィール会員解析失敗: %s", e)
            return None
        if not isinstance(raw, dict):
            return None
        uid = str(raw.get("uid") or "").strip()
        mid = str(raw.get("mid") or uid or "").strip()
        if not mid:
            return None
        card_text = str(raw.get("cardText") or "")
        history_text = str(raw.get("historyText") or "").strip()
        key = f"comeon-{mid}"
        if uid:
            self._profile_uid_by_key[key] = uid
        return {
            "name": "（名前不明）",
            "uid": uid,
            "mid": mid,
            "key": key,
            "profile_uid": uid,
            "card_text": card_text,
            "card_html_head": "",
            "history_text": history_text,
            "match_rate": str(raw.get("matchRate") or "").strip(),
            "has_send_button": bool(raw.get("hasSendButton")),
        }

    def _parse_profile_tab_members(
        self, page: Page, step: PriorityStep
    ) -> list[dict[str, Any]]:
        """J1GirlUserPage?tab=N 型のプロフィール巡回一覧から会員を収集."""
        tab_name = step.tab
        wait_ms = 22000 if tab_name == "マッチ率" else 16000
        self._wait_profile_tab_ready(page, step, timeout_ms=wait_ms)

        members, history_texts = self.collect_members(page, tab_name, quiet=True)
        if members and any(m.has_send_button for m in members):
            sendable = sum(1 for m in members if m.has_send_button)
            logger.info(
                "【%s】プロフィール内リストDOM: %d 件（送信可 %d）",
                tab_name,
                len(members),
                sendable,
            )
            return self._collected_to_raw_cards(members, history_texts)

        uids = self._collect_profile_tab_uids(page, step)
        for attempt in range(4):
            if len(uids) > 1:
                break
            self._pause_ms(1200)
            uids = self._collect_profile_tab_uids(page, step)
        logger.info(
            "【%s】プロフィール型一覧: uid %d 件を巡回", tab_name, len(uids)
        )
        if not uids:
            one = self._parse_single_profile_member(page)
            return [one] if one else []

        parsed: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for uid in uids[:MAX_PROFILE_TAB_MEMBERS]:
            current_uid = self._url_query_param(page, "uid")
            if uid != current_uid:
                if not self._goto_profile_tab(page, uid, step):
                    continue
                self._pause_ms(700)
            card = self._parse_single_profile_member(page)
            if not card:
                continue
            key = str(card.get("key") or "")
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            parsed.append(card)
        return parsed

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

        def _on_frame_navigated(frame) -> None:
            try:
                if frame != page.main_frame:
                    return
                url = frame.url or ""
                prev = getattr(self, "_last_nav_action", "") or "(none)"
                logger.warning("NAVIGATED -> %s (last_action=%s)", url, prev)
                u = url.lower()
                if "j1girluserpage" in u or "j10comeonkeeplist" in u:
                    for line in traceback.format_stack(limit=12)[:-2]:
                        logger.warning("  nav_stack: %s", line.rstrip())
            except Exception:
                pass

        def _on_load(frame) -> None:
            try:
                if frame != page.main_frame:
                    return
                url = frame.url or ""
                prev = getattr(self, "_last_nav_action", "") or "(none)"
                logger.warning("PAGE_LOAD -> %s (last_action=%s)", url, prev)
            except Exception:
                pass

        page.on("dialog", _on_dialog)
        page.on("framenavigated", _on_frame_navigated)
        page.on("load", _on_load)

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
        self._locked_send_budget = None
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
                    from job_runner import JobCancelled

                    if isinstance(e, JobCancelled):
                        context.close()
                        raise
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
        ws_endpoint = (os.getenv("PLAYWRIGHT_BROWSER_WS_ENDPOINT") or "").strip()
        if not ws_endpoint:
            token = (os.getenv("BROWSERLESS_TOKEN") or "").strip()
            if token:
                ws_endpoint = f"wss://chrome.browserless.io?token={token}"
        if ws_endpoint:
            logger.info("リモートブラウザに接続します")
            return p.chromium.connect_over_cdp(ws_endpoint)
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
        self._pause_ms(int(LOGIN_POST_SUBMIT_WAIT_SEC * 1000))
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
            self._check_job_control()
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
                    self._pause_ms(LOGIN_RETRY_WAIT_MS)

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
            self._pause_ms(400)

    def _mitene_remaining_values_in_text(self, text: str) -> list[int]:
        normalized = _normalize_digits(text or "")
        return [int(v) for v in MITENE_REMAINING_PATTERN.findall(normalized)]

    def _extract_remaining_from_text(
        self, text: str, *, allow_slash: bool = False
    ) -> int | None:
        """テキスト断片から残り回数を取得（複数マッチ時は最大値＝誤った0を避ける）."""
        values = self._mitene_remaining_values_in_text(text)
        if values:
            return max(values)
        if not allow_slash:
            return None
        normalized = _normalize_digits(text or "")
        match = REMAINING_SLASH_PATTERN.search(normalized)
        if match:
            return int(match.group(1))
        return None

    def _parse_remaining_count(self, page: Page) -> int | None:
        """CTA付近・ラベル・本文から「ミテネ残り回数：N回」を取得."""
        try:
            return self._parse_remaining_count_inner(page)
        except Exception as e:
            if _is_destroyed_context_error(e):
                return None
            raise

    def _parse_remaining_from_labels(self, page: Page) -> list[int]:
        values: list[int] = []
        try:
            loc = page.get_by_text(self.standard.remaining_label, exact=False)
            n = self._safe_count(loc)
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
                    values.append(parsed)
        except Exception:
            pass
        return values

    def _parse_remaining_from_body(self, page: Page) -> int | None:
        """本文全体から取得（正の値のみ・0は誤検出になりやすいので採用しない）."""
        for source in (
            lambda: page.inner_text("body"),
            lambda: page.content(),
        ):
            try:
                values = self._mitene_remaining_values_in_text(source())
                positives = [v for v in values if v > 0]
                if positives:
                    return max(positives)
            except Exception:
                continue
        return None

    def _collect_remaining_reads(self, page: Page) -> list[tuple[str, int]]:
        reads: list[tuple[str, int]] = []

        near = self._parse_remaining_near_cta(page)
        if near is not None:
            reads.append(("cta", near))

        label_vals = self._parse_remaining_from_labels(page)
        if label_vals:
            reads.append(("label", max(label_vals)))

        if not reads:
            body_val = self._parse_remaining_from_body(page)
            if body_val is not None:
                reads.append(("body", body_val))

        return reads

    def _merge_remaining_reads(self, reads: list[tuple[str, int]]) -> int | None:
        if not reads:
            return None
        values = [v for _, v in reads]
        if any(v > 0 for v in values):
            chosen = max(v for v in values if v > 0)
            if len(set(values)) > 1:
                logger.info(
                    "残り回数の読み取り差異 %s → %d 回を採用",
                    reads,
                    chosen,
                )
            return chosen
        sources = {source for source, _ in reads}
        if sources & {"cta", "label"}:
            return 0
        return None

    def _parse_remaining_count_inner(self, page: Page) -> int | None:
        return self._merge_remaining_reads(self._collect_remaining_reads(page))

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
        return self._extract_remaining_from_text(block_text, allow_slash=True)

    def _cap_send_budget(self, remaining: int) -> int:
        if self.standard.max_send_per_run > 0:
            return min(remaining, self.standard.max_send_per_run)
        return remaining

    def _retry_remaining_on_suspect_zero(self, page: Page) -> int | None:
        """0 判定のときだけホームを安定させて再読み取り（表示遅延・誤検出対策）."""
        on_home = self._safe_count_text(page, self.standard.find_members_button) > 0
        if not on_home:
            return None
        for attempt in range(1, 5):
            wait_ms = 600 + attempt * 350
            logger.info(
                "残り回数0のため再確認 (%d/4) … %d ms 待機",
                attempt,
                wait_ms,
            )
            self._pause_ms(wait_ms)
            page.evaluate("window.scrollTo(0, 0)")
            self._pause_ms(250)
            self._wait_page_settled(page, quick=True)
            for _ in range(2):
                parsed = self._parse_remaining_count_inner(page)
                if parsed is not None and parsed > 0:
                    return parsed
                page.evaluate(
                    "window.scrollBy(0, Math.min(window.innerHeight, 420))"
                )
                self._pause_ms(350)
        return self._parse_remaining_count_inner(page)

    def _budget_read_error(self, reason: str) -> RuntimeError:
        return RuntimeError(f"{BUDGET_READ_FAILED_PREFIX}（{reason}）")

    def _collect_remaining_dom_snippet(self, page: Page) -> str:
        chunks: list[str] = []
        try:
            cta = page.get_by_text(self.standard.find_members_button, exact=False)
            if self._safe_count(cta) > 0:
                text = cta.first.evaluate(
                    """el => {
                        const p = el.closest('p, div, section, li') || el.parentElement;
                        return (p ? p.innerText : el.innerText) || '';
                    }"""
                )
                chunks.append(f"CTA付近: {(text or '')[:800]}")
            else:
                chunks.append("CTA付近: （ボタン未検出）")
        except Exception as exc:
            chunks.append(f"CTA付近: （取得失敗: {exc}）")
        try:
            loc = page.get_by_text(self.standard.remaining_label, exact=False)
            if self._safe_count(loc) > 0:
                chunks.append(f"ラベル: {loc.first.inner_text()[:400]}")
            else:
                chunks.append("ラベル: （未検出）")
        except Exception as exc:
            chunks.append(f"ラベル: （取得失敗: {exc}）")
        try:
            chunks.append(f"body先頭: {page.inner_text('body')[:1500]}")
        except Exception as exc:
            chunks.append(f"body先頭: （取得失敗: {exc}）")
        return "\n".join(chunks)

    def _log_budget_read(
        self,
        page: Page,
        *,
        dom_snippet: str,
        regex_values: list[int],
        reads: list[tuple[str, int]],
        final_budget: int | None,
    ) -> None:
        logger.info("【残り回数取得】URL: %s", page.url or "")
        logger.info("【残り回数取得】読み取ったDOM: %s", dom_snippet)
        logger.info(
            "【残り回数取得】正規表現で取得した数値: %s",
            regex_values if regex_values else "（なし）",
        )
        logger.info(
            "【残り回数取得】読み取りソース: %s",
            reads if reads else "（なし）",
        )
        logger.info(
            "【残り回数取得】最終budget: %s",
            final_budget if final_budget is not None else "（取得失敗）",
        )

    def _has_confident_zero_read(self, reads: list[tuple[str, int]]) -> bool:
        zero_sources = {source for source, value in reads if value == 0}
        return bool(zero_sources & {"cta", "label", "retry", "list"})

    def _read_send_budget(self, page: Page) -> int:
        """ミテネ残り回数を取得。budget==0 のみ DailyLimitReached、取得失敗は RuntimeError."""
        if self._locked_send_budget is not None:
            logger.info(
                "【残り回数取得】ロック済みbudgetを使用: %d",
                self._locked_send_budget,
            )
            return self._locked_send_budget

        if not self._is_on_pickup_member_page(page):
            self._ensure_deco_home(page)

        dom_snippet = self._collect_remaining_dom_snippet(page)
        reads = self._collect_remaining_reads(page)
        regex_values = self._mitene_remaining_values_in_text(dom_snippet)
        remaining = self._merge_remaining_reads(reads)

        if remaining is None:
            self._log_budget_read(
                page,
                dom_snippet=dom_snippet,
                regex_values=regex_values,
                reads=reads,
                final_budget=None,
            )
            on_home = (
                self._safe_count_text(page, self.standard.find_members_button) > 0
            )
            if not on_home:
                logger.warning(
                    "姫デコのホーム画面を開けませんでした。"
                    "「女の子ログイン」のURLが古い・期限切れ、またはログインに失敗している可能性があります。"
                )
                raise self._budget_read_error("ホーム画面を開けませんでした")
            raise self._budget_read_error("CTA付近の残り回数が読み取れませんでした")

        if remaining < 0:
            self._log_budget_read(
                page,
                dom_snippet=dom_snippet,
                regex_values=regex_values,
                reads=reads,
                final_budget=None,
            )
            raise self._budget_read_error(f"不正な残り回数: {remaining}")

        if remaining == 0:
            retried = self._retry_remaining_on_suspect_zero(page)
            if retried is not None and retried > 0:
                logger.info("残り回数の再確認で %d 回を取得", retried)
                remaining = retried
                reads.append(("retry", retried))
            else:
                try:
                    if self._safe_count_text(
                        page, self.standard.find_members_button
                    ) > 0:
                        logger.info("残り0のため会員探し画面でも再確認します")
                        self._open_find_members(page)
                        list_rem = self._parse_remaining_count(page)
                        if list_rem is not None and list_rem > 0:
                            logger.info("会員探し画面で残り %d 回を取得", list_rem)
                            remaining = list_rem
                            reads.append(("list", list_rem))
                        elif list_rem == 0:
                            reads.append(("list", 0))
                except Exception as e:
                    logger.debug("会員探しでの残り回数再確認失敗: %s", e)

            if remaining == 0:
                if self._has_confident_zero_read(reads):
                    self._log_budget_read(
                        page,
                        dom_snippet=dom_snippet,
                        regex_values=regex_values,
                        reads=reads,
                        final_budget=0,
                    )
                    raise DailyLimitReached("ミテネ残り回数が 0 です。")
                self._log_budget_read(
                    page,
                    dom_snippet=dom_snippet,
                    regex_values=regex_values,
                    reads=reads,
                    final_budget=None,
                )
                raise self._budget_read_error(
                    "残り0と判定されましたがCTA付近で確認できませんでした"
                )

        budget = self._cap_send_budget(remaining)
        self._log_budget_read(
            page,
            dom_snippet=dom_snippet,
            regex_values=regex_values,
            reads=reads,
            final_budget=budget,
        )
        self._locked_send_budget = budget
        logger.info("送信予定回数（ミテネ残り回数）: %d", budget)
        return budget

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
        if self._member_extraction_debug_enabled() and key.startswith("comeon-"):
            mid = key[7:]
            if mid not in self._debug_run_sent_success:
                self._debug_run_sent_success.append(mid)
        self._invalidate_list_cache()
        if not self.dry_run:
            self._record_member_sent(key)
            if was_new:
                self._no_history_sent_today += 1

    def _wait_for_send_buttons(self, page: Page, timeout_ms: int = 20000) -> bool:
        """「ミテネを送る」ボタンが出るまで待つ."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            self._check_job_control()
            if self._page_has_send_targets(page):
                return True
            self._pause_ms(400)
        return False

    def _ensure_member_list_page(self, page: Page) -> None:
        """送信後、③会員一覧（ミテネを送るが並ぶ画面）に戻す."""
        step = self._current_step
        if self._is_member_profile_page(page):
            if step and self._is_step_profile_page(page, step):
                self._wait_page_settled(page, quick=True)
                return
            logger.info("送信後: プロフィールから一覧へ戻る")
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
        self._check_job_control()
        logger.info("移動前にブラウザの未完了処理を強制停止します")
        try:
            page.evaluate("window.stop();")
        except Exception:
            pass
        self._pause_ms(PRE_NAV_STOP_MS)

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
            self._check_job_control()
            attempt += 1
            logger.info(
                "一覧URLへ直接遷移を試みます (試行 %d) -> %s",
                attempt,
                target,
            )
            self._stop_browser_pending_tasks(page)
            if not self._goto_list_target(page, target):
                self._pause_ms(400)
                continue

            self._pause_ms(LIST_GOTO_SETTLE_MS)

            if self._is_member_profile_page(page):
                logger.warning(
                    "【%s】プロフィールへリダイレクトされたため再試行: %s",
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
                self._pause_ms(LIST_URL_FIXED_EXTRA_MS)
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
        self._pause_ms(FINAL_FORCED_WAIT_MS)

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
                logger.info(
                    "【%s】一覧DOM検出直後 URL=%s", step.tab, page.url or ""
                )
                break
            except Exception:
                continue

        self._set_nav_debug_action(
            f"_navigate_to_url_safe:{step.tab}:post_dom_detect:profile_check"
        )
        if self._is_member_profile_page(page):
            logger.warning(
                "【%s】一覧直打ち後もプロフィールURL: %s",
                step.tab,
                page.url,
            )
            return False

        self._set_nav_debug_action(
            f"_navigate_to_url_safe:{step.tab}:pre_dismiss_optional_popups"
        )
        self._dismiss_optional_popups(page)
        logger.info(
            "【%s】dismiss_optional_popups直後 URL=%s", step.tab, page.url or ""
        )

        self._set_nav_debug_action(
            f"_navigate_to_url_safe:{step.tab}:post_dismiss:is_on_step_list_page"
        )
        if path and self._is_on_step_list_page(page, path):
            logger.info("【%s】URL直打ち成功: %s", step.tab, page.url)
            return True
        self._set_nav_debug_action(
            f"_navigate_to_url_safe:{step.tab}:post_dismiss:verify_step_list"
        )
        if self._verify_step_list(page, step):
            logger.info("【%s】URL直打ち成功: %s", step.tab, page.url)
            return True
        self._set_nav_debug_action(
            f"_navigate_to_url_safe:{step.tab}:post_dismiss:is_on_target_list_url"
        )
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
        self._pause_ms(MYGIRL_TAB_CLICK_WAIT_MS)

        if self._is_member_profile_page(page):
            mygirl_step = PriorityStep(tab="マイガール", list_path=MYGIRL_LIST_PATH)
            if self._is_step_profile_page(page, mygirl_step):
                self._current_list_path = MYGIRL_LIST_PATH
                logger.info(
                    "【マイガール】プロフィール型一覧表示: %s", page.url
                )
                return True
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
    ) -> tuple[list[Member], dict[str, str]]:
        """window.stop → URL固定 → スクロール → collect_members."""
        self._check_job_control()
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
                logger.info("【%s】直打ち失敗 → 横タブ切替を試行", step.tab)
                opened = self._navigate_to_step_list(page, step)
        if not opened:
            logger.warning("【%s】一覧取得失敗（gid=%s）", step.tab, self._gid())
            return [], {}

        if self._member_extraction_debug_enabled():
            self._log_tab_switch_debug(page, step.tab)

        if not self._ensure_on_step_list_for_parse(page, step):
            logger.warning(
                "【%s】一覧ページを確認できないため解析を中止", step.tab
            )
            return [], {}

        pre_count = self._prepare_list_page_before_collect(page, step.tab)
        self._log_list_page_before_parse(
            page, step.tab, pre_count, step=step
        )
        if self._is_member_profile_page(page) or not self._verify_step_list(
            page, step
        ):
            logger.warning(
                "【%s】一覧でないため解析を中止: %s", step.tab, page.url
            )
            return [], {}

        return self.collect_members(
            page, step.tab, list_prepared=True, pre_card_count=pre_count
        )

    def _raw_cards_to_collected(
        self, cards: list[dict[str, Any]]
    ) -> tuple[list[Member], dict[str, str]]:
        """プロフィール型一覧の生カード → Member + history_texts."""
        members: list[Member] = []
        history_texts: dict[str, str] = {}
        for card in cards:
            if not isinstance(card, dict):
                continue
            mid = str(card.get("mid") or card.get("uid") or "").strip()
            if not mid:
                continue
            history_texts[mid] = str(card.get("history_text") or "").strip()
            members.append(
                Member(
                    member_id=mid,
                    name=str(card.get("name") or "（名前不明）"),
                    has_send_button=bool(card.get("has_send_button")),
                )
            )
        return members, history_texts

    def _collected_to_raw_cards(
        self,
        members: list[Member],
        history_texts: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Member + history_texts → プロフィール巡回用の生カード dict."""
        cards: list[dict[str, Any]] = []
        for member in members:
            mid = member.member_id
            if not mid:
                continue
            cards.append(
                {
                    "name": member.name,
                    "uid": mid,
                    "mid": mid,
                    "key": member_queue_key(mid),
                    "history_text": history_texts.get(mid, ""),
                    "has_send_button": member.has_send_button,
                }
            )
        return cards

    def _filter_new_members(
        self,
        members: list[Member],
        history_texts: dict[str, str],
        step: PriorityStep,
    ) -> list[Member]:
        """新規会員（ミテネ履歴に「送信済」なし）かつ送信ボタンあり."""
        return self._apply_step_member_filter(members, history_texts, step)

    def _filter_sendable_members(
        self,
        members: list[Member],
        history_texts: dict[str, str],
        step: PriorityStep,
    ) -> list[Member]:
        """送信ボタンがあり未送信キューに入れられる会員."""
        return self._apply_step_member_filter(members, history_texts, step)

    def _members_to_keys(self, members: list[Member]) -> list[str]:
        keys: list[str] = []
        for member in members:
            key = member_queue_key(member.member_id)
            if key in self._sent_member_keys or key in self._failed_member_keys:
                continue
            if not member.has_send_button:
                continue
            keys.append(key)
        return keys

    def _sort_members_oldest_first(self, members: list[Member]) -> list[Member]:
        """全会員を送信日古い順（同日ランダム）に並べる."""
        pool = [
            m
            for m in members
            if m.member_id
            and m.has_send_button
            and member_queue_key(m.member_id) not in self._sent_member_keys
            and member_queue_key(m.member_id) not in self._failed_member_keys
        ]
        if not pool:
            return []
        random.shuffle(pool)
        pool.sort(key=lambda m: m.last_sent or OLDEST_SORT_DEFAULT_DATE)
        ordered: list[Member] = []
        for _, grp in itertools.groupby(
            pool,
            key=lambda m: m.last_sent or OLDEST_SORT_DEFAULT_DATE,
        ):
            batch = list(grp)
            if len(batch) > 1:
                random.shuffle(batch)
            ordered.extend(batch)
        return ordered

    def _reset_debug_run_tracking(self) -> None:
        self._debug_run_sent_success.clear()
        self._debug_run_sent_failed.clear()
        self._debug_run_excluded.clear()
        self._debug_exclusion_logged.clear()
        self._send_run_phases.clear()
        self._send_phase_tracker = None
        self._last_send_attempt = None

    def _member_id_from_queue_key(self, key: str) -> str:
        return key[7:] if key.startswith("comeon-") else (key or "").strip()

    def _set_send_attempt_outcome(
        self, member_id: str, status: str, reason: str = ""
    ) -> None:
        self._last_send_attempt = {
            "member_id": member_id,
            "status": status,
            "reason": reason,
        }

    def _begin_send_phase_tracking(self, label: str, keys: list[str]) -> None:
        queued = [
            self._member_id_from_queue_key(k)
            for k in keys
            if k.startswith("comeon-")
        ]
        self._send_phase_tracker = SendPhaseRecord(label=label, queued_ids=queued)
        self._send_run_phases.append(self._send_phase_tracker)
        logger.info("=====送信フェーズ===== %s", label)

    def _record_send_attempt_from_last(self) -> None:
        outcome = self._last_send_attempt
        tracker = self._send_phase_tracker
        if not outcome or not tracker:
            return
        member_id = outcome.get("member_id") or ""
        if not member_id:
            return
        status = outcome.get("status") or "失敗"
        reason = outcome.get("reason") or ""
        tracker.index += 1
        tracker.attempted.append(member_id)
        total = len(tracker.queued_ids) or tracker.index
        logger.info("%d/%d", tracker.index, total)
        logger.info("member_id=%s", member_id)
        if status == "成功":
            tracker.success.append(member_id)
            logger.info("成功")
        elif status == "スキップ":
            tracker.skipped[member_id] = reason
            logger.info("スキップ")
            if reason:
                logger.info("理由: %s", reason)
        else:
            tracker.failed[member_id] = reason or "送信失敗"
            logger.info("失敗")
            if reason:
                logger.info("理由: %s", reason)
        logger.info("")

    def _log_send_reconciliation_block(
        self,
        *,
        title: str,
        queued_ids: list[str],
        success_ids: list[str],
        failed_ids: dict[str, str],
        not_executed_ids: list[str],
    ) -> None:
        logger.info("=====%s=====", title)
        logger.info("送信キュー")
        for mid in queued_ids:
            logger.info(mid)
        logger.info("")
        logger.info("成功")
        for mid in success_ids:
            logger.info(mid)
        logger.info("")
        logger.info("失敗")
        for mid in queued_ids:
            if mid in failed_ids:
                logger.info(mid)
        for mid, _reason in failed_ids.items():
            if mid not in queued_ids:
                logger.info(mid)
        logger.info("")
        logger.info("未実行")
        for mid in not_executed_ids:
            logger.info(mid)

    def _finish_send_phase_tracking(self) -> None:
        tracker = self._send_phase_tracker
        if not tracker:
            return
        attempted_set = set(tracker.attempted)
        not_executed = [
            mid for mid in tracker.queued_ids if mid not in attempted_set
        ]
        failed_all = dict(tracker.failed)
        failed_all.update(tracker.skipped)
        self._log_send_reconciliation_block(
            title=f"照合 {tracker.label}",
            queued_ids=tracker.queued_ids,
            success_ids=tracker.success,
            failed_ids=failed_all,
            not_executed_ids=not_executed,
        )
        self._send_phase_tracker = None

    def _log_run_send_reconciliation(self) -> None:
        if not self._send_run_phases:
            return
        queued: list[str] = []
        seen_queued: set[str] = set()
        for phase in self._send_run_phases:
            for mid in phase.queued_ids:
                if mid in seen_queued:
                    continue
                seen_queued.add(mid)
                queued.append(mid)
        success: list[str] = []
        seen_success: set[str] = set()
        failed: dict[str, str] = {}
        attempted: set[str] = set()
        for phase in self._send_run_phases:
            attempted.update(phase.attempted)
            for mid in phase.success:
                if mid not in seen_success:
                    seen_success.add(mid)
                    success.append(mid)
            failed.update(phase.failed)
            failed.update(phase.skipped)
        not_executed = [mid for mid in queued if mid not in attempted]
        self._log_send_reconciliation_block(
            title="照合",
            queued_ids=queued,
            success_ids=success,
            failed_ids=failed,
            not_executed_ids=not_executed,
        )
        logger.info(
            "送信キュー %d 件 → 成功 %d / 失敗 %d / 未実行 %d",
            len(queued),
            len(success),
            len(failed),
            len(not_executed),
        )

    def _register_failed_member_key(self, key: str) -> None:
        self._failed_member_keys.add(key)
        if self._member_extraction_debug_enabled() and key.startswith("comeon-"):
            mid = key[7:]
            if mid not in self._debug_run_sent_failed:
                self._debug_run_sent_failed.append(mid)

    def _log_exclusion_immediate(self, member: Member, reason: str) -> None:
        if not self._member_extraction_debug_enabled():
            return
        mid = (member.member_id or "").strip()
        if not mid or mid in self._debug_exclusion_logged:
            return
        self._debug_exclusion_logged.add(mid)
        logger.info("【除外】member_id=%s", mid)
        logger.info("%s", member.name)
        logger.info("理由：%s", reason)
        if mid not in self._debug_run_excluded:
            self._debug_run_excluded.append(mid)

    def _log_filter_exclusions_immediate(
        self,
        enriched: list[Member],
        filtered: list[Member],
        duplicates: list[Member],
        step: PriorityStep,
    ) -> None:
        if not self._member_extraction_debug_enabled():
            return
        mode = step.member_filter or "sendable"
        filtered_ids = {m.member_id for m in filtered}
        for member in duplicates:
            self._log_exclusion_immediate(member, "重複")
        for member in enriched:
            if member.member_id in filtered_ids:
                continue
            self._log_exclusion_immediate(
                member, self._member_filter_exclusion_reason(member, mode)
            )

    def _log_locator_correlation_failure(
        self,
        tab_name: str,
        *,
        member_id: str,
        name: str,
        inner_text: str,
        outer_html: str,
    ) -> None:
        """DOMカードから member_id を Locator に対応づけできなかった場合（必ず出力）."""
        logger.info("【%s】Locator対応失敗", tab_name)
        logger.info("member_id: %s", member_id or "(未取得)")
        logger.info("名前: %s", name)
        logger.info("card outerHTML先頭500: %s", (outer_html or "")[:500])
        logger.info("card innerText: %s", inner_text)

    def _log_tab_correlation_stats_debug(
        self, page: Page, tab_name: str, members: list[Member]
    ) -> None:
        if not self._member_extraction_debug_enabled():
            return
        surface = self._member_card_surface(page)
        card_count = self._count_member_cards_on_surface(surface)
        locator_count = self._count_mitene_send_buttons(page)
        id_success = len(members)
        id_fail = self._debug_parse_id_fail_count
        logger.info("【%s】カード数: %d", tab_name, card_count)
        logger.info("【%s】送信ボタンLocator数: %d", tab_name, locator_count)
        logger.info("【%s】member_id対応成功数: %d", tab_name, id_success)
        logger.info("【%s】対応失敗数: %d", tab_name, id_fail)

    def _log_tab_switch_debug(self, page: Page, tab_name: str) -> None:
        if not self._member_extraction_debug_enabled():
            return
        try:
            title = page.title()
        except Exception:
            title = ""
        card_count = self._count_member_cards_on_surface(
            self._member_card_surface(page)
        )
        logger.info("【%s】タブ切替後", tab_name)
        logger.info("URL: %s", page.url or "")
        logger.info("ページタイトル: %s", title)
        logger.info("カード数: %d", card_count)

    def _log_scroll_metrics_debug(self, page: Page) -> None:
        if not self._member_extraction_debug_enabled():
            return
        try:
            metrics = page.evaluate(
                """() => ({
                    scrollTop: document.documentElement.scrollTop
                        || document.body.scrollTop || 0,
                    scrollHeight: Math.max(
                        document.body.scrollHeight,
                        document.documentElement.scrollHeight
                    ),
                    clientHeight: document.documentElement.clientHeight
                        || window.innerHeight || 0
                })"""
            )
        except Exception:
            metrics = {}
        if isinstance(metrics, dict):
            logger.info(
                "【スクロール終了】scrollTop=%s scrollHeight=%s clientHeight=%s",
                metrics.get("scrollTop", "?"),
                metrics.get("scrollHeight", "?"),
                metrics.get("clientHeight", "?"),
            )

    def _log_oldest_first_sort_debug(
        self, tab_name: str, ordered: list[Member]
    ) -> None:
        if not self._member_extraction_debug_enabled() or not ordered:
            return
        logger.info("【%s】古い順ソート結果（全%d件）", tab_name, len(ordered))
        for i, member in enumerate(ordered, 1):
            last_sent = (
                member.last_sent.isoformat()
                if member.last_sent
                else "（履歴なし）"
            )
            logger.info(
                "%d. member_id=%s 名前=%s last_sent=%s",
                i,
                member.member_id,
                member.name,
                last_sent,
            )

    def _log_debug_run_summary(self) -> None:
        if not self._member_extraction_debug_enabled():
            return
        logger.info("====== 実行終了デバッグサマリー ======")
        logger.info("送信成功: %s", self._debug_run_sent_success)
        logger.info("送信失敗: %s", self._debug_run_sent_failed)
        logger.info("送信対象外: %s", self._debug_run_excluded)

    def _send_member_keys_phase(
        self,
        page: Page,
        label: str,
        keys: list[str],
        budget: int,
        sent: int,
        sent_by_step: dict[str, int],
        *,
        members: list[Member] | None = None,
    ) -> int:
        """キーリストをキューに載せて残り回数ぶん送信."""
        self._check_job_control()
        tab_name = self._current_step.tab if self._current_step else "一覧"
        if sent >= budget or not keys:
            if not keys:
                self._log_pipeline_funnel_queue(tab_name, 0)
            return sent
        if members:
            self._log_final_send_targets(members, label)
            if self._member_extraction_debug_enabled():
                for member in members:
                    self._remember_debug_member_name(member.member_id, member.name)
        self._send_button_queue = [k for k in keys if k not in self._sent_member_keys]
        step_limit = min(len(self._send_button_queue), budget - sent)
        self._log_member_extraction_queue_debug(len(self._send_button_queue))
        if step_limit <= 0:
            return sent
        self._begin_send_phase_tracking(label, self._send_button_queue[:step_limit])
        logger.info("%s: %d 人へ送信開始", label, step_limit)
        try:
            return self._send_loop_for_step(
                page, label, budget, sent, sent_by_step, step_limit
            )
        finally:
            self._finish_send_phase_tracking()

    def _send_oldest_first_phases(
        self,
        page: Page,
        budget: int,
        sent: int,
        sent_by_step: dict[str, int],
    ) -> int:
        """⑥⑦: キープ→マッチ率を各タブ内で送信日古い順に送信."""
        logger.info("=== フェーズ2: 送信日古い順（⑥⑦）===")
        for label, tab, list_path in OLDEST_FIRST_PHASE_TABS:
            if sent >= budget:
                break
            self._check_job_control()
            step = PriorityStep(
                tab=tab,
                member_filter="sent_oldest_first",
                list_path=list_path,
            )
            members, history_texts = self._fetch_tab_members(page, step)
            filtered = self._apply_step_member_filter(
                members, history_texts, step
            )
            keys = self._members_to_keys(filtered)
            if not keys:
                logger.info("%s: 送信対象 0 件", label)
                continue
            logger.info("%s: %d 人へ送信開始", label, len(keys))
            sent = self._send_member_keys_phase(
                page,
                label,
                keys,
                budget,
                sent,
                sent_by_step,
                members=filtered[: len(keys)],
            )
        return sent

    def _execute_phased_send_pipeline(
        self,
        page: Page,
        budget: int,
        sent: int,
        sent_by_step: dict[str, int],
        skipped_steps: list[str],
    ) -> int:
        """フェーズ1（①〜③新規）→ ④マイガール古い順 → ⑤みたよ → ⑥⑦古い順."""
        self._check_job_control()
        gid = self._gid()
        logger.info("=== フェーズ1: 新規会員優先巡回（gid=%s）===", gid)

        new_matchings: list[Member] = []

        # ① キープ直打ち → マイガールタブクリック → 新規送信
        step1 = PriorityStep(
            tab="マイガール",
            member_filter="new_only",
            list_path=MYGIRL_LIST_PATH,
        )
        mygirl_members, mygirl_hist = self._fetch_tab_members(page, step1)
        new_mygirls = self._filter_new_members(
            mygirl_members, mygirl_hist, step1
        )
        if new_mygirls:
            logger.info("【1】新規マイガール %d 件 → 送信", len(new_mygirls))
            keys = self._members_to_keys(new_mygirls)
            sent = self._send_member_keys_phase(
                page,
                "①マイガール（新規）",
                keys,
                budget,
                sent,
                sent_by_step,
                members=new_mygirls,
            )
        else:
            logger.info("【1】新規マイガール 0 件")

        if sent >= budget:
            return sent

        self._check_job_control()
        # ② キープ一覧へ直打ち → 新規送信
        step2 = PriorityStep(
            tab="キープ",
            member_filter="new_only",
            list_path=KEEP_LIST_PATH,
        )
        keep_members, keep_hist = self._fetch_tab_members(page, step2)
        new_keeps = self._filter_new_members(keep_members, keep_hist, step2)
        if new_keeps:
            logger.info("【2】新規キープ %d 件 → 送信", len(new_keeps))
            keys = self._members_to_keys(new_keeps)
            sent = self._send_member_keys_phase(
                page,
                "②キープ（新規）",
                keys,
                budget,
                sent,
                sent_by_step,
                members=new_keeps,
            )
        else:
            logger.info("【2】新規キープ 0 件")

        if sent >= budget:
            return sent

        self._check_job_control()
        # ③ マッチ率（新規・残り回数ぶん）
        step3 = PriorityStep(
            tab="マッチ率",
            member_filter="new_only",
            list_path="/J10ComeonAiMatchingList.php",
        )
        match_members, match_hist = self._fetch_tab_members(page, step3)
        new_matchings = self._filter_new_members(
            match_members, match_hist, step3
        )
        self._match_rate_had_new = len(new_matchings) > 0
        if new_matchings:
            limit = budget - sent
            batch = new_matchings[:limit]
            logger.info(
                "【3】新規マッチ率 %d 件 → 残り %d 回分送信",
                len(new_matchings),
                len(batch),
            )
            keys = self._members_to_keys(batch)
            sent = self._send_member_keys_phase(
                page,
                "③マッチ率（新規）",
                keys,
                budget,
                sent,
                sent_by_step,
                members=batch,
            )
        else:
            logger.info("【3】新規マッチ率 0 件")

        if sent >= budget:
            return sent

        self._check_job_control()
        # ④ マイガール（送信日古い順）
        step4 = PriorityStep(
            tab="マイガール",
            member_filter="sent_oldest_first",
            list_path=MYGIRL_LIST_PATH,
        )
        mygirl_members2, mygirl_hist2 = self._fetch_tab_members(page, step4)
        mygirl_ordered = self._apply_step_member_filter(
            mygirl_members2, mygirl_hist2, step4
        )
        if mygirl_ordered:
            keys = self._members_to_keys(mygirl_ordered)
            logger.info("【4】マイガール（古い順） %d 件 → 送信", len(keys))
            sent = self._send_member_keys_phase(
                page,
                "④マイガール（古い順）",
                keys,
                budget,
                sent,
                sent_by_step,
                members=mygirl_ordered[: len(keys)],
            )
        else:
            logger.info("【4】マイガール（古い順）送信対象 0 件")

        if sent >= budget:
            return sent

        self._check_job_control()
        # ⑤ みたよ（③で新規マッチ率0件のときのみ）
        step5 = PriorityStep(
            tab="みたよ",
            member_filter="sendable",
            list_path="/J10ComeonVisitorList.php",
        )
        visitor_members, visitor_hist = self._fetch_tab_members(page, step5)
        if not new_matchings:
            sendable = self._filter_sendable_members(
                visitor_members, visitor_hist, step5
            )
            if sendable:
                logger.info("【5】みたよ %d 件 → 送信", len(sendable))
                keys = self._members_to_keys(sendable)
                sent = self._send_member_keys_phase(
                    page,
                    "⑤みたよ",
                    keys,
                    budget,
                    sent,
                    sent_by_step,
                    members=sendable,
                )
            else:
                logger.info("【5】みたよ送信対象 0 件")
        else:
            logger.info("【SKIP】マッチ率に新規あり → みたよはスキップ")

        if sent >= budget:
            return sent

        sent = self._send_oldest_first_phases(
            page, budget, sent, sent_by_step
        )

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
            pre_count = self._prepare_list_page_before_collect(page, step.tab)
            self._log_list_page_before_parse(
                page, step.tab, pre_count, step=step
            )
            self._scroll_member_list_to_end(page)
            page.evaluate("window.scrollTo(0, 0)")
            self._pause_ms(300)
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
            self._check_job_control()
            for surface in self._iter_surfaces(page):
                if self._pickup_tab_bar_visible(surface):
                    return True
            self._pause_ms(400)
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
                            const text = (a.innerText || '').trim();
                            const r = a.getBoundingClientRect();
                            if (r.width < 20 || r.height < 12
                                || r.top > window.innerHeight * 0.35) {
                                return false;
                            }
                            if (href.includes('comeon')) return true;
                            if ((href.includes('girluserpage') || href.includes('tab='))
                                && (text === tabName || tabLabels.includes(text))) {
                                return true;
                            }
                            return false;
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
            self._check_job_control()
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
                self._record_send_attempt_from_last()
                failed_attempts += 1
                stall += 1
                if stall >= 5:
                    self._scroll_member_list(page)
                    self._refresh_send_button_queue(page, log_scan=False)
                    stall = 0
                continue
            self._record_send_attempt_from_last()
            failed_attempts = 0
            stall = 0
            scroll_rounds = 0
            sent += 1
            step_sent += 1
            sent_by_step[label] += 1
            self._send_done = sent
            self._emit_send_progress(sent, initial_budget)
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
        step = self._current_step
        if self._is_member_profile_page(page):
            if step:
                uids = self._collect_profile_tab_uids(page, step)
                if len(uids) > 1:
                    return True
            return self._count_mitene_send_buttons(page) > 0
        if self._is_on_comeon_list_page(page):
            tab = self._tab_name_from_page(page)
            members, _histories = self.collect_members(page, tab, quiet=True)
            return any(m.has_send_button for m in members)
        fallback = self._scan_send_buttons_fallback(page)
        return len(fallback) > 0

    def _wait_profile_tab_ready(
        self,
        page: Page,
        step: PriorityStep,
        *,
        timeout_ms: int = 16000,
    ) -> bool:
        """プロフィール型タブの読み込み待ち（再帰なし）."""
        deadline = time.monotonic() + timeout_ms / 1000
        surface = self._member_card_surface(page)
        while time.monotonic() < deadline:
            self._check_job_control()
            uids = self._collect_profile_tab_uids(page, step)
            btn_n = self._count_mitene_send_buttons(page)
            card_n = self._count_member_cards_on_surface(surface)
            if len(uids) > 1 or btn_n > 0 or card_n > 0:
                return True
            self._pause_ms(500)
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
        self._profile_uid_by_key.clear()

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

    def _log_send_pipeline_info(self) -> None:
        logger.info("=== ミテネ送信パイプライン ===")
        logger.info("対象抽出: collect_members → _apply_step_member_filter")
        logger.info(
            "送信順決定: ①②③新規 → ④マイガール古い順 → ⑤みたよ"
            " → ⑥⑦キープ・マッチ率古い順"
        )
        logger.info(
            "新規会員判定: div.kitene_question > span.question「ミテネ履歴」"
            "の span.answer に「送信済」なし（_apply_step_member_filter のみ）"
        )
        logger.info(
            "gid=%s キープ起点=%s",
            self._gid(),
            build_list_url(self._gid(), KEEP_LIST_PATH),
        )
        logger.info("ロジック版: %s", SEND_LOGIC_VERSION)

    def _member_extraction_debug_enabled(self) -> bool:
        if self.standard.member_extraction_debug:
            return True
        return os.environ.get("MITENE_MEMBER_EXTRACTION_DEBUG", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    def _ajax_list_load_wait(self) -> None:
        """Ajax 遅延読込待ち（scrollHeight だけでは足りないため 2〜3 秒）."""
        self._pause_ms(random.randint(*LIST_AJAX_LOAD_WAIT_MS))

    def _scroll_list_to_bottom(self, page: Page) -> None:
        page.evaluate(
            "window.scrollTo(0, Math.max("
            "document.body.scrollHeight, document.documentElement.scrollHeight))"
        )

    def _get_list_scroll_height(self, page: Page) -> int:
        try:
            return int(
                page.evaluate(
                    """() => Math.max(
                        document.body.scrollHeight,
                        document.documentElement.scrollHeight
                    )"""
                )
            )
        except Exception:
            return 0

    def _parse_tab_member_total_count(
        self, page: Page, tab_name: str
    ) -> int | None:
        """一覧またはホームに表示される「現在の○○数」を取得."""
        pat = TAB_MEMBER_TOTAL_PATTERNS.get(tab_name)
        if not pat:
            return None
        try:
            text = _normalize_digits(page.inner_text("body") or "")
            m = pat.search(text)
            if m:
                raw = m.group(1).replace(",", "").replace("，", "")
                return int(raw)
        except Exception:
            pass
        return None

    def _log_list_fetch_vs_expected(
        self, tab_name: str, fetched: int, expected: int
    ) -> None:
        if self._counts_roughly_match(fetched, expected, tolerance=0.02):
            logger.info(
                "【%s】取得件数 %d / %d（照合OK）", tab_name, fetched, expected
            )
            return
        logger.warning("【%s】取得件数 %d / %d", tab_name, fetched, expected)
        logger.warning("【%s】まだ一覧取得が不足しています", tab_name)

    def _scroll_merge_parse_enabled(self) -> bool:
        """スクロール走査マージは解析漏れ調査用の暫定実装（通常オフ）."""
        if self.standard.member_scroll_merge_parse:
            return True
        return os.environ.get("MITENE_SCROLL_MERGE_PARSE", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    def _counts_roughly_match(self, a: int, b: int, *, tolerance: float = 0.02) -> bool:
        if a <= 0 and b <= 0:
            return True
        if a <= 0 or b <= 0:
            return False
        return abs(a - b) <= max(2, int(a * tolerance))

    def _log_collect_count_verification(
        self,
        tab_name: str,
        *,
        stable_card_count: int,
        dom_count: int,
        member_count: int,
        parse_mode: str,
    ) -> None:
        """スクロール後カード数と最終 Member 数の照合（単回解析で十分か検証）."""
        logger.info("【%s】件数照合", tab_name)
        logger.info("  解析方式: %s", parse_mode)
        logger.info("  DOM安定後カード数: %d", stable_card_count)
        logger.info("  DOM解析: %d", dom_count)
        logger.info("  Member生成: %d", member_count)
        dom_ok = self._counts_roughly_match(stable_card_count, dom_count)
        mem_ok = self._counts_roughly_match(stable_card_count, member_count)
        if dom_ok and mem_ok:
            logger.info("  照合結果: ほぼ一致")
            return
        logger.warning(
            "  照合結果: 不一致（カード %d → DOM %d → Member %d）",
            stable_card_count,
            dom_count,
            member_count,
        )
        if not self._scroll_merge_parse_enabled():
            logger.warning(
                "  調査時のみ MITENE_SCROLL_MERGE_PARSE=1 または"
                " config member_scroll_merge_parse: true で走査マージを有効化"
            )

    def _log_collect_stage_debug(
        self, tab_name: str, stage: str, page: Page | None = None
    ) -> None:
        if not self._member_extraction_debug_enabled():
            return
        if stage == "URL" and page is not None:
            logger.info("【%s】URL: %s", tab_name, page.url or "")
        else:
            logger.info("【%s】%s", tab_name, stage)

    def _log_no_send_button_members_debug(self, members: list[Member]) -> None:
        if not self._member_extraction_debug_enabled():
            return
        no_btn = [m for m in members if not m.has_send_button]
        if not no_btn:
            return
        logger.info("↓")
        logger.info("送信ボタンなし会員")
        for member in no_btn:
            dom = self._debug_member_dom.get(member.member_id, {})
            logger.info("%s", member.member_id)
            logger.info("%s", member.name)
            logger.info("innerText: %s", dom.get("inner_text") or "（未取得）")
            logger.info(
                "outerHTML先頭300: %s",
                (dom.get("outer_html") or "（未取得）")[:300],
            )

    def _remember_debug_member_name(self, member_id: str, name: str) -> None:
        mid = (member_id or "").strip()
        if mid:
            self._debug_member_names[mid] = name or "（名前不明）"

    def _log_per_send_debug_header(self, member_id: str, name: str) -> None:
        if not self._member_extraction_debug_enabled():
            return
        logger.info("------ 送信1件デバッグ ------")
        logger.info("member_id: %s", member_id)
        logger.info("名前: %s", name)

    def _log_per_send_debug_success(
        self,
        *,
        cta_ok: bool,
        modal_shown: bool,
        ok_clicked: bool,
        remaining_before: int | None,
        remaining_after: int | None,
    ) -> None:
        if not self._member_extraction_debug_enabled():
            return
        logger.info("CTAクリック成功: %s", "はい" if cta_ok else "いいえ")
        logger.info("確認モーダル表示: %s", "はい" if modal_shown else "いいえ")
        logger.info("OKクリック: %s", "はい" if ok_clicked else "いいえ")
        if remaining_before is not None and remaining_after is not None:
            logger.info("残回数: %s→%s", remaining_before, remaining_after)
        else:
            logger.info(
                "残回数: %s→%s",
                remaining_before if remaining_before is not None else "?",
                remaining_after if remaining_after is not None else "?",
            )
        logger.info("送信成功")

    def _log_per_send_debug_failure(
        self,
        *,
        cta_ok: bool,
        modal_shown: bool,
        ok_clicked: bool,
        remaining_before: int | None,
        remaining_after: int | None,
        reason: str,
    ) -> None:
        if not self._member_extraction_debug_enabled():
            return
        logger.info("CTAクリック成功: %s", "はい" if cta_ok else "いいえ")
        logger.info("確認モーダル表示: %s", "はい" if modal_shown else "いいえ")
        logger.info("OKクリック: %s", "はい" if ok_clicked else "いいえ")
        if remaining_before is not None and remaining_after is not None:
            logger.info("残回数: %s→%s", remaining_before, remaining_after)
        else:
            logger.info(
                "残回数: %s→%s",
                remaining_before if remaining_before is not None else "?",
                remaining_after if remaining_after is not None else "?",
            )
        logger.info("送信失敗理由: %s", reason)

    def _member_filter_exclusion_reason(self, member: Member, mode: str) -> str:
        key = member_queue_key(member.member_id)
        if key in self._sent_member_keys:
            return "本実行で送信済"
        if key in self._failed_member_keys:
            return "送信失敗"
        if not member.has_send_button:
            return "送信ボタンなし"
        if mode == "new_only" and member.sent_history:
            return "送信済"
        if mode == "sent_oldest_first" and not member.sent_history:
            return "送信履歴なし"
        return "フィルタ条件不一致"

    def _build_filter_exclusions(
        self,
        enriched: list[Member],
        filtered: list[Member],
        step: PriorityStep,
        *,
        duplicates: list[Member] | None = None,
    ) -> list[tuple[Member, str]]:
        mode = step.member_filter or "sendable"
        filtered_ids = {m.member_id for m in filtered}
        seen: set[str] = set()
        exclusions: list[tuple[Member, str]] = []
        for member in duplicates or []:
            if not member.member_id or member.member_id in seen:
                continue
            seen.add(member.member_id)
            exclusions.append((member, "重複"))
        for member in enriched:
            if member.member_id in filtered_ids or member.member_id in seen:
                continue
            seen.add(member.member_id)
            exclusions.append(
                (member, self._member_filter_exclusion_reason(member, mode))
            )
        return exclusions

    def _log_member_extraction_debug(
        self,
        step: PriorityStep,
        members: list[Member],
        enriched: list[Member],
        filtered: list[Member],
        *,
        duplicates: list[Member] | None = None,
    ) -> None:
        """デバッグモード: タブごとの抽出〜フィルタ漏斗ログ."""
        tab = step.tab
        total = len(members)
        with_btn = sum(1 for m in members if m.has_send_button)
        sent_n = sum(1 for m in enriched if m.sent_history)
        unsent_n = sum(
            1 for m in enriched if m.has_send_button and not m.sent_history
        )
        exclusions = self._build_filter_exclusions(
            enriched, filtered, step, duplicates=duplicates
        )

        logger.info("======================")
        logger.info("%s", tab)
        logger.info("======================")
        logger.info("取得カード数: %d", total)
        logger.info("↓")
        logger.info("送信ボタンあり: %d", with_btn)
        logger.info("↓")
        logger.info("送信済: %d", sent_n)
        logger.info("↓")
        logger.info("未送信: %d", unsent_n)
        logger.info("↓")
        logger.info("Filter後: %d", len(filtered))
        if exclusions:
            logger.info("↓")
            logger.info("除外")
            for member, reason in exclusions:
                logger.info("%s", member.member_id)
                logger.info("%s", member.name)
                logger.info("理由：%s", reason)
        if filtered:
            logger.info("↓")
            logger.info("送信対象")
            for member in filtered:
                logger.info("%s", member.member_id)
                logger.info("%s", member.name)
                self._remember_debug_member_name(member.member_id, member.name)
        self._log_no_send_button_members_debug(members)

    def _count_list_cards_on_page(self, page: Page) -> int:
        return self._count_member_cards_on_surface(self._member_card_surface(page))

    def _ensure_on_step_list_for_parse(
        self, page: Page, step: PriorityStep
    ) -> bool:
        """解析前に J10 一覧URL上にいることを保証する（プロフィールURLは不可）."""
        for attempt in range(1, 4):
            on_profile = self._is_member_profile_page(page)
            verified = self._verify_step_list(page, step)
            logger.info(
                "【%s】一覧確認 (試行%d): URL=%s"
                " _is_member_profile_page=%s _verify_step_list=%s",
                step.tab,
                attempt,
                page.url or "",
                on_profile,
                verified,
            )
            if not on_profile and verified:
                return True
            if on_profile:
                logger.warning(
                    "【%s】プロフィールページ上だったため一覧へ戻します",
                    step.tab,
                )
            else:
                logger.warning(
                    "【%s】一覧未確認のため一覧を開き直します",
                    step.tab,
                )
            if not self._open_step_list(page, step):
                self._navigate_to_url_safe(page, step, force_reload=True)
            self._pause_ms(500)
        on_profile = self._is_member_profile_page(page)
        verified = self._verify_step_list(page, step)
        if not on_profile and verified:
            return True
        logger.warning(
            "【%s】一覧ページへ戻せません: URL=%s profile=%s verify=%s",
            step.tab,
            page.url or "",
            on_profile,
            verified,
        )
        return False

    def _ensure_list_from_profile(self, page: Page, tab_name: str) -> None:
        """プロフィールページなら J10 一覧へ戻る（全タブ共通）."""
        if not self._is_member_profile_page(page):
            return
        step = self._current_step
        logger.info("【%s】プロフィールページのため一覧へ戻る", tab_name)
        if step and step.tab == tab_name:
            if self._open_step_list(page, step):
                self._wait_page_settled(page, quick=True)
                return
            self._navigate_to_url_safe(page, step, force_reload=True)
            return
        path = TAB_LIST_PATHS.get(tab_name, "")
        if path:
            url = self._list_url(page, path)
            if url and self._safe_goto(page, url):
                self._wait_page_settled(page, quick=True)

    def _is_list_loading_visible(self, page: Page) -> bool:
        try:
            return not bool(page.evaluate(LIST_LOADING_GONE_JS))
        except Exception:
            return False

    def _poll_wait_for_list_render(
        self, page: Page, tab_name: str
    ) -> tuple[int, bool]:
        """
        500ms ごとに会員カード数・ローディング状態を確認（最大8秒）。
        カード1件以上で終了。タイムアウトしても続行。
        """
        deadline = time.monotonic() + LIST_RENDER_WAIT_MAX_MS / 1000
        card_n = 0
        poll = 0
        while time.monotonic() < deadline:
            self._check_job_control()
            poll += 1
            card_n = self._count_list_cards_on_page(page)
            loading = self._is_list_loading_visible(page)
            if card_n > 0:
                logger.info(
                    "【%s】一覧描画待機完了: カード %d件"
                    "（%d ms・poll %d）",
                    tab_name,
                    card_n,
                    poll * LIST_RENDER_POLL_MS,
                    poll,
                )
                return card_n, True
            if not loading and poll > 1:
                logger.debug(
                    "【%s】ローディング終了・カード未検出"
                    "（poll %d・継続待機）",
                    tab_name,
                    poll,
                )
            self._pause_ms(LIST_RENDER_POLL_MS)
        logger.warning(
            "【%s】一覧描画待機タイムアウト（%d秒）"
            " 最終カード数=%d",
            tab_name,
            LIST_RENDER_WAIT_MAX_MS // 1000,
            card_n,
        )
        return card_n, False

    def _log_list_page_before_parse(
        self,
        page: Page,
        tab_name: str,
        card_count: int,
        *,
        step: PriorityStep | None = None,
    ) -> None:
        """解析開始直前の診断ログ（全タブ共通）."""
        try:
            title = page.title()
        except Exception:
            title = ""
        try:
            ready_state = page.evaluate("() => document.readyState")
        except Exception:
            ready_state = "?"
        on_profile = self._is_member_profile_page(page)
        verified = (
            self._verify_step_list(page, step)
            if step is not None
            else None
        )
        logger.info("【%s】解析開始直前", tab_name)
        logger.info("  URL: %s", page.url or "")
        logger.info("  _is_member_profile_page(): %s", on_profile)
        if verified is not None:
            logger.info("  _verify_step_list(): %s", verified)
        logger.info("  会員カード数: %d", card_count)
        logger.info("  ページタイトル: %s", title)
        logger.info("  document.readyState: %s", ready_state)
        if card_count == 0:
            self._save_list_page_html_dump(page, tab_name)

    def _save_list_page_html_dump(self, page: Page, tab_name: str) -> None:
        """カード0件時のみ page.content() をログファイルへ保存."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_tab = re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff-]+", "_", tab_name)
        path = self.log_dir / f"list_html_{safe_tab}_{stamp}.html"
        try:
            html = page.content()
            path.write_text(html, encoding="utf-8")
            logger.warning(
                "【%s】カード0件のため HTML を保存: %s", tab_name, path
            )
        except Exception as exc:
            logger.warning("【%s】HTML保存失敗: %s", tab_name, exc)

    def _prepare_list_page_before_collect(
        self, page: Page, tab_name: str
    ) -> int:
        """
        DOM解析・スクロール前の全タブ共通準備。
        プロフィール→一覧 / 一覧描画ポーリング待機。
        """
        logger.info("【%s】一覧準備開始", tab_name)
        self._ensure_list_from_profile(page, tab_name)
        card_n, _ = self._poll_wait_for_list_render(page, tab_name)
        return card_n

    def _log_pipeline_funnel_stage(
        self,
        tab_name: str,
        label: str,
        count: int,
        *,
        arrow: bool = True,
    ) -> None:
        """抽出〜送信キューまでの段階件数（各段階必ず出力）."""
        logger.info("【%s】%s: %d", tab_name, label, count)
        if arrow:
            logger.info("↓")

    def _log_pipeline_funnel_queue(
        self, tab_name: str, queue_count: int
    ) -> None:
        self._log_pipeline_funnel_stage(
            tab_name, "最終送信キュー数", queue_count, arrow=False
        )

    def _log_member_extraction_queue_debug(self, queue_count: int) -> None:
        tab = (
            self._current_step.tab
            if self._current_step
            else self._tab_name_from_page_if_list()
        )
        self._log_pipeline_funnel_queue(tab, queue_count)

    def _tab_name_from_page_if_list(self) -> str:
        return "一覧"

    def _history_text_is_sent(self, history_text: str) -> bool:
        """一覧DOMのミテネ履歴テキストから送信済みか判定（filter専用）."""
        return not is_new_member_from_history((history_text or "").strip())

    def _enrich_member_sent_fields(
        self, member: Member, history_text: str
    ) -> Member:
        sent = self._history_text_is_sent(history_text)
        last_sent = (
            self._parse_history_date(history_text) if sent else None
        )
        return Member(
            member_id=member.member_id,
            name=member.name,
            has_send_button=member.has_send_button,
            sent_history=sent,
            last_sent=last_sent,
        )

    def _log_final_send_targets(
        self, members: list[Member], reason: str
    ) -> None:
        if not members:
            return
        logger.info("送信対象")
        for i, member in enumerate(members, 1):
            logger.info("%d.", i)
            logger.info("member_id=%s", member.member_id)
            logger.info("名前: %s", member.name)
            logger.info("送信理由: %s", reason)

    def collect_members(
        self,
        page: Page,
        tab_name: str,
        *,
        quiet: bool = False,
        list_prepared: bool = False,
        pre_card_count: int | None = None,
    ) -> tuple[list[Member], dict[str, str]]:
        """
        タブの会員一覧を収集する共通入口。
        一覧描画待機 → スクロール → DOM解析 → Member 生成。
        """
        step = self._current_step
        if list_prepared:
            pre_count = (
                pre_card_count
                if pre_card_count is not None
                else self._count_list_cards_on_page(page)
            )
        else:
            pre_count = self._prepare_list_page_before_collect(page, tab_name)
            if (
                step
                and step.tab == tab_name
                and not self._ensure_on_step_list_for_parse(page, step)
            ):
                logger.warning(
                    "【%s】一覧ページを確認できないため解析を中止", tab_name
                )
                return [], {}
            self._log_list_page_before_parse(
                page, tab_name, pre_count, step=step
            )
            if step and step.tab == tab_name:
                if self._is_member_profile_page(page) or not self._verify_step_list(
                    page, step
                ):
                    logger.warning(
                        "【%s】一覧でないため解析を中止: %s",
                        tab_name,
                        page.url,
                    )
                    return [], {}
        debug_on = self._member_extraction_debug_enabled()
        self._log_pipeline_funnel_stage(
            tab_name,
            "スクロール前カード数",
            pre_count,
        )
        if debug_on:
            self._log_collect_stage_debug(tab_name, "URL", page)
            self._log_collect_stage_debug(tab_name, "スクロール走査開始")
        raw_cards = self._parse_list_page_cards(page, tab_name)
        summary = getattr(self, "_last_list_fetch_summary", {}) or {}
        post_count = int(summary.get("final_card_count") or len(raw_cards))

        if post_count == 0 and not raw_cards:
            logger.info(
                "【%s】スクロール後0件 → %d秒追加待機して再取得",
                tab_name,
                LIST_ZERO_RETRY_WAIT_MS // 1000,
            )
            self._pause_ms(LIST_ZERO_RETRY_WAIT_MS)
            pre_count = self._prepare_list_page_before_collect(page, tab_name)
            self._log_list_page_before_parse(
                page, tab_name, pre_count, step=step
            )
            if pre_count > 0:
                self._invalidate_list_cache()
                raw_cards = self._parse_list_page_cards(page, tab_name)
                summary = getattr(self, "_last_list_fetch_summary", {}) or {}
                post_count = int(
                    summary.get("final_card_count") or len(raw_cards)
                )

        if post_count == 0 and not raw_cards:
            logger.warning("【%s】会員0件", tab_name)
            self._log_pipeline_funnel_stage(
                tab_name, "スクロール後カード数", 0, arrow=False
            )
            return [], {}

        self._log_pipeline_funnel_stage(
            tab_name, "スクロール後カード数", post_count
        )
        self._log_pipeline_funnel_stage(
            tab_name, "DOM安定後カード数", post_count
        )
        self._log_pipeline_funnel_stage(
            tab_name, "DOM解析カード数", len(raw_cards)
        )
        if debug_on:
            self._log_collect_stage_debug(
                tab_name, f"カード取得完了: {len(raw_cards)}件"
            )

        members: list[Member] = []
        history_texts: dict[str, str] = {}
        seen: set[str] = set()
        duplicates: list[Member] = []
        gen_stats: dict[str, int] = {
            "member_idなし": 0,
            "重複": 0,
            "不正な型": 0,
        }
        for card in raw_cards:
            if not isinstance(card, dict):
                gen_stats["不正な型"] += 1
                continue
            member_id = str(card.get("member_id") or "").strip()
            if not member_id:
                gen_stats["member_idなし"] += 1
                continue
            card_name = str(card.get("name") or "（名前不明）")
            if member_id in seen:
                gen_stats["重複"] += 1
                dup_member = Member(
                    member_id=member_id,
                    name=card_name,
                    has_send_button=bool(card.get("has_send_button")),
                )
                duplicates.append(dup_member)
                if debug_on:
                    self._log_exclusion_immediate(dup_member, "重複")
                continue
            seen.add(member_id)
            history_text = str(card.get("history_text") or "").strip()
            history_texts[member_id] = history_text
            self._remember_debug_member_name(member_id, card_name)
            if debug_on:
                self._debug_member_dom[member_id] = {
                    "inner_text": str(card.get("inner_text") or ""),
                    "outer_html": str(card.get("outer_html") or "")[:500],
                }
            members.append(
                Member(
                    member_id=member_id,
                    name=card_name,
                    has_send_button=bool(card.get("has_send_button")),
                )
            )

        self._log_member_gen_stats(tab_name, len(raw_cards), gen_stats, len(members))

        if members and sum(1 for m in members if m.has_send_button) == 0:
            fallback = self._scan_send_buttons_fallback(page)
            for item in fallback:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("mid") or "").strip()
                if not mid:
                    continue
                if mid in seen:
                    dup_member = Member(
                        member_id=mid,
                        name="（名前不明）",
                        has_send_button=True,
                    )
                    duplicates.append(dup_member)
                    if debug_on:
                        self._log_exclusion_immediate(dup_member, "重複")
                    continue
                seen.add(mid)
                history_texts[mid] = str(item.get("history_text") or "")
                members.append(
                    Member(
                        member_id=mid,
                        name="（名前不明）",
                        has_send_button=True,
                    )
                )
            if fallback:
                logger.info(
                    "【%s】送信ボタン再検出: フォールバック %d 件",
                    tab_name,
                    len(fallback),
                )

        self._debug_collect_duplicates[tab_name] = duplicates

        self._log_pipeline_funnel_stage(tab_name, "Member生成数", len(members))
        logger.info(
            "【%s】collect_members完了: DOM解析 %d件 → Member %d件"
            "（送信ボタンあり %d件）",
            tab_name,
            len(raw_cards),
            len(members),
            sum(1 for m in members if m.has_send_button),
        )
        self._log_collect_count_verification(
            tab_name,
            stable_card_count=post_count,
            dom_count=len(raw_cards),
            member_count=len(members),
            parse_mode="スクロール走査マージ",
        )

        if debug_on:
            self._log_tab_correlation_stats_debug(page, tab_name, members)

        if not quiet and not self._member_extraction_debug_enabled():
            logger.info("===== %s =====", tab_name)
            logger.info("取得カード数: %d", len(members))
            logger.info(
                "送信ボタンあり: %d",
                sum(1 for m in members if m.has_send_button),
            )
        return members, history_texts

    def _log_card_parse_stats(
        self,
        tab_name: str,
        stats: dict[str, Any],
        final_count: int,
        py_exclusions: dict[str, int],
    ) -> None:
        """MEMBER_CARD_PARSE_JS のセレクタ別件数・除外理由を必ず出力."""
        logger.info("【%s】MEMBER_CARD_PARSE_JS セレクタ別件数", tab_name)
        selector_hits = stats.get("selectorHits") or {}
        for sel, count in selector_hits.items():
            logger.info("  %s = %d", sel, count)
        logger.info(
            "  合計(querySelectorAll) = %d",
            int(stats.get("selectorHitsTotal") or 0),
        )
        logger.info("【%s】DOM解析内訳", tab_name)
        logger.info(
            "  DOM取得(prune前) %d件",
            int(stats.get("rawNodesBeforePrune") or 0),
        )
        logger.info(
            "  nested除去 %d件", int(stats.get("nestedPruned") or 0)
        )
        logger.info(
            "  prune後ノード %d件", int(stats.get("nodesAfterPrune") or 0)
        )
        logger.info(
            "  duplicateIdマージ %d件",
            int(stats.get("duplicateIdMerged") or 0),
        )
        logger.info(
            "  member_idなし %d件", int(stats.get("noMemberId") or 0)
        )
        logger.info(
            "  nameなし %d件", int(stats.get("nameMissing") or 0)
        )
        logger.info(
            "  historyなし %d件", int(stats.get("historyMissing") or 0)
        )
        logger.info(
            "  親要素からID復元 %d件",
            int(stats.get("recoveredFromParent") or 0),
        )
        logger.info(
            "  スクロール走査 %d回", int(stats.get("scrollPasses") or 0)
        )
        logger.info(
            "  JS uniqueMemberIds %d件",
            int(stats.get("uniqueMemberIds") or 0),
        )
        if py_exclusions:
            logger.info("【%s】Python DOM解析除外", tab_name)
            for reason, count in py_exclusions.items():
                if count:
                    logger.info("  %s %d件", reason, count)
        logger.info("  最終 %d件", final_count)

    def _log_member_gen_stats(
        self,
        tab_name: str,
        raw_count: int,
        gen_stats: dict[str, int],
        member_count: int,
    ) -> None:
        logger.info("【%s】Member生成", tab_name)
        logger.info("  入力(DOM解析済) %d件", raw_count)
        excluded = sum(gen_stats.values())
        if excluded:
            logger.info("  Member生成失敗")
            for reason, count in gen_stats.items():
                if count:
                    logger.info("    %s %d件", reason, count)
        logger.info("  最終 %d件", member_count)

    def _card_eval_row_to_dict(
        self,
        item: dict[str, Any],
        tab_name: str,
        py_exclusions: dict[str, int],
    ) -> dict[str, Any] | None:
        mid = str(item.get("mid") or "").strip()
        uid = str(item.get("uid") or "").strip()
        member_id = mid or uid
        name = str(item.get("name") or "（名前不明）")
        inner_text = str(item.get("cardText") or "")
        outer_html = str(
            item.get("cardOuterHtmlHead") or item.get("cardHtmlHead") or ""
        )[:500]
        if not member_id:
            py_exclusions["member_idなし"] = (
                py_exclusions.get("member_idなし", 0) + 1
            )
            if py_exclusions["member_idなし"] <= 5:
                self._log_locator_correlation_failure(
                    tab_name,
                    member_id=mid or uid,
                    name=name,
                    inner_text=inner_text,
                    outer_html=outer_html,
                )
            return None
        return {
            "member_id": member_id,
            "name": name,
            "has_send_button": bool(item.get("hasSendButton")),
            "history_text": str(item.get("historyText") or "").strip(),
            "inner_text": inner_text,
            "outer_html": outer_html,
        }

    def _rows_to_parsed_cards(
        self,
        rows: list[dict[str, Any]],
        tab_name: str,
        py_exclusions: dict[str, int],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for item in rows:
            if not isinstance(item, dict):
                py_exclusions["不正な型"] = (
                    py_exclusions.get("不正な型", 0) + 1
                )
                continue
            parsed_item = self._card_eval_row_to_dict(
                item, tab_name, py_exclusions
            )
            if not parsed_item:
                continue
            mid = parsed_item["member_id"]
            existing = merged.get(mid)
            if not existing or _card_dict_richness(
                parsed_item
            ) > _card_dict_richness(existing):
                merged[mid] = parsed_item
        return list(merged.values())

    def _evaluate_member_cards(
        self,
        surface: Any,
        tab_name: str,
        parse_arg: dict[str, str],
        py_exclusions: dict[str, int],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        raw = surface.evaluate(MEMBER_CARD_PARSE_JS, parse_arg)
        rows, stats = _extract_card_parse_result(raw)
        if not rows and raw is not None:
            if not (
                isinstance(raw, dict)
                and isinstance(raw.get("cards"), list)
                and len(raw["cards"]) == 0
            ):
                logger.warning(
                    "【%s一覧】evaluate 戻り値が不正: type=%s",
                    tab_name,
                    type(raw).__name__,
                )
        parsed = self._rows_to_parsed_cards(rows, tab_name, py_exclusions)
        return parsed, stats

    def _scroll_parse_merge_list(
        self,
        page: Page,
        tab_name: str,
        surface: Any,
        parse_arg: dict[str, str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        スクロール → Ajax待ち → DOM解析 → member_idマージ を終了条件まで繰り返す。
        終了: scrollHeight / カード数 / uniqueMemberIds が連続で変化しないこと。
        """
        merged: dict[str, dict[str, Any]] = {}
        agg_stats: dict[str, Any] = {}
        py_exclusions: dict[str, int] = {
            "member_idなし": 0,
            "不正な型": 0,
        }
        expected_total = self._parse_tab_member_total_count(page, tab_name)
        if expected_total:
            logger.info(
                "【%s】現在の%s数: %d件", tab_name, tab_name, expected_total
            )

        try:
            page.evaluate("window.scrollTo(0, 0)")
            self._pause_ms(400)
        except Exception:
            pass

        prev_metrics = {
            "scroll_height": -1,
            "card_count": -1,
            "unique_ids": -1,
        }
        stable_rounds = 0
        final_card_count = 0
        final_scroll_height = 0
        round_no = 0

        for round_i in range(LIST_SCROLL_PARSE_MAX_ROUNDS):
            self._check_job_control()
            round_no = round_i + 1

            try:
                page.evaluate(
                    "window.scrollBy(0, Math.min(window.innerHeight * 0.75, 800))"
                )
                self._pause_ms(400)
                self._scroll_list_to_bottom(page)
            except Exception:
                pass
            self._ajax_list_load_wait()

            scroll_height = self._get_list_scroll_height(page)
            card_count = self._count_member_cards_on_surface(surface)

            try:
                batch, stats = self._evaluate_member_cards(
                    surface, tab_name, parse_arg, py_exclusions
                )
            except Exception as e:
                if round_i == 0:
                    logger.warning(
                        "【%s一覧】カード解析失敗: %s", tab_name, e
                    )
                break

            _merge_parse_stats(agg_stats, stats, scroll_pass=round_no)

            for parsed_item in batch:
                mid = parsed_item["member_id"]
                existing = merged.get(mid)
                if not existing or _card_dict_richness(
                    parsed_item
                ) > _card_dict_richness(existing):
                    merged[mid] = parsed_item

            unique_ids = len(merged)
            final_card_count = card_count
            final_scroll_height = scroll_height

            logger.info("スクロール走査 %d回目", round_no)
            logger.info("  カード数: %d", card_count)
            logger.info("  uniqueMemberIds: %d", unique_ids)
            logger.info("  scrollHeight: %d", scroll_height)

            if (
                scroll_height == prev_metrics["scroll_height"]
                and card_count == prev_metrics["card_count"]
                and unique_ids == prev_metrics["unique_ids"]
            ):
                stable_rounds += 1
                if stable_rounds >= LIST_SCROLL_STABLE_ROUNDS:
                    logger.info(
                        "【%s】スクロール終了: scrollHeight・カード数・"
                        "uniqueMemberIds が %d 回連続で変化なし",
                        tab_name,
                        LIST_SCROLL_STABLE_ROUNDS,
                    )
                    break
            else:
                stable_rounds = 0

            prev_metrics = {
                "scroll_height": scroll_height,
                "card_count": card_count,
                "unique_ids": unique_ids,
            }

            if (
                expected_total
                and unique_ids >= expected_total - 3
                and stable_rounds >= 2
            ):
                logger.info(
                    "【%s】期待件数 %d に到達（取得 %d）",
                    tab_name,
                    expected_total,
                    unique_ids,
                )
                break
        else:
            logger.warning(
                "【%s】スクロール走査が上限 %d 回に達しました",
                tab_name,
                LIST_SCROLL_PARSE_MAX_ROUNDS,
            )

        parsed = list(merged.values())
        logger.info("【%s】最終取得", tab_name)
        logger.info("  カード数: %d", final_card_count)
        logger.info("  uniqueMemberIds: %d", len(parsed))
        logger.info("  scrollHeight: %d", final_scroll_height)

        if expected_total:
            self._log_list_fetch_vs_expected(
                tab_name, len(parsed), expected_total
            )

        agg_stats["scrollPasses"] = round_no
        self._log_card_parse_stats(
            tab_name, agg_stats, len(parsed), py_exclusions
        )
        self._debug_parse_id_fail_count = py_exclusions.get("member_idなし", 0)

        summary = {
            "final_card_count": final_card_count,
            "unique_member_ids": len(parsed),
            "expected_total": expected_total,
            "scroll_passes": round_no,
            "scroll_height": final_scroll_height,
        }
        return parsed, summary

    def _parse_list_page_cards(
        self, page: Page, tab_name: str
    ) -> list[dict[str, Any]]:
        """collect_members() 内部専用: スクロール走査マージでカード取得."""
        url = page.url or ""
        if self._cached_list_url == url and self._cached_list_cards is not None:
            return [
                c for c in self._cached_list_cards if isinstance(c, dict)
            ]

        history_label = self.standard.mitene_history_label
        surface = self._member_card_surface(page)
        parse_arg = {"historyLabel": history_label}

        logger.info("【%s】DOM解析: スクロール走査マージ", tab_name)
        parsed, summary = self._scroll_parse_merge_list(
            page, tab_name, surface, parse_arg
        )
        self._last_list_fetch_summary = summary

        if not parsed:
            self._log_member_card_selector_debug(surface, tab_name)
        self._cached_list_cards = parsed
        self._cached_list_url = url
        return parsed

    def _filtered_members_to_queue_dicts(
        self, filtered: list[Member]
    ) -> list[dict[str, Any]]:
        """filter 済み Member → 送信キュー用 dict."""
        result: list[dict[str, Any]] = []
        for member in filtered:
            key = member_queue_key(member.member_id)
            if key in self._sent_member_keys or key in self._failed_member_keys:
                continue
            if not member.has_send_button:
                continue
            result.append(
                {
                    "key": key,
                    "mid": member.member_id,
                    "name": member.name,
                    "sent_history": member.sent_history,
                    "has_send_button": member.has_send_button,
                    "history_date": member.last_sent,
                }
            )
        return result

    def _members_from_fallback_items(
        self, fallback: list[dict[str, Any]]
    ) -> tuple[list[Member], dict[str, str]]:
        members: list[Member] = []
        history_texts: dict[str, str] = {}
        for item in fallback:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            mid = str(item.get("mid") or "").strip()
            if not mid and key.startswith("comeon-"):
                mid = key[7:]
            if not mid:
                continue
            history_texts[mid] = str(item.get("history_text") or "")
            members.append(
                Member(
                    member_id=mid,
                    name="（名前不明）",
                    has_send_button=bool(item.get("has_send_button", True)),
                )
            )
        return members, history_texts

    def _scan_member_cards(self, page: Page) -> list[dict[str, Any]]:
        """送信ループ用: Member 取得 → _apply_step_member_filter → dict キュー."""
        step = self._current_step
        members: list[Member] = []
        history_texts: dict[str, str] = {}

        if self._is_member_profile_page(page):
            if step and self._is_step_profile_page(page, step):
                cards = self._parse_profile_tab_members(page, step)
                members, history_texts = self._raw_cards_to_collected(cards)
            else:
                return []
        elif self._is_on_comeon_list_page(page):
            tab = self._tab_name_from_page(page)
            members, history_texts = self.collect_members(page, tab, quiet=True)
        else:
            fallback = self._scan_send_buttons_fallback(page)
            members, history_texts = self._members_from_fallback_items(fallback)

        tab_name = (
            step.tab
            if step
            else (
                self._tab_name_from_page(page)
                if self._is_on_comeon_list_page(page)
                else "一覧"
            )
        )
        filter_step = step or PriorityStep(
            tab=tab_name,
            member_filter="sendable",
            list_path=TAB_LIST_PATHS.get(tab_name, ""),
        )
        filtered = self._apply_step_member_filter(
            members, history_texts, filter_step, quiet=True
        )
        result = self._filtered_members_to_queue_dicts(filtered)
        if result:
            return result

        fallback = self._scan_send_buttons_fallback(page)
        if not fallback:
            return []
        fb_members, fb_histories = self._members_from_fallback_items(fallback)
        if not fb_members:
            return []
        filtered_fb = self._apply_step_member_filter(
            fb_members, fb_histories, filter_step, quiet=True
        )
        return self._filtered_members_to_queue_dicts(filtered_fb)

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
                    "has_send_button": True,
                }
            )
        if result:
            logger.info("カード走査フォールバック: %d 人検出", len(result))
        return result

    def _apply_step_member_filter(
        self,
        members: list[Member],
        history_texts: dict[str, str],
        step: PriorityStep,
        *,
        quiet: bool = False,
    ) -> list[Member]:
        """タブごとの会員条件（未送信のみ / 送信日古い順 / 全員）— 送信済判定はここだけ."""
        enriched = [
            self._enrich_member_sent_fields(
                m, history_texts.get(m.member_id, "")
            )
            for m in members
            if m.member_id
        ]
        debug_on = self._member_extraction_debug_enabled()
        log_verbose = not quiet or debug_on
        duplicates = self._debug_collect_duplicates.pop(step.tab, [])

        mode = step.member_filter or "sendable"
        if mode == "new_only":
            filtered = [
                m
                for m in enriched
                if m.has_send_button
                and not m.sent_history
                and member_queue_key(m.member_id)
                not in self._sent_member_keys
                and member_queue_key(m.member_id)
                not in self._failed_member_keys
            ]
            if log_verbose and not debug_on:
                logger.info(
                    "【%s】新規会員（ミテネ履歴に「送信済」なし）: %d / %d 人",
                    step.tab,
                    len(filtered),
                    len(enriched),
                )
            if not filtered and enriched and log_verbose and not debug_on:
                logger.info("【%s】未送信会員 0 人", step.tab)
        elif mode == "sent_oldest_first":
            pool = [
                m
                for m in enriched
                if m.has_send_button
                and m.sent_history
                and member_queue_key(m.member_id)
                not in self._sent_member_keys
                and member_queue_key(m.member_id)
                not in self._failed_member_keys
            ]
            filtered = self._sort_members_oldest_first(pool)
            if log_verbose and not debug_on:
                logger.info(
                    "【%s】全会員（古い順・同日ランダム）: %d / %d 人",
                    step.tab,
                    len(filtered),
                    len(enriched),
                )
        else:
            filtered = [
                m
                for m in enriched
                if m.has_send_button
                and member_queue_key(m.member_id)
                not in self._sent_member_keys
                and member_queue_key(m.member_id)
                not in self._failed_member_keys
            ]

        self._log_pipeline_funnel_stage(step.tab, "Filter後", len(filtered))

        if debug_on:
            self._log_filter_exclusions_immediate(
                enriched, filtered, duplicates, step
            )
            if mode == "sent_oldest_first":
                self._log_oldest_first_sort_debug(step.tab, filtered)
            self._log_member_extraction_debug(
                step,
                members,
                enriched,
                filtered,
                duplicates=duplicates,
            )
        elif log_verbose:
            sent_n = sum(1 for m in enriched if m.sent_history)
            unsent_n = sum(
                1 for m in enriched if m.has_send_button and not m.sent_history
            )
            logger.info("【%s】送信履歴判定（Member %d件）", step.tab, len(enriched))
            logger.info("送信済: %d", sent_n)
            logger.info("未送信: %d", unsent_n)
            logger.info("Filter後: %d", len(filtered))
        return filtered

    def _count_new_members_on_page(self, page: Page) -> int:
        """未送信会員数（一覧DOM・送信ボタンあり）."""
        step = self._current_step
        if not step:
            return 0
        new_step = PriorityStep(
            tab=step.tab,
            member_filter="new_only",
            list_path=step.list_path,
        )
        if self._is_member_profile_page(page) and self._is_step_profile_page(
            page, step
        ):
            cards = self._parse_profile_tab_members(page, step)
            members, histories = self._raw_cards_to_collected(cards)
        elif self._is_on_comeon_list_page(page) and not self._is_member_profile_page(
            page
        ):
            tab = self._tab_name_from_page(page)
            members, histories = self.collect_members(page, tab, quiet=True)
        else:
            scanned = self._scan_member_cards(page)
            return sum(
                1
                for m in scanned
                if isinstance(m, dict) and not m.get("sent_history")
            )
        filtered = self._apply_step_member_filter(
            members, histories, new_step, quiet=True
        )
        return len(filtered)

    def _scan_unsent_member_keys(self, page: Page) -> list[str]:
        """一覧を走査し、現在ステップ条件に合う会員IDリストを返す."""
        scanned = self._scan_member_cards(page)
        keys = [
            str(m["key"])
            for m in scanned
            if isinstance(m, dict) and m.get("key")
        ]
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

    def _check_job_control(self) -> None:
        from job_runner import get_current_job_id, wait_if_paused

        wait_if_paused(get_current_job_id())

    def _pause_ms(self, ms: int) -> None:
        from job_runner import interruptible_sleep

        interruptible_sleep(max(0, ms) / 1000.0)

    def _send_mitene_standard(self, page: Page) -> int:
        if self.dry_run:
            logger.info("ドライラン: ログイン・残り回数の確認のみ（送信しません）")
            self._ensure_deco_home(page)
            remaining = self._parse_remaining_count(page)
            logger.info(
                "ドライラン: 残り回数 %s",
                remaining if remaining is not None else "取得できず",
            )
            return 0

        logger.info("②ホームでミテネ残り回数を取得")
        steps = self.standard.priority_steps or list(DEFAULT_PRIORITY_STEPS)
        skipped_steps: list[str] = []

        budget = self._read_send_budget(page)
        self._sent_member_keys.clear()
        self._failed_member_keys.clear()
        self._send_button_queue.clear()
        self._reset_debug_run_tracking()
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
            self._emit_send_progress(0, budget)
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
                    if budget > 0:
                        logger.warning(
                            "一覧画面の残り回数0を無視（ホームで %d 回取得済み）",
                            budget,
                        )
                    else:
                        raise DailyLimitReached("ミテネ残り回数が 0 です。")
                else:
                    target = min(target, list_remaining)
            self._send_target = target
            self._emit_send_progress(0, target)
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
            self._begin_send_phase_tracking(
                "レガシー送信", list(self._send_button_queue)
            )
            scroll_rounds = 0
            stall = 0
            failed_attempts = 0
            max_failed = min(target + 15, 35)
            while sent < target:
                self._check_job_control()
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
                    self._record_send_attempt_from_last()
                    failed_attempts += 1
                    stall += 1
                    if stall >= 5 and scroll_rounds <= self.standard.max_scroll_rounds:
                        scroll_rounds += 1
                        self._recover_more_send_buttons(page)
                        stall = 0
                    continue
                self._record_send_attempt_from_last()
                # キューが空になったら軽く補充（全62件ログは出さない）
                if len(self._send_button_queue) < 3:
                    self._refresh_send_button_queue(page, log_scan=False)
                failed_attempts = 0
                stall = 0
                scroll_rounds = 0
                sent += 1
                self._send_done = sent
                self._emit_send_progress(sent, target)
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
            self._finish_send_phase_tracking()

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
                self._log_run_send_reconciliation()
                self._log_debug_run_summary()
                return sent
            note = (
                f"目標 {budget} 回のうち {sent} 回しか送れませんでした。"
                "送れる会員が足りない・本日すでに送済み・サイト側で拒否された可能性があります。"
            )
            if self._failed_member_keys:
                note += f"（送信できなかった会員: {len(self._failed_member_keys)} 人）"
            if self.standard.must_use_full_budget:
                self._log_run_send_reconciliation()
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
        self._log_run_send_reconciliation()
        self._log_debug_run_summary()
        return sent

    _POPUP_DISMISS_LABELS: tuple[str, ...] = (
        "閉じる",
        "×",
        "キャンセル",
        "後で",
        "OK",
        "了解",
        "確認",
        "とじる",
    )

    def _is_popup_button_locator(self, locator: Locator) -> bool:
        """button / input[type=button|submit] のみ True."""
        try:
            return bool(
                locator.evaluate(
                    """el => {
                        const tag = el.tagName.toLowerCase();
                        if (tag === 'button') return true;
                        if (tag === 'input') {
                            const t = (el.getAttribute('type') || '').toLowerCase();
                            return t === 'button' || t === 'submit';
                        }
                        return false;
                    }"""
                )
            )
        except Exception:
            return False

    def _popup_button_locators(self, page: Page, label: str) -> list[Locator]:
        """ポップアップ用ボタンのみ列挙（曖昧な get_by_text は使わない）."""
        found: list[Locator] = []
        seen: set[int] = set()

        def _add(loc: Locator) -> None:
            n = self._safe_count(loc)
            for i in range(n):
                item = loc.nth(i)
                key = id(item)
                if key in seen:
                    continue
                seen.add(key)
                found.append(item)

        _add(page.get_by_role("button", name=label, exact=True))
        _add(page.locator(f'button:text-is("{label}")'))
        _add(page.locator(f'input[type="button"][value="{label}"]'))
        _add(page.locator(f'input[type="submit"][value="{label}"]'))
        return found

    def _recover_list_after_popup_misclick(self, page: Page) -> bool:
        """ポップアップ操作でプロフィールへ飛んだ場合、一覧URLへ戻す."""
        if not self._is_member_profile_page(page):
            return False
        logger.warning(
            "ポップアップクリック失敗: プロフィールへ遷移したため一覧へ戻ります: %s",
            page.url or "",
        )
        step = self._current_step
        if step:
            list_url = self._list_url_for_step(page, step)
            if list_url and self._safe_goto(page, list_url):
                self._wait_page_settled(page, quick=True)
                logger.info("一覧へ復帰: %s", page.url or "")
                return True
        if self._current_list_path:
            list_url = self._list_url(page, self._current_list_path)
            if list_url and self._safe_goto(page, list_url):
                self._wait_page_settled(page, quick=True)
                logger.info("一覧へ復帰: %s", page.url or "")
                return True
        return False

    def _dismiss_optional_popups(self, page: Page) -> None:
        """モーダル／ポップアップの閉じるボタンのみクリック（会員カード等は対象外）."""
        for label in self._POPUP_DISMISS_LABELS:
            for el in self._popup_button_locators(page, label):
                if not self._safe_is_visible(el):
                    continue
                try:
                    meta = el.evaluate(
                        """el => ({
                            tagName: el.tagName,
                            className: el.className || '',
                            outerHTML: (el.outerHTML || '').slice(0, 300),
                        })"""
                    )
                except Exception:
                    meta = {}
                logger.warning(
                    "POPUP_BUTTON_CANDIDATE label=%r url=%s meta=%s",
                    label,
                    page.url or "",
                    meta,
                )
                if not self._is_popup_button_locator(el):
                    logger.warning(
                        "POPUP_BUTTON_SKIP label=%r: button以外のためクリックしない tag=%s",
                        label,
                        (meta or {}).get("tagName", "?"),
                    )
                    continue
                url_before = page.url or ""
                try:
                    self._set_nav_debug_action(
                        f"_dismiss_optional_popups:click:{label}"
                    )
                    el.click(timeout=2000)
                    self._pause_ms(300)
                except Exception as exc:
                    logger.warning(
                        "POPUP_BUTTON_CLICK_FAIL label=%r: %s", label, exc
                    )
                    continue
                url_after = page.url or ""
                logger.warning(
                    "POPUP_BUTTON_CLICK_DONE label=%r url_before=%s url_after=%s",
                    label,
                    url_before,
                    url_after,
                )
                if self._is_member_profile_page(page):
                    self._recover_list_after_popup_misclick(page)
                break

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

    def _mitene_send_button_locator(self, page: Page) -> Locator:
        """姫デコ CTA: ♡ミテネを送る / registComeon."""
        return page.locator(
            ".kitene_send_btn a, "
            "a.kitene_send_btn__text_wrapper, "
            'a[onclick*="registComeon"], '
            'a:has-text("ミテネを送る"), '
            'button:has-text("ミテネを送る")'
        )

    def _scroll_member_list_to_end(self, page: Page, *, max_rounds: int = 150) -> int:
        """一覧を最下部までスクロールし、Ajax 遅延読込分も DOM に載せる."""
        surface = self._member_card_surface(page)
        prev = {"scroll_height": -1, "card_count": -1}
        stable = 0
        final_count = 0
        for _ in range(max_rounds):
            self._check_job_control()
            try:
                self._scroll_list_to_bottom(page)
                self._ajax_list_load_wait()
                scroll_height = self._get_list_scroll_height(page)
                final_count = self._count_member_cards_on_surface(surface)
                if (
                    scroll_height == prev["scroll_height"]
                    and final_count == prev["card_count"]
                ):
                    stable += 1
                    if stable >= LIST_SCROLL_STABLE_ROUNDS:
                        break
                else:
                    stable = 0
                prev = {
                    "scroll_height": scroll_height,
                    "card_count": final_count,
                }
            except Exception:
                break
        logger.info("一覧を最下部までスクロール完了（会員カード %d 件）", final_count)
        self._log_scroll_metrics_debug(page)
        return final_count

    def _scroll_member_list(self, page: Page) -> None:
        for _ in range(2):
            self._check_job_control()
            try:
                page.evaluate("window.scrollBy(0, window.innerHeight * 0.55)")
                self._pause_ms(350)
            except Exception:
                break

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
            self._check_job_control()
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
            self._pause_ms(poll_ms)
        return False

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
            member_id = self._member_id_from_queue_key(key)
            self._set_send_attempt_outcome(
                member_id, "スキップ", "既に送信済または失敗済"
            )
            return False
        if not key.startswith("comeon-"):
            self._set_send_attempt_outcome(key, "スキップ", "不正なキューキー")
            return False
        member_id = key[7:]
        self._last_send_attempt = None
        debug_send = self._member_extraction_debug_enabled()
        send_name = self._debug_member_names.get(member_id, "（名前不明）")
        cta_ok = False
        modal_shown = False
        ok_clicked = False
        remaining_before: int | None = None
        remaining_after: int | None = None
        if debug_send:
            self._log_per_send_debug_header(member_id, send_name)
        try:
            step = self._current_step
            if self._is_member_profile_page(page):
                if step and self._is_step_profile_page(page, step):
                    if not self._navigate_to_profile_member(
                        page, member_id, step
                    ):
                        logger.warning(
                            "プロフィール型一覧で会員 %s を表示できません",
                            key,
                        )
                        if debug_send:
                            self._log_per_send_debug_failure(
                                cta_ok=cta_ok,
                                modal_shown=modal_shown,
                                ok_clicked=ok_clicked,
                                remaining_before=remaining_before,
                                remaining_after=remaining_after,
                                reason="プロフィール表示失敗",
                            )
                        self._set_send_attempt_outcome(
                            member_id, "失敗", "プロフィール表示失敗"
                        )
                        return False
                else:
                    logger.warning(
                        "送信前にプロフィール検出 — 一覧へ戻してから再試行: %s",
                        page.url,
                    )
                    if not step or not self._navigate_to_url_safe(
                        page, step, force_reload=True
                    ):
                        if debug_send:
                            self._log_per_send_debug_failure(
                                cta_ok=cta_ok,
                                modal_shown=modal_shown,
                                ok_clicked=ok_clicked,
                                remaining_before=remaining_before,
                                remaining_after=remaining_after,
                                reason="一覧へ戻れず",
                            )
                        self._set_send_attempt_outcome(
                            member_id, "失敗", "一覧へ戻れず"
                        )
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
                self._pause_ms(500)
                self._dismiss_optional_popups(page)
                if not self._tap_mitene_cta(page, member_id):
                    if debug_send:
                        self._log_per_send_debug_failure(
                            cta_ok=False,
                            modal_shown=modal_shown,
                            ok_clicked=ok_clicked,
                            remaining_before=remaining_before,
                            remaining_after=remaining_after,
                            reason="CTAクリック失敗",
                        )
                    btn_after = self._kitene_button_locator(page, member_id)
                    cta_reason = (
                        "ボタン消失"
                        if self._safe_count(btn_after) == 0
                        else "CTAクリック失敗"
                    )
                    self._set_send_attempt_outcome(member_id, "失敗", cta_reason)
                    return False
                cta_ok = True
            else:
                cta_ok = True
            if self.human.fast_send:
                self.human.pause(80, 150)
            else:
                self.human.action_pause()
            self._pause_ms(250 if self.human.fast_send else 600)
            modal_shown = self._wait_confirm_layer(page, timeout_ms=4000)
            confirm_wait = 350 if self.human.fast_send else 800
            for _ in range(4):
                if self._confirm_send_dialog(page):
                    ok_clicked = True
                self._pause_ms(confirm_wait)
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
                    if debug_send:
                        self._log_per_send_debug_success(
                            cta_ok=cta_ok,
                            modal_shown=modal_shown,
                            ok_clicked=ok_clicked,
                            remaining_before=remaining_before,
                            remaining_after=remaining_after,
                        )
                    self._set_send_attempt_outcome(member_id, "成功")
                    self._mark_member_sent(key)
                    self._ensure_member_list_page(page)
                    return True
                if self._wait_kitene_send_result(page, member_id, timeout_ms=1200):
                    if debug_send:
                        if remaining_after is None:
                            remaining_after = self._parse_remaining_count(page)
                        self._log_per_send_debug_success(
                            cta_ok=cta_ok,
                            modal_shown=modal_shown,
                            ok_clicked=ok_clicked,
                            remaining_before=remaining_before,
                            remaining_after=remaining_after,
                        )
                    self._set_send_attempt_outcome(member_id, "成功")
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
                if debug_send:
                    self._log_per_send_debug_success(
                        cta_ok=cta_ok,
                        modal_shown=modal_shown,
                        ok_clicked=ok_clicked,
                        remaining_before=remaining_before,
                        remaining_after=remaining_after,
                    )
                self._set_send_attempt_outcome(member_id, "成功")
                self._mark_member_sent(key)
                self._ensure_member_list_page(page)
                return True
            if self._wait_kitene_send_result(page, member_id, timeout_ms=5000):
                if debug_send:
                    if remaining_after is None:
                        remaining_after = self._parse_remaining_count(page)
                    self._log_per_send_debug_success(
                        cta_ok=cta_ok,
                        modal_shown=modal_shown,
                        ok_clicked=ok_clicked,
                        remaining_before=remaining_before,
                        remaining_after=remaining_after,
                    )
                self._set_send_attempt_outcome(member_id, "成功")
                self._mark_member_sent(key)
                self._ensure_member_list_page(page)
                return True
            self._register_failed_member_key(key)
            if len(self._failed_member_keys) <= 2:
                self._save_debug_screenshot(page, f"send-fail-{member_id}")
            fail_state = self._kitene_member_send_state(page, member_id)
            logger.info(
                "送信未完了 %s（状態: %s・残り %s→%s）",
                key,
                fail_state,
                remaining_before,
                remaining_after,
            )
            logger.info("送信できなかったため次へ (%s)", key)
            if debug_send:
                self._log_per_send_debug_failure(
                    cta_ok=cta_ok,
                    modal_shown=modal_shown,
                    ok_clicked=ok_clicked,
                    remaining_before=remaining_before,
                    remaining_after=remaining_after,
                    reason=f"残回数未減少（状態: {fail_state}）",
                )
            self._set_send_attempt_outcome(
                member_id, "失敗", f"残回数未減少（状態: {fail_state}）"
            )
        except Exception as e:
            if _is_destroyed_context_error(e):
                self._wait_page_settled(page, quick=True)
                self._ensure_member_list_page(page)
            logger.debug("タップ失敗: %s", e)
            self._register_failed_member_key(key)
            if debug_send:
                self._log_per_send_debug_failure(
                    cta_ok=cta_ok,
                    modal_shown=modal_shown,
                    ok_clicked=ok_clicked,
                    remaining_before=remaining_before,
                    remaining_after=remaining_after,
                    reason=f"例外: {e}",
                )
            self._set_send_attempt_outcome(member_id, "失敗", f"例外: {e}")
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
