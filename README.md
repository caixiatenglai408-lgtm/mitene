# 姫デコ ミテネ！自動送信

複数の女の子アカウントを登録し、「ミテネできる会員を探す」から残り回数ぶんミテネをランダムに自動送信するシステムです。

## 機能

- **女の子ログイン管理** … ID・パスワードを画面から登録（全員分）
- **送信スケジュール** … 曜日ごとに「午前9時」「午後7時」「午前0時」をCTAでON/OFF
- **自動送信 ON/OFF** … 規制・ペナルティ時にワンタップで停止
- **定時実行** … 管理画面起動中、毎分チェックして各時間枠（約30分間）に全員送信

## セットアップ

### 別の Mac で初めて使う場合（重要）

フォルダをコピーしただけでは動きません。**新しい Mac では次が必要**です。

1. **コマンドラインデベロッパツール**（初回のみ・5〜15分）
   - 「ターミナル」を開き、次を実行:
   ```bash
   xcode-select --install
   ```
   - 表示されたウィンドウで「インストール」をクリック
   - `xcrun: error: invalid active developer path` と出たらこれが原因です

2. **`ブラウザをインストール.command`** をダブルクリック（Chromium 取得）

3. **`起動.command`** または **`ブラウザで開く.command`** で管理画面を開く

`.venv` フォルダは Mac ごとに作り直してください（別 PC からコピーしない）。

### 開発者向け（ターミナル）

```bash
cd "/Users/fujiseayaka/Desktop/自動送信機能"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# .env に MITENE_SECRET_KEY（任意）と FLASK_SECRET_KEY を設定
chmod +x scripts/start_dashboard.sh
```

暗号化キー生成:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## ローカルアプリとして起動（おすすめ）

### 方法1: アプリウィンドウ（専用画面）

**`起動.command`** をダブルクリック

- 専用ウィンドウが開きます（ブラウザのタブ不要）
- 裏で `http://127.0.0.1:5050` が動きます
- ウィンドウを閉じると終了します

### 方法2: macOS の .app 化

```bash
chmod +x scripts/create_mac_app.sh
./scripts/create_mac_app.sh
```

作成された **`ミテネ自動送信.app`** をダブルクリック（Dock に置けます）。

### 方法3: ブラウザで URL を開く

**`ブラウザで開く.command`** をダブルクリック、または:

```bash
.venv/bin/python launch_app.py --browser
```

#### デスクトップにアイコンを置く（Chrome などと同じショートカット）

```bash
chmod +x scripts/install_desktop_shortcut.sh
./scripts/install_desktop_shortcut.sh
```

デスクトップに **`ミテネ管理画面`**（DECOアイコン・矢印付きエイリアス）ができます。ダブルクリックで従来どおりブラウザが開きます。

**http://127.0.0.1:5050**（`http://localhost:5050` でも可）

**Google Chrome で開く**

`ブラウザで開く.command` とデスクトップの **ミテネ管理画面** は、既定で **Google Chrome** で URL を開きます（Chrome 未インストール時は macOS の標準ブラウザにフォールバック）。

**タブを閉じると Terminal も終了**

管理画面の Chrome タブをすべて閉じると、裏で動いているサーバーと黒い Terminal ウィンドウも自動で閉じます（複数タブで開いている場合は、最後のタブを閉じたとき）。

**ミテネ送信中**は、タブを閉じても **送信が完了するまでサーバーは動き続けます**。送信が終わってからタブを閉じると Terminal も閉じます。

別のブラウザにしたい場合:

```bash
.venv/bin/python launch_app.py --browser --browser-app Safari
```

または `.env` に `MITENE_BROWSER_APP=Safari` を設定。

**開けないとき（Mac）**

- 初回は **右クリック →「開く」**（ダブルクリックでは「開発元が未確認」で弾かれます）
- それでもダメなとき: **システム設定 → プライバシーとセキュリティ** で「このまま開く」
- Chrome などでダウンロードした場合は、ターミナルで次を実行してから再度ダブルクリック:
  ```bash
  cd "/パス/自動送信機能"
  xattr -cr .
  chmod +x *.command scripts/*.sh
  ```
- すでに **`起動.command` が動いている** 場合は、どちらか一方だけ使う
- 英語名の **`open-in-browser.command`** も同じ動作です

