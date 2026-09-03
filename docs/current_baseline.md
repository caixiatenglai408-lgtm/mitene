# 現行 baseline 仕様書（姫デコ ミテネ自動送信）

- **確定日**: 2026-07-12（Phase 1 cleanup 時点の作業ツリー）
- **位置づけ**: 今後 Claude Code / Cursor で改修する際の **第一参照資料**。
- **最重要ルール**: **README・古いコメント・過去の会話より、実行コードと本書を正とする。**
  本書と実コードが食い違ったら実コードが正（見つけたら本書を直す）。
- Phase 1 では「挙動を一切変えない整理」のみ実施。送信対象・巡回順・判定・待機時間・timeout・
  config.yaml の有効値は変更していない。

---

## Baseline Git 固定（Phase 1.1）

- **Rollback タグ**: `baseline-phase1`（annotated tag）
  - このタグが指すコミット = 「Phase 2 以降で問題が起きたら戻る地点」。
  - コミット本文は `baseline: lock current working system before phase 2`。
  - 正確なコミット hash はタグオブジェクトに記録されている：
    `git rev-list -n 1 baseline-phase1` / `git show baseline-phase1`
    （hash をこの Markdown 自身へ完全に埋め込むと循環参照になるため、
    hash の正本はタグとし、本書はタグ名で参照する）
- **戻し方（Phase 2 以降で全面ロールバックが必要になったとき）**:
  - 現行システムのコード一式を丸ごと戻す（作業ツリーへ展開・コミットはしない）:
    `git checkout baseline-phase1 -- .`
  - 個別ファイルだけ戻す: `git checkout baseline-phase1 -- <path>`
  - **注意**: `data/accounts.json` / `data/settings.json` / `.env` / `logs/` / `playwright/.auth/` /
    `screenshots/` はこのタグに含まれない（`.gitignore` 済みのユーザーデータ）。ロールバックしても
    これらは現状のまま残る（＝消えない）。
- **このタグに含まれない実行時データ**（意図的に除外・Git 管理外）:
  `data/accounts.json`, `data/settings.json`, `.env`, `logs/**`（`member_sends.jsonl` /
  `sent_history.jsonl` 含む）, `playwright/.auth/**`（storage_state）, `screenshots/**`,
  `.venv/`, `playwright-browsers/`, `dist/`, `build/`, `__pycache__/`, `.pytest_cache/`。

---

## 0. リポジトリ状態（重要な前提）

- **Phase 1.1 以前の `git HEAD` は `0c98454 "Add redis dependency for Vercel"`（2026-06-16）で、
  現行システムを表していなかった。** 現在動いている 7 フェーズパイプライン・Web UI・スケジューラ等の
  大半は「未コミットの作業ツリー変更」だった。
- **Phase 1.1 で、その作業ツリー全体を `baseline-phase1` タグ付きコミットとして固定した。**
  以降の「baseline」= このタグのコミット内容 ＋ Git 管理外のユーザーデータ（上記）。
- `tests/` ディレクトリは**現存しない**。`.pytest_cache` と `mypy.ini` のコメントは、過去に存在した
  「Mixin 分割構成 + tests/」という**廃止済みアーキテクチャ**の名残（現行 `mitene_sender.py` は単一クラス）。
  → 現時点で実行可能なプロジェクトテストは無い。
- `tests/` ディレクトリは**現存しない**。`.pytest_cache` と `mypy.ini` のコメントは、過去に存在した
  「Mixin 分割構成 + tests/」という**廃止済みアーキテクチャ**の名残（現行 `mitene_sender.py` は単一クラス）。
  → 現時点で実行可能なプロジェクトテストは無い。

---

## 1. 起動経路（エントリーポイント）

### ローカルアプリ（通常運用）
- `起動.command` / `ブラウザで開く.command` → **`launch_app.py` `main()`**
  - `.venv/bin/python web/app.py` を**子プロセス**で起動（`start_server()`）。
  - `--browser`（`ブラウザで開く`）: `MITENE_AUTO_EXIT=1` を渡し、ブラウザで `http://127.0.0.1:5050` を開く。
    `wait_until_browser_tabs_closed()` がタブ全閉じ＋アイドルを検知して終了（送信中は待つ）。
  - 無印（`起動.command`）: `run_webview()`（pywebview 専用ウィンドウ）。
  - サブコマンド `--server-only` / `--scheduled` / `--install-browsers` は **PyInstaller 凍結 exe 専用**
    （`_bootstrap_from_argv()`）。venv 運用では使われない。

### Web UI 本体
- **`web/app.py` `main()`**: `start_scheduler()` → Mac/Win なら `sync_platform_schedule()` →
  `app.run(host=0.0.0.0, port=PORT, threaded=True)`。
- Vercel 入口は `api/index.py`（`from web.app import app`）。`main()` は呼ばれず `start_scheduler()` も動かない。
  **現行ローカル運用では未使用**（`VERCEL` 未設定）。

### 手動送信（画面ボタン → API → 実処理）
| 操作 | API | 実処理 |
|---|---|---|
| 今すぐ全員送信 / 出勤のみ / お休みのみ | `POST /api/run-now` `{group: all|working|off}` | `start_background_job(... run_manual_batch(group, dry_run=False))` |
| 全員ドライラン（API のみ・index にボタン無し） | `POST /api/run-dry-all` | `run_manual_batch(dry_run=True)` |
| 各行「今すぐ送信」 | `POST /api/run-account/<id>` | `start_background_job(... run_single_account(id, respect_enabled=False))` |
| 各行テスト（API のみ・露出無し） | `POST /api/run-test/<id>` | `run_for_account(dry_run=True)` を同期実行 |

`GET /api/status` は「後方互換」で中身は `/api/data` と同じ（JS からは呼ばれない）。

