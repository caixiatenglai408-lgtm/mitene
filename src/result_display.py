"""手動・自動送信の結果表示（名前一覧・エラー）."""

from __future__ import annotations

import re
from typing import Any

_NO_REMAINING_RE = re.compile(r"ミテネ残り回数.*0|残り回数.*0\s*です")


def is_no_remaining_result(r: dict[str, Any]) -> bool:
    if r.get("status") == "no_remaining":
        return True
    text = " ".join(
        str(r.get(k) or "")
        for k in ("message", "error")
    )
    return bool(_NO_REMAINING_RE.search(text))


def classify_result(r: dict[str, Any]) -> str:
    if r.get("skipped"):
        return "skipped"
    if r.get("status") == "zero_send":
        return "error"
    if is_no_remaining_result(r):
        return "no_remaining"
    if r.get("error") and not r.get("name"):
        return "error"
    if r.get("ok") is False or r.get("error"):
        return "error"
    if r.get("dry_run"):
        return "dry_run"
    if (r.get("sent") or 0) > 0:
        return "success"
    if r.get("ok"):
        return "error"
    return "error"


def _detail_for_completed(r: dict[str, Any], kind: str, *, dry_run: bool) -> str:
    if kind == "no_remaining":
        return "ミテネ残り回数なし"
    if kind == "dry_run" or dry_run:
        return "ドライラン（送信なし）"
    sent = r.get("sent") or 0
    return f"{sent} 件送信"


def _detail_for_error(r: dict[str, Any]) -> str:
    return (
        str(r.get("error") or r.get("message") or "送信できませんでした")
        .strip()
    )


def build_run_display(
    results: list[dict[str, Any]] | None, *, dry_run: bool = False
) -> dict[str, Any]:
    if not results:
        return {
            "summary": "対象の女の子がいません（登録・有効化を確認してください）",
            "completed": [],
            "errors": [],
            "has_issues": True,
        }

    completed: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for r in results:
        kind = classify_result(r)
        name = str(r.get("name") or "").strip() or "（名前なし）"

        if kind == "skipped":
            continue
        if kind in ("success", "no_remaining", "dry_run"):
            completed.append(
                {
                    "name": name,
                    "detail": _detail_for_completed(r, kind, dry_run=dry_run),
                    "kind": kind,
                }
            )
            continue
        errors.append({"name": name, "detail": _detail_for_error(r)})

    sent_total = sum(int(x.get("sent") or 0) for x in results)
    done_count = len(completed)
    total = len([x for x in results if not x.get("skipped")]) or len(results)
    if not total and results:
        total = len(results)

    if results and results[0].get("error") and not results[0].get("name"):
        errors.insert(
            0,
            {"name": "全体", "detail": str(results[0]["error"])},
        )
        summary = str(results[0]["error"])
    else:
        summary = f"完了: {done_count}/{max(total, done_count + len(errors))} 名"
        if not dry_run:
            summary += f"、合計 {sent_total} 件送信"
        elif completed:
            summary += "（ドライラン）"

    has_issues = bool(errors) or (
        not dry_run
        and any(
            classify_result(r) == "error"
            for r in results
            if not r.get("skipped")
        )
    )

    return {
        "summary": summary,
        "completed": completed,
        "errors": errors,
        "has_issues": has_issues,
    }