### Windows で起動する場合

#### 方法A: .exe 版（Python 不要・配布向け）

**Windows PC 上でビルド**（開発用 Mac からは .exe は作れません）:

```powershell
cd "自動送信機能"
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\build_windows_exe.ps1
```

`dist\MiteneAutoSend\` フォルダができます。**フォルダごと ZIP にして配布**してください。

| ファイル | 用途 |
|----------|------|
| **`MiteneAutoSend.exe`** | 本体 |
| **`ブラウザをインストール.bat`** | 初回のみ Chromium 取得 |
| **`起動.bat`** | 専用ウィンドウで起動 |
| **`ブラウザで開く.bat`** | ブラウザで管理画面を開く |
| **`使い方.txt`** | 初回手順 |

データ（`data\`・`logs\`・`config.yaml`）は exe と同じフォルダに保存されます。

#### 方法B: Python + venv 版（開発・従来）

| ファイル | 用途 |
|----------|------|
| **`起動.bat`** | 専用ウィンドウで起動 |
| **`ブラウザで開く.bat`** | ブラウザで管理画面を開く |
| **`ブラウザをインストール.bat`** | Playwright 用ブラウザの再インストール |

初回は [Python 3](https://www.python.org/downloads/) のインストールが必要です（「Add python.exe to PATH」にチェック）。

| ファイル | 用途 |
|----------|------|
| `web/static/icons/deco-app-icon-1024.png` | 姫デコアイコン（透過・1024px） |
| `web/static/icon.icns` | Mac `.app` 用（`scripts/build_app_icon.py` で生成） |
| `web/static/favicon.ico` | ブラウザタブ用 |
| `scripts/build_app_icon.py` | PNG → icns / favicon |
| `scripts/process_deco_icon.py` | 元画像から PNG を再生成 |
| `scripts/create_mac_app.sh` | `ミテネ自動送信.app` を作成（DECOアイコン付き） |

プレビュー: 管理画面起動後 **http://127.0.0.1:5050/portal-preview**

Portal 登録例: 名前「姫デコ ミテネ自動送信」、URL `http://127.0.0.1:5050`（常時起動時）

### 方法4: Dock に追加（PWA）

Chrome / Edge で `http://127.0.0.1:5050` を開き、メニューから **「アプリをインストール」** または **「Dock に追加」** でアプリのように使えます。

### 使い方

1. **女の子ログイン** … 共通のログインURLを保存し、各女の子のID・PWを追加
2. **設定・スケジュール** … 曜日×時間のCTAをON、必要なら「自動送信 ON」
3. 規制時は **自動送信 OFF**（定時送信が止まります）

### 手動ボタン

| ボタン | 説明 |
|--------|------|
| 今すぐ全員送信 | 自動送信ON時のみ、全員本番送信 |
| 全員ドライラン | OFFでも可。送信せず動作確認 |
| 各女の子の「今すぐ送信」 | 1人だけ実行 |

## 送信ロジック

> **正確な現行仕様は [`docs/current_baseline.md`](docs/current_baseline.md) を参照。以下は概要で、食い違う場合は実行コードと `docs/current_baseline.md` が正。**

1. **①** ログイン（女の子ログインに登録したID・PW / `J1Login.php`）
2. **②** ホームで **ミテネ残り回数** を取得 → その回数を「送信予算（budget）」として固定
3. **③** `config.yaml` の `flow: standard` / `priority_steps`（7件）で、`_execute_phased_send_pipeline` が次の順に会員一覧を巡回して「ミテネを送る」を予算ぶん送信:
   1. マイガール（新規のみ）
   2. キープ（新規のみ）
   3. マッチ率（新規のみ）
   4. マイガール（送信日が古い順）
   5. みたよ（③マッチ率で新規0件のときだけ）
   6. キープ（送信日が古い順）
   7. マッチ率（送信日が古い順）
4. **1会員ずつ** 送信（連打しない）。同一実行内で同じ会員への再送・失敗リトライはしない。
5. **送信間隔** … `config.yaml` の `human.between_members_ms`（現在 `[300, 500]` = 0.3〜0.5秒）。女の子どうしの間隔は `between_accounts_ms`（現在 `[3000, 5000]` = 3〜5秒）。