`_use_worker()`（`MITENE_WORKER_URL` 設定時）だと上記はすべて `worker_client.py` 経由で
外部ワーカーに委譲。**`.env` が無く未設定なので現行は必ずローカル実行。**

### スケジュール送信（2系統・両方有効、`last_run_slot` で二重実行を抑止）
1. **画面プロセス内**: `scheduler_service.start_scheduler()` → APScheduler cron `minute=*/1` → `tick()` →
   `run_all_scheduled()`。**画面起動中のみ。**
2. **OS 登録**: `sync_platform_schedule()` が Mac は `~/Library/LaunchAgents/com.local.mitene.sender.plist`、
   Win は タスク `MiteneAutoSend` を登録。発火時 `scripts/run_scheduled.py`（凍結時は
   `launch_app.py --scheduled`）→ `run_all_scheduled()`。**画面が閉じていても実行。**
   launchd の発火時刻は各スロットの `Hour`、`Minute=1`（`platform_schedule/common.py`）。

### CLI（開発検証用・通常運用では未使用）
- `src/main.py`: `.env` の `HIMEDECO_LOGIN_ID / PASSWORD / BASE_URL` で 1 人だけ実行。

---

## 2. UI → runner → MiteneSender（時系列）

```
[ボタン] index.html runBatch() / account_list.js
  → POST /api/run-now (web/app.py: api_run_now)
    → validate_before_run(group)                      # base_url / enabled / グループ対象の有無
    → start_background_job(name, fn, ...)              # job_runner.py: _jobs[job_id] 生成, Thread 起動, sleep_guard.acquire()
      └ run_manual_batch → run_send_job(group, dry_run, job_id)   # scheduler_service.py
          for account in accounts_for_manual_group(group):        # 対象決定（下記3）
            bind_send_progress(job_id, account.id, account.name)   # 進捗コールバック紐付け
            run_for_account(account, base_url, dry_run)            # runner.py
              → build_sender(...)                                 # config.yaml → MiteneSender
              → MiteneSender.run()                                # mitene_sender.py
                  _launch() / _new_context()                      # Chromium 起動（headless, iPhone UA）
                  _ensure_logged_in(page, context)                # J1Login.php, 最大3回
                  _send_mitene_standard(page)                     # ← 現行送信処理（flow == standard）
                    _read_send_budget(page)  → budget 確定・_locked_send_budget に固定
                    _execute_phased_send_pipeline(page, budget)   # ← 7 フェーズ（下記5）
                      各フェーズ: _fetch_tab_members → _apply_step_member_filter
                                  → _send_member_keys_phase → _send_loop_for_step → _send_one_mitene
                    self._last_run_report = {budget, sent, sent_by_step, skipped_steps, note}
                  return sent (int)
              → status 決定（success / zero_send / no_remaining / budget_read_failed / error / dry_run / skipped）
              → result dict
            report_job_progress(job_id, partial_results=results)   # partial_display を随時更新
      └ _finalize_background_job(job_id, results, ...)             # job_runner.py
          display = build_run_display(results)                     # result_display.py: completed / errors
          save_last_run_report("manual", results)                 # settings.last_run に保存（画面には非表示）
          _jobs[job_id] を status=done / completed=True / display= に更新, sleep_guard.release()
[ポーリング] run_poll.js watchJob() → GET /api/jobs/<id> を2秒毎
  running → onProgress / onStatus（進捗ラベル）
  それ以外 → onDone → run_ui.js finalizeJob → showJobResult → run_results.js renderRunDisplay
             #run-status にサマリ、#manual-run-result に「送信完了」「未完了・エラー」リスト
```

スケジュール経由は UI 層が無く `run_all_scheduled()` → `_run_all_scheduled_impl()` が直接
`run_for_account()` を回し、末尾で `save_last_run_report("scheduled", ...)`。

---

## 3. 送信対象の判定

### アカウント（女の子）レベル
- **手動 全員** `accounts_for_manual_group("all")` = `partition_enabled_by_attendance()` の
  「enabled かつ 本日出勤」→「enabled かつ お休み」の順（`store.py`）。
- **手動 working / off**: 出勤側だけ / お休み側だけ。
- **スケジュール**: `order_accounts_by_attendance([a for a in load_accounts() if a.enabled])`（出勤→お休み）。
  `human.shuffle_account_order=true` なので**各グループ内はシャッフル**（`scheduler_service.py`）。
- **単体** `/api/run-account`・`/api/run-test`: `respect_enabled=False`（無効フラグでも送る）。
  全員送信・スケジュールは enabled 必須。
- 「本日出勤」= `settings.working_today_ids`。`attendance_date` が変わると自動リセット。

### 会員（送信先）レベル ― 7 フェーズパイプライン（`_execute_phased_send_pipeline`）

`config.yaml` が `flow: standard` かつ `priority_steps` 7 件なので、実行されるのは
`_send_mitene_standard` の `if steps:` 分岐 = **`_execute_phased_send_pipeline`**。

| # | タブ | list_path | member_filter | 実行条件 |
|---|---|---|---|---|
| ① | マイガール | `/J10ComeonMyGirlList.php` | `new_only` | 常時 |
| ② | キープ | `/J10ComeonKeepList.php` | `new_only` | 常時 |
| ③ | マッチ率 | `/J10ComeonAiMatchingList.php` | `new_only` | 常時。新規有無を `_match_rate_had_new` に記録 |
| ④ | マイガール | 同上 | `sent_oldest_first` | 常時 |
| ⑤ | みたよ | `/J10ComeonVisitorList.php` | `sendable` | **③で新規マッチ率が 0 件のときのみ**（`if not new_matchings`） |
| ⑥ | キープ | 同上 | `sent_oldest_first` | 常時（`_send_oldest_first_phases`） |
| ⑦ | マッチ率 | 同上 | `sent_oldest_first` | 常時（同上） |

各フェーズ前後に `if sent >= budget: return sent`。

### `_apply_step_member_filter`（送信済み判定はここだけ）
- **必ず `has_send_button == True`**（カード内に有効な「ミテネを送る」。`.kitene_send_zumi_btn`＝送信済表示は除外）。
- `new_only`: `sent_history == False`。判定は一覧DOMの「ミテネ履歴」テキストに「送信済」を含まないこと
  （`is_new_member_from_history` / `_history_text_is_sent`）。
- `sent_oldest_first`: `sent_history == True` のみ → `_sort_members_oldest_first`
  （最終送信日 昇順・同日ランダム・日付不明は `date(1970,1,1)` 扱いで最優先）。
- `sendable`: `has_send_button` のみ。
- **共通除外**: `comeon-{id}` が今回の `_sent_member_keys`（送信済）／`_failed_member_keys`（失敗済）に
  入っている会員。→ **同一実行内で再送・失敗リトライしない。**
- 同一実行内で複数タブに現れた同一 ID は `collect_members` の `seen` セットで排除。

### 一覧取得・スクロール
- `_fetch_tab_members` → `_navigate_to_url_safe`（J10一覧URL直打ち・`force_reload=True`）または
  マイガールは `_open_mygirl_via_keep_tab`（キープ経由）。失敗時 `_navigate_to_step_list`（横タブ切替）。
- 到達後 `_scroll_member_list_to_end`（最大150ラウンド、`scrollHeight` とカード数が
  `LIST_SCROLL_STABLE_ROUNDS=4` 連続不変で停止）で Ajax 遅延読込分を DOM に載せ、
  `_scroll_parse_merge_list` で解析（`member_scroll_merge_parse` フラグに関係なく常にこの解析）。
- 送信ループ中にキューが尽きたら `_scroll_member_list` + `_refresh_send_button_queue` を
  `max_scroll_rounds`(=30) まで。3 連続で増えなければそのフェーズ終了。
- あるタブで対象 0 件 → 次フェーズへ。

### `member_cooldown_days` / `max_no_history_sends_per_day`
`_filter_member_queue` にロジックはあるが、**呼ばれるのは `priority_steps` が空のレガシー経路のみ**
（`_scan_unsent_member_keys` 内 `if not self.standard.priority_steps:`）。現行 config では**未評価**。
`config.yaml` の値も両方 `0`（制限なし）。

### 日本語まとめ（1 文）
> 「enabled で、当日出勤なら優先されるアカウント」でログインし、そのアカウントについて
> 「マイガール／キープ／マッチ率／（③で新規0件のときのみ）みたよ」の各一覧に表示される会員のうち、
> ①〜③は『ミテネ履歴に「送信済」が無く』『有効な「ミテネを送る」ボタンがあり』『今回まだ送っていない／
> 失敗していない』新規会員、④⑥⑦は同条件で『既に送信履歴がある会員を最終送信日の古い順』に、
> ミテネ残り回数を使い切るまで上から順に送る。

---

## 4. budget（送信件数の決め方）

- `sent` の上限 = **`budget`（= `_read_send_budget()` が返すミテネ残り回数）** だけ。
- `budget = _cap_send_budget(remaining)` = `min(remaining, max_send_per_run)`（`max_send_per_run>0` のとき）。
  現行 `max_send_per_run: 0` → **キャップ無し = 残り回数そのまま**。
- 一度読んだら `self._locked_send_budget` に固定。実行中に増減しても追随しない。
- `self._send_target = budget`。各ループは `while sent < budget`。
- `must_use_full_budget`（現行 **false**）: 送り切れなかったとき true なら `RuntimeError`（＝エラー）。
  false なので**送れた分だけで正常終了**。

### 残り回数の取得（`_collect_remaining_reads` → `_merge_remaining_reads`）
- ソースは最大 3 つ:
  - `cta`: 「ミテネできる会員を探す」要素から親を最大10段さかのぼり `innerText` に「ミテネ残り回数」を
    含むブロック → 正規表現。
  - `label`: 「ミテネ残り回数」要素（最大5個）の親4段 → 正規表現。
  - `body`（上2つが空のとき）: `page.inner_text("body")` → `page.content()`、**正の値のみ** max。
- 正規表現:
  - `MITENE_REMAINING_PATTERN = /ミテネ残り回数\s*[：:：]?\s*([0-9０-９]+)\s*回?/`（全角対応、複数マッチは max）。
  - CTA 付近のみ `REMAINING_SLASH_PATTERN = /残り回数\s*[：:]\s*([0-9０-９]+)\s*\/\s*([0-9０-９]+)/`（「20 / 20」）をフォールバック。
- マージ: どれか >0 → **正の値の最大**。全ソース 0 かつ `cta`/`label` を含む → `0`。読めない → `None`。

### 取得失敗・0・不正値（`_read_send_budget`）
| 状況 | 挙動 |
|---|---|
| `None`（読めない） | ホームに居なければ `RuntimeError("ミテネ残り回数取得失敗（ホーム画面を開けませんでした）")`、居れば `…（CTA付近の残り回数が読み取れませんでした）` |
| `< 0` | `RuntimeError("…（不正な残り回数: N）")` |
| `== 0` | `_retry_remaining_on_suspect_zero`（4回・待機＋スクロール＋再読）→ さらに「会員を探す」画面でも再確認。なお 0 で `cta/label/retry/list` のいずれかが確証ある 0（`_has_confident_zero_read`）→ `DailyLimitReached("ミテネ残り回数が 0 です。")`。確証なし → `RuntimeError("…（残り0と判定されましたがCTA付近で確認できませんでした）")` |
| 正常 | `budget = _cap_send_budget(remaining)`、`_locked_send_budget` に固定 |

`RuntimeError` の文言には接頭辞 `BUDGET_READ_FAILED_PREFIX = "ミテネ残り回数取得失敗"` が付く。