規制が心配なときは `config.yaml` の `human.between_members_ms` / `between_accounts_ms` を長めにしてください（例: `[9000, 32000]`）。

> `config.yaml` の `priority_steps` は現状「7フェーズ経路を有効化するスイッチ」としてのみ働き、巡回順そのものは `_execute_phased_send_pipeline` 側のハードコードが優先します（詳細は `docs/current_baseline.md`）。

## CLI（1人だけ試す場合）

```bash
# .env に HIMEDECO_LOGIN_ID / PASSWORD / BASE_URL を設定
python src/main.py --dry-run --headed
```

## データ保存場所

| ファイル | 内容 |
|----------|------|
| `data/accounts.json` | 女の子一覧（パスワードは暗号化推奨） |
| `data/settings.json` | ON/OFF・スケジュール・ログインURL |
| `logs/{女の子ID}/` | 送信ログ |
| `playwright/.auth/{女の子ID}.json` | セッション |

## スリープ中でも送信（Mac / Windows）

管理画面の **「スリープ対策 ON」**（デフォルトON）で、OS の定時実行に登録されます。管理画面が閉じていても送信できます。

| OS | 仕組み |
|----|--------|
| **Mac** | `launchd` で送信時刻に実行。任意で `pmset` によるスリープ解除予約 |
| **Windows** | タスクスケジューラ「`MiteneAutoSend`」（**スリープから復帰して実行 / WakeToRun**） |

**確実に動かすコツ**

- ノートPCは **電源接続**・**フタ開き**（バッテリーのみ・フタ閉じでは起きないことが多い）
- 設定変更後は **「登録を再同期」** を押すか:
  - Mac: `bash scripts/install_platform_schedule.sh`
  - Windows: `スリープ対策を登録.bat` または `scripts/install_platform_schedule.ps1`
- Mac のスリープ解除（任意）: `bash scripts/schedule_wake_sudo.sh`

**限界**

- 電源オフ・完全シャットダウン中は送信されません
- 最も確実なのは従来どおり **アプリを起動したまま＋スリープしない** 運用です

## Vercel + 送信ワーカー（みんなで使う場合）

管理画面を Vercel で公開し、**送信もブラウザから**行う構成です。

| 役割 | ホスト |
|------|--------|
| 管理画面（登録・設定・送信ボタン） | Vercel |
| データ保存（登録・設定） | Vercel Redis（`REDIS_URL` 等） |
| Playwright 実行（実際の送信） | Railway 等の **送信ワーカー** |

Vercel 単体ではブラウザ自動操作ができないため、`worker/` を別サーバーにデプロイします。

### 1. Railway にワーカーをデプロイ

1. [Railway](https://railway.app/) で GitHub リポジトリ `mitene` を Import
2. **Root Directory** はリポジトリ直下のまま
3. Railway が `worker/Dockerfile` を検出してビルド
4. **Variables** に Vercel と同じ値を設定:
   - `REDIS_URL`（または `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN`）
   - `MITENE_SECRET_KEY`（パスワード暗号化用・任意）
   - `MITENE_WORKER_SECRET`（任意・推奨）
5. デプロイ後の URL を控える（例: `https://mitene-worker.up.railway.app`）

### 2. Vercel にワーカー URL を設定

Vercel → プロジェクト `mitene` → **Settings → Environments**（環境変数）:

| 名前 | 値 |
|------|-----|
| `MITENE_WORKER_URL` | Railway の URL（末尾スラッシュなし） |
| `MITENE_WORKER_SECRET` | Railway と同じ秘密文字列（設定した場合） |

設定後 **Redeploy**。

### 3. 動作確認

- 管理画面の黄色・青い警告が消える
- 「今すぐ送信」でエラーにならない

## 注意

- `data/accounts.json` は他人に見られないよう保管してください
- 利用規約・店舗ルール・規制期間は **自動送信 OFF** で運用してください
- **スリープ中も送信（Mac）** … 管理画面で「スリープ対策 ON」にすると、送信時刻に Mac を起こして送信します（`起動.command` が閉じていても可）。電源接続・フタ開きが確実。詳細は下記
- 初回は `pywebview` のインストールで数分かかることがあります