---

## 5. 1 件送信処理（`_send_one_mitene`）

1. キューから `key = _send_button_queue.pop(0)`（空なら `_scan_unsent_member_keys` で先頭1件を取り直す）。
2. `key` が `_sent_member_keys` / `_failed_member_keys` に含まれる、または `comeon-` で始まらない → **スキップ**
   （`return False`、送信カウント増えず）。
3. `member_id = key[7:]`。プロフィール型一覧なら `_navigate_to_profile_member` で該当会員へ。
4. `remaining_before = _parse_remaining_count(page)` を記録。
5. ボタン Locator（`_kitene_button_locator`: `.js-regist_comeon_{id} a`, `.u_{id} .kitene_send_btn a`,
   `a[onclick*="registComeon({id})"]`）。無ければ一覧を再読込して再取得。
6. **`_tap_mitene_cta`**: ① Playwright `element.click(timeout=12000)` → ② カード内 `a`/`button` を
   DOM `el.click()` → ③ `registComeon(Number(mid))` 直呼び。失敗時は `_dismiss_optional_popups` 後にもう一度。
   2回失敗 → `_set_send_attempt_outcome(member_id,"失敗",...)`、`return False`。
7. 待機（fast_send: `pause(80,150)` + `_pause_ms(250)`）。
8. `_wait_confirm_layer(4000)`（`#colorbox` 等が visible になるまで最大4秒）。
9. **確認ループ 最大4回**: `_confirm_send_dialog()` → `_pause_ms(350)` → `remaining_after` 再取得
   → 判定A/判定B（下記6）。
10. ループ後にもう一度 `remaining_after` 判定A → 判定B（`_wait_kitene_send_result(..., 5000)`）。
11. どれも不成立 → `_register_failed_member_key(key)`（失敗2件以下ならスクショ）、
    `_set_send_attempt_outcome(member_id,"失敗","残回数未減少（状態: ...）")`、`return False`。
12. 例外時 → コンテキスト破棄系エラーなら一覧に戻し、`_register_failed_member_key`、`return False`。

---

## 6. 送信成功判定 / 失敗判定 / 送信済み記録

### 「1 件成功」= 次のいずれか（OR・優先順）
1. **【最優先】ミテネ残り回数が減った**: `remaining_before` と `remaining_after` がともに数値で
   `remaining_after < remaining_before`。
2. **`_wait_kitene_send_result()` が True**: 対象会員の `.js-regist_comeon_{id}` ラッパについて、
   `innerText` に「送信済」を含む／内部の `.kitene_send_zumi_btn` が表示状態／`active` クラスが外れた。
   （`member_id` が無い場合のみ、ページ全体に「送信しました／送りました／送信完了」＝ `_mitene_send_succeeded`）
- `cta_ok` / `modal_shown` / `ok_clicked` は**成功条件ではなくデバッグログ用**。

### 失敗判定
- 確認ループ後まで判定A・Bのどちらも成立しない → 失敗。
- CTA クリック2回失敗、プロフィール表示失敗、一覧へ戻れず、例外 → 失敗。
- 失敗した `key` は `_failed_member_keys` に入り、**その実行中は二度と対象にしない**（リトライしない）。

### 成功時の更新（順序）
1. `_set_send_attempt_outcome(member_id, "成功")` → `self._last_send_attempt`。
2. **`_mark_member_sent(key)`**:
   - `self._sent_member_keys.add(key)`（メモリ set）
   - `_invalidate_list_cache()`
   - 非 dry_run のみ `_record_member_sent(key)` → `logs/{account_id}/member_sends.jsonl` に1行追記
     （`{member_key, date, time}`）、`self._member_last_sent[key]=today`、新規なら `_no_history_sent_today += 1`。
3. `_ensure_member_list_page(page)` → 一覧へ戻す。
4. `return True` → `_send_loop_for_step` が `sent += 1; step_sent += 1; sent_by_step[label] += 1;
   self._send_done = sent; _emit_send_progress(sent, budget)`。
5. `_send_loop_for_step` は毎試行 `_record_send_attempt_from_last()` を呼び `SendPhaseRecord`
   （`success` / `failed` / `skipped` / `attempted`）へ反映（照合ログ用）。

### 実行全体の記録
- `_send_mitene_standard` の戻り `sent > 0` かつ非 dry_run → `_record_sent(count=sent, flow="standard")`
  → `logs/{account_id}/sent_history.jsonl` に1行（`{date, time, status:"sent", flow, count}`）。
- `_send_mitene_standard` の各 `return` 直前に **`self._last_run_report = {budget, sent, sent_by_step,
  skipped_steps, note}`** を必ずセット。

### 書き込みタイミングまとめ
| 対象 | いつ |
|---|---|
| `_sent_member_keys`（メモリ） | 1件成功の直後（`_mark_member_sent`） |
| `logs/{id}/member_sends.jsonl` | 1件成功の直後（非dry_run） |
| `_send_done` / 進捗 | 成功で `sent` を増やした直後 |
| `logs/{id}/sent_history.jsonl` | そのアカウントの送信フェーズ全終了後に1回（`sent>0` 時のみ） |
| `_last_run_report` | `_send_mitene_standard` の各 return 直前 |
| `settings.last_run` | `_finalize_background_job` / `run_all_scheduled` / `run_send_job` 末尾（**画面には非表示**） |

---

## 7. result 構造 と _sent_member_keys による成功補正

### `run_for_account()` が返す `status`
| status | 条件 | `ok` |
|---|---|---|
| `skipped` | `respect_enabled=True` かつ `account.enabled=False`（`skipped="disabled"`） | ― |
| `dry_run` | `dry_run=True` で正常終了 | `True` |
| `success` | 非dry_run で `run()` が `sent > 0` | `True` |
| `zero_send` | 非dry_run で `sent == 0`（例外なし）。`message = sender.zero_send_message()` | `True`（※表示上はエラー扱い） |
| `no_remaining` | `DailyLimitReached` 捕捉 | `True`、`message`=例外文 |
| `budget_read_failed` | `RuntimeError` で文言に「ミテネ残り回数取得失敗」を含む | `False`、`error`/`message`=文言 |
| `error` | `base_url` 未設定 / その他想定外例外 | `False`（※部分送信ありなら success へ昇格） |

result に現れうるフィールド: `account_id, name, ok, sent, status, dry_run, error, message, report, skipped`。
`report` = `sender._last_run_report`（`budget, sent, sent_by_step, skipped_steps, note`）。

### 成功補正（**Phase 1 で維持・変更禁止**）
- **`runner.resolve_sent_this_run(sender)`**（`src/runner.py`）:
  `sender._last_run_report["sent"]` が >0 ならそれを、0 なら `len(sender._sent_member_keys)` を返す。
- 呼び出し2箇所:
  1. `DailyLimitReached` 捕捉時（`run_for_account`）— `sent` に実績を計上。
  2. **`_account_error_result()`**（想定外例外時）— `resolve_sent_this_run(sender) > 0` なら
     **`result["sent"]=N, ok=True, status="success", message=str(error)`** に昇格し、
     `report` も添付する。
- 目的: 「今回実際に `_mark_member_sent` された件数 > 0 なら、UI で完了・送信件数を表示する」現行挙動を守る。
- **現行コードに存在しないシンボル**（過去案 / 別実装の名残・混同注意）:
  `sent_member_keys_count`, `_recover_result_from_sent_keys`, `result_recovery`,
  `original_status`, `recovered_from_error`。→ Phase 1 で新設もしない。

---

## 8. UI の completed / errors 判定

### Python（`result_display.py`）
`classify_result(r)`:
1. `r.skipped` → `"skipped"`（表示除外）
2. `status == "zero_send"` → `"error"`
3. `r.dry_run` → `"dry_run"`
4. `(r.sent or 0) > 0` → `"success"`
5. `status == "budget_read_failed"` → `"budget_read_failed"`（errors 側）
6. `status == "no_remaining"` → `"no_remaining"`（completed 側）
7. `error` があり `name` 無し → `"error"`
8. `ok is False` または `error` あり → `"error"`
9. `ok` truthy → `"error"`（保険）
10. それ以外 → `"error"`

`build_run_display`:
- `kind in ("success","no_remaining","dry_run")` → **completed** `{name, detail, kind}`
  （detail: dry_run→「ドライラン（送信なし）」／`sent>0`→「N 件送信」／`no_remaining`→「ミテネ残り回数なし」）。
- それ以外 → **errors** `{name, detail = error or message or "送信できませんでした"}`。
- `results[0]` が `name` 無し `error` あり → errors 先頭に `{"全体", error}`、`summary = その error`。
- 通常 summary = `"完了: {completed数}/{max(total, completed+errors)} 名"`、非dry_run なら
  `"、合計 {全 r の sent 合計} 件送信"` を追記。
- `has_issues = errors があれば True、または（非dry_run かつ classify=="error" の結果が1つでもある）`。

### JavaScript
- `run_poll.js watchJob()`: `GET /api/jobs/<id>` を2秒毎。`job.status === "running"` → `onProgress`＋
  `onStatus(running:true)`。それ以外 → `onDone`。通信エラー時は `/api/client-heartbeat/status` の
  `busy` を見て、busy か一時的エラー正規表現なら**エラーにせず「（通信復帰待ち）」で継続**。
- `run_ui.js isTerminalJob()`: `job.completed === true` または `status in ("done","error")`。
- `run_ui.js showJobResult()`: `display = job.display || (results から生成) || job.partial_display ||
  (error/message から errors 1件)`。`errorsOnly = errors>0 && !completed`。
  - `#run-status` が「完了：…」になるのは **terminal かつ completed が1件以上**。completed が空
    （全員エラー/zero_send）なら「エラー：…」表示。
- `run_results.js renderRunDisplay()`: `completed`>0 → 見出し「送信完了」、`errors`>0 → 見出し「未完了・エラー」。
- `run_progress.js`: running 中、`job.progress.account_id` の行に「（done/budget）」or「送信中…」、
  `partial_results`/`completed_account_ids` の行に「完了」バッジ。

### 表示されないもの
- **`settings.last_run`（保存済みの前回結果）は index 画面に一切描画されない。**
  埋めるのは「今このブラウザで開始したジョブ」の `watchJob` 経由のみ。リロードで前回結果は消える。

---

## 9. スケジュール実行条件

- `run_all_scheduled()` → `_run_all_scheduled_impl()`（`require_automation=True` 既定）:
  1. `settings.automation_enabled` が False → `[{"error": "自動送信がOFFです"}]`。
  2. `is_scheduled_now(settings, now)`: `automation_enabled` かつ `current_slot_key(now)` が非 None
     （`hour==9 & minute<30` → `"09:00"` ／ `hour==0 & minute<30` → `"00:00"` ／ `hour==19 & minute<30` → `"19:00"`）
     かつ `settings.schedule[weekday][slot]` が True。False → `[]`。
  3. `settings.last_run_slot == run_slot_id(now)`（`"{date}_{slot}"`）→ 既に実行済みとして `[]`。
  4. `base_url` 未設定 → `[{"error": "base_url未設定"}]`。
  5. 対象 = `order_accounts_by_attendance([enabled])`、`shuffle_account_order` でグループ内シャッフル。
  6. 各アカウントを `run_for_account(dry_run=False)`。1人例外 → `{status:"error"}` を積んで続行。
     `JobCancelled` で break。
  7. 非 force・非 dry_run・automation ON なら `settings.last_run_slot = run_slot_id` を保存。
  8. 結果があれば `save_last_run_report("scheduled", results)`。
- 画面内 APScheduler（毎分 `tick`）と OS 登録（launchd/タスク）が**両方**この関数を呼ぶ。
  重複防止は `settings.last_run_slot`（ファイル経由）＋ `_run_lock`（同一プロセス内のみ）。
- 出勤/お休みの優先順位は手動と同じ（出勤→お休み、グループ内シャッフル）。

---

## 10. 現在の待機時間（**Phase 1 で変更禁止**）

`config.yaml` の `human` が有効（`enabled: true, fast_send: true`）。`pause(min,max)` は
`random.randint(min,max)` を `interruptible_sleep`（一時停止/中止を 0.25 秒刻みで反映）で待つ。

### 固定で必ず待つ時間
| 箇所 | 時間 |
|---|---|
| ログイン画面を開く前 | 2.0 秒（`LOGIN_PRE_OPEN_WAIT_SEC=(2,2)`） |
| ログイン送信後 | 2.0 秒（`LOGIN_POST_SUBMIT_WAIT_SEC`）＋ `after_login_pause`（`after_login_delay_ms:[0,0]`=0） |
| ログイン失敗→再試行前 | 30 秒（`LOGIN_RETRY_WAIT_MS`）。SSL/通信ブロック検知時は 60 秒（`LOGIN_ACCESS_BLOCK_WAIT_MS`） |
| 会員間（1件送るごと） | **0.3〜0.5 秒**（`between_members_ms:[300,500]`） |
| 送信直後 | **30〜80ms**（fast_send 固定分岐。非fastは `after_send_delay_ms`） |
| CTA タップ後 | `pause(80,150)` ＋ `_pause_ms(250)`（fast_send 分岐） |
| 確認ループ1周 | `_pause_ms(350)`（fast_send。非fastは 800）× 最大4周 |
| アカウント間 | **3〜5 秒**（`between_accounts_ms:[3000,5000]`、`i>0` かつ非dry_run） |
| 一覧 Ajax 読込 | **2〜3 秒**（`LIST_AJAX_LOAD_WAIT_MS`）× スクロール各ラウンド |
| 一覧スクロール安定判定 | `scrollHeight`＋カード数が 4 連続不変（`LIST_SCROLL_STABLE_ROUNDS`）まで。各タブ最低 ~8〜12 秒 |
| 残り0の再確認 | `600 + n*350` ms × 4 回、各回スクロール2回で +350ms |

### 最大 timeout だが条件成立で即終了（＝固定 sleep ではない・短縮しない）
| 箇所 | timeout | 即終了条件 |
|---|---|---|
| `_wait_confirm_layer` | 4000ms | `#colorbox` 等が visible |
| `_wait_kitene_send_result` | 1200ms（ループ内）/ 5000ms（最終） | 対象ボタンが「送信済/zumi表示/active外れ」 |
| `_wait_for_send_buttons` | 20000ms / 15000ms | 「ミテネを送る」出現 |
| `_poll_wait_for_list_render` | ~8000ms（`LIST_RENDER_WAIT_MAX_MS`） | 会員カード or ローディング消滅 |
| Playwright 既定 | `browser.timeout_ms = 45000` | 要素操作成功 |
| `_tap_mitene_cta` の click | 12000ms | クリック成功 |
| `_ensure_deco_home` 各セレクタ | 12000ms | ホーム要素 visible |

---

## 11. 現在有効な設定（要点）

### `config.yaml`（`build_sender` が読む）— Phase 1 で変更禁止
| 設定 | 現在値 | 役割 |
|---|---|---|
| `flow` | `standard` | `_send_mitene_standard` 経路 |
| `mitene.find_members_button` | `ミテネできる会員を探す` | CTA / 残回数近傍検出 |
| `mitene.remaining_label` | `ミテネ残り回数` | 残回数ラベル検出 |
| `mitene.mitene_history_label` | `ミテネ履歴` | 新規/送信済判定 |
| `mitene.max_send_per_run` | `0` | budget キャップ無し |
| `mitene.must_use_full_budget` | `false` | 送れた分で正常終了 |
| `mitene.max_scroll_rounds` | `30` | キュー補充スクロール上限 |
| `mitene.member_cooldown_days` | `0` | 現行パイプライン経路では未評価 |
| `mitene.max_no_history_sends_per_day` | `0` | 同上 |
| `mitene.priority_steps` | 7件 | **7フェーズ経路の有効化スイッチ**（順序は `_execute_phased_send_pipeline` が優先） |
| `mitene.confirm_buttons` | `[ミテネを送る, 送る, OK]` | 確認ボタン一致 |
| `mitene.skip_special_banners` | `true` | ポップアップ閉じ |
| `mitene.member_extraction_debug` | `false` | デバッグログ量のみ（動作不変） |
| `mitene.member_scroll_merge_parse` | `false` | 照合不一致時のヒント行にのみ影響（解析経路は不変） |
| `human.enabled` | `true` | 待機ロジック有効 |
| `human.fast_send` | `true` | クリック周りの待機短縮分岐 |
| `human.action_delay_ms` | `[80,150]` | 各クリック前待ち |
| `human.between_members_ms` | `[300,500]` | 会員間隔 |
| `human.between_accounts_ms` | `[3000,5000]` | アカウント間隔 |
| `human.tab_switch_delay_ms` | `[400,800]` | タブ切替待ち |
| `human.after_login_delay_ms` | `[0,0]` | 0秒 |
| `human.after_send_delay_ms` | `[40,80]` | fast_send=true の間は未使用（30-80固定） |
| `human.scroll_probability` | `0` | ランダムスクロール無効 |
| `human.shuffle_member_order` | `false` | `sendable` でも順序維持 |
| `human.shuffle_account_order` | `true` | スケジュール時のグループ内シャッフル |
| `human.typing_delay_ms` / `idle_pause_probability` / `idle_pause_ms` / `shuffle_tab_order` | 各値 | `HumanBehavior` に読み込むが**現行コードで参照なし**（config と属性の対応維持のため残置） |
| `login.id_placeholder` / `password_placeholder` / `submit_text` | 各値 | ログインフォーム検出 |
| `browser.headless` | `true` | `--headed` CLI で上書き可 |
| `browser.slow_mo_ms` | `0` | Playwright 操作間隔 |
| `browser.timeout_ms` | `45000` | 全操作の既定 timeout |
| `browser.viewport` | `390×844 / is_mobile:true` | モバイル表示・iPhone UA |
| `logging.screenshot_on_error` | `true` | エラー時 `logs/{id}/error_*.png` |
| `mitene_gift.*` | 各値 | `flow: gift` 用（現行未使用） |

### `data/settings.json`
| キー | 現在値 | 役割 |
|---|---|---|
| `automation_enabled` | `true` | スケジュールゲート・OS登録要否 |
| `base_url` | `https://spgirl.cityheaven.net/J1Login.php` | `_canonical_login_url` |
| `schedule` | 全曜日 `09:00:true` 他 false | `is_scheduled_now`・OS trigger |
| `last_run_slot` | `2026-06-04_19:00` | スロット重複実行防止 |
| `sleep_schedule_enabled` | `true` | OS 定時登録の有効化 |
| `attendance_date` / `working_today_ids` | 出勤4名 | 出勤優先。日付跨ぎで自動リセット |
| `last_run` | 前回 manual 結果 dict | 保存のみ（画面非表示） |

### 環境変数（実挙動に効くもの）
`PORT` / `MITENE_AUTO_EXIT` / `MITENE_BROWSER_APP` / `MITENE_SECRET_KEY`（パスワード暗号化・未設定なら平文）
/ `FLASK_SECRET_KEY` / `MITENE_WORKER_URL`・`MITENE_WORKER_SECRET`（別環境）/ `VERCEL` /
`REDIS_URL` ほか（別環境）/ `PLAYWRIGHT_BROWSERS_PATH`（`setup_runtime` が固定）/
`PLAYWRIGHT_BROWSER_WS_ENDPOINT`・`BROWSERLESS_TOKEN`（リモート Chrome）/
`MITENE_MEMBER_EXTRACTION_DEBUG`・`MITENE_SCROLL_MERGE_PARSE`（デバッグ）/
`HIMEDECO_*`（`src/main.py` CLI 専用）。
**現行ローカル運用では `.env` ファイルが存在せず、`MITENE_WORKER_URL` / `VERCEL` は未設定。**

---

## 12. 現行ローカルで未使用の「別環境／別構成」コード（削除しない）

| 対象 | 位置づけ |
|---|---|
| `worker/`（`worker/app.py` ほか） | Railway 等にデプロイする送信ワーカー。`MITENE_WORKER_URL` 設定時のみ使用 |
| `src/worker_client.py` | Vercel 管理画面 → 外部ワーカーへの委譲クライアント |
| `api/index.py` | Vercel serverless の Flask 入口 |
| `src/data_store.py` の Redis 分岐 | `VERCEL` 環境での永続化（`storage_mode()=="kv"`） |
| `flow: "gift"` 一式（`_send_mitene_gift`, `MiteneGiftConfig`, `mitene_gift:`） | 現行 `flow: standard` のため不使用 |
| `src/main.py` | CLI 単体実行（開発検証用） |
| `_send_mitene_standard` の `else`（レガシー単一路線） | `priority_steps` を手で空にしたときのみ通る |
| `src/platform_schedule/windows.py` | Windows 実行時のみ |
| PyInstaller 関連（`mitene_autosend.spec`, `scripts/build_windows_exe.ps1`, `dist/`, `build/`） | exe ビルド・配布用 |

これらは「今使っていない」だけで「別環境で必要」。**Phase 1 では一切削除しない。**

---

## 13. Phase 1 で確認した「整理候補」と現状の判定（削除保留）

| 候補 | 再確認結果 | Phase 1 判定 |
|---|---|---|
| `.cursor/rules/mitene-send-working.mdc` | 旧仕様（`priority_steps: []` 単一路線・`between_members_ms:[800,1200]`）を記載 | **現行仕様へ全面更新済み** |
| README「送信ロジック」節 | 旧仕様（`J10ComeonVisitorList.php` 単一・`約1〜2秒`・`[9000,32000]`） | **現行仕様へ更新済み。実コード/本書を正とする旨も追記** |
| `mypy.ini` の「Mixin 分割構成のため」コメント | 現行は単一クラス。`tests/` も現存しない | 未変更（CI 設定に影響しうるため触らない）。負債として記録 |
| `.pytest_cache/v/cache/nodeids` | 廃止済みの `tests/` を参照した残骸 | 未変更（`.gitignore` 済み・生成物）。負債として記録 |
| `human.typing_delay_ms` | `HumanBehavior.__init__` で代入のみ・参照なし | 保留（config と属性の対応を保つ。config は変更禁止） |
| `human.idle_pause_probability` / `idle_pause_ms` / `shuffle_tab_order` | 同上（代入のみ・参照なし） | 保留（同上） |
| `human.after_send_delay_ms` | `fast_send: true` の間は `after_send_pause` の 30-80ms 固定分岐で未使用 | 保留（`fast_send` を戻すと有効化されるため残す） |
| `_ensure_list_from_profile` | **`_prepare_list_page_before_collect`（line 4650）から呼ばれている＝現役** | 保留対象外（使用中）。前回調査の「呼び出し元なし」は誤り |
| `_tab_name_from_page_if_list` | `_ensure_on_step_list_for_parse` 付近（line 4678）から参照＝現役（常に「一覧」を返す1行） | 保留対象外（使用中） |
| `PICKUP_TAB_LABELS` | タブスクロール JS（4箇所）で使用中 | 保留対象外（使用中）。前回調査の「候補」は誤り |
| `REMAINING_SLASH_PATTERN` | 定義1・使用1（`_extract_remaining_from_text`）。重複ではない | 保留対象外（使用中）。前回調査の「重複」は誤り |
| `run_poll.js recoverAfterDisconnect` | `window.MiteneRunPoll` に export されるが呼び出し箇所なし | 保留（公開 API。挙動に影響なし） |
| `run_progress.js finish()` | 空関数。`window.MiteneRunProgress.finish` として export、呼び出しなし | 保留（同上） |
| `_scroll_merge_parse_enabled` / `member_scroll_merge_parse` | 解析経路は不変。照合不一致時のログ行の出し分けにのみ影響 | 保留（config 変更禁止・ログ用途） |
| `dist/` `build/` `screenshots/` | すべて `.gitignore` 済み（PyInstaller 出力・エラー証跡） | 保留（配布/障害調査に使う可能性） |
| `source_bundle.zip` `API設計書.zip` `名称未設定フォルダ/`（未追跡） | 古い作業ファイル/スクショ束 | **退避推奨（Phase 1 未実施）**。プロジェクト外 `_archive/` へ移動案。削除はしない |

**Phase 1 での結論: コードは1行も削除していない。** 変更は「ドキュメント3点」＋「`runner.py` に説明コメント1件」のみ。

---

## 14. デバッグコードの分類（Phase 1 では削除しない）

| 分類 | 対象 | 扱い |
|---|---|---|
| **障害調査に有効・残す** | `_log_run_send_reconciliation` / `_log_send_reconciliation_block` / `_finish_send_phase_tracking`（送信キュー→成功/失敗/未実行の照合）、`_record_send_attempt_from_last`、`_set_send_attempt_outcome`、`_save_error_screenshot`、`_read_send_budget` 内の `_log_budget_read`（残回数の読み取り根拠）、`_send_one_mitene` の「送信成功 …（残り N → M）」「送信未完了 …（状態: …）」ログ | **維持**。特に「実送信済みなのに result が失敗扱い」の追跡（`resolve_sent_this_run` / `_account_error_result` 経路、`_sent_member_keys` と `sent` の差）に必要 |
| **通常運用には不要・調査時のみ** | `member_extraction_debug` 系（`_log_member_extraction_debug` / `_log_pipeline_funnel_*` / `_log_per_send_debug_*` / `_debug_*` フィールド群 / `_log_collect_stage_debug` / `_log_tab_switch_debug` / `_log_scroll_metrics_debug`）。既定 `false` で出力自体がスキップされ、動作は不変 | **維持**（フラグで無効。消すと調査手段を失う） |
| **完全に不要** | 現時点で該当なしと判断（前回「候補」は 13 章の通りすべて使用中 or config 連動 or 別環境） | 削除なし |

> `[result_recovery]` / `original_status` / `recovered_from_error` という**文字列/シンボルは現行コードに存在しない**。
> 実送信済みなのに result が失敗扱いになるケースの追跡は、現状 `resolve_sent_this_run` の戻り値と
> `_account_error_result` の昇格ログ、`_log_run_send_reconciliation` の「成功 N / 失敗 M / 未実行 K」で行う。
> このあたりのログは Phase 1 で削らない。

---

## 15. 既知の技術的負債（Phase 1 では未修正・記録のみ）

1. **`config.yaml priority_steps` と `_execute_phased_send_pipeline` の二重管理**（7章 NOTE / 3章）。
   config はスイッチとしてしか効かない。統合は将来 Phase。
2. `src/mitene_sender.py` が 6547 行・単一クラス。`mypy.ini` は廃止済みの Mixin 分割前提のまま。
3. `settings.last_run` を保存しているが画面に描画するコードが無い（実質デッドな表示経路）。
4. `zero_send` が `ok=True` なのに UI では `errors` 側に出る（`ok` の意味と表示が逆）。
5. スケジュール実行が「画面内 APScheduler」＋「OS 登録」の二重発火構造。重複防止は
   `settings.last_run_slot`（ファイル）＋プロセス内ロックのみで、跨プロセスの競合窓が理論上ある。
6. `load_working_today_ids()`（読み取り関数）が日付跨ぎ検知で `save_settings` を呼び、
   Mac/Win では `sync_platform_schedule()`（OS 登録の再同期）まで走る副作用。
7. `member_cooldown_days` / `max_no_history_sends_per_day` は現行パイプライン経路で未評価だが
   config・コメント上は機能するように見える。
8. `_send_button_queue` 枯渇時に `_scan_unsent_member_keys` でページから拾い直すため、
   フェーズのフィルタを完全には通っていない会員が送られる余地がある。
9. `tests/` が存在せず、回帰を検知する自動テストが無い。

---

## 16. 次 Phase 候補（Phase 1 では実施しない）

- 送信速度 / 会員間インターバル（`between_members_ms`）/ アカウント間インターバル（`between_accounts_ms`）の見直し。
- 一覧取得速度（`_scroll_member_list_to_end` の安定判定・`LIST_AJAX_LOAD_WAIT_MS`）の最適化。
- `priority_steps` と `_execute_phased_send_pipeline` の統合（config を単一の制御元にする）。
- `mitene_sender.py` の分割（Mixin or モジュール分割。`mypy.ini` もそれに合わせる）。
- `settings.last_run` の画面表示 or 保存廃止。
- `zero_send` / `no_remaining` / `budget_read_failed` の UI 分類の整理。
- `tests/` の再整備（最低限、budget パース・フィルタ・`classify_result` の単体テスト）。

いずれも本 Phase では触らない。着手時は必ず本書を先に読むこと。
