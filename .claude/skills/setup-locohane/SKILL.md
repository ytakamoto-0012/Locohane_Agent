---
name: setup-locohane
description: Locohane を新しい環境（または clone 直後）で動かせる状態にセットアップする。README_DETAIL.md「セットアップと起動」節の手順（python_env.bat の Python 環境指定・requirements.txt の依存インストール・.env 作成・推論サーバー接続先の設定・CLAUDE.md の実行環境パス更新）をユーザーに確認しながら順に実施し、最後に設定の読込検証と起動方法の案内まで行う。「Locohaneをセットアップして」「環境構築して」「初期設定して」「/setup-locohane」等で使う。
---

# setup-locohane: Locohane の初期セットアップ

README_DETAIL.md「セットアップと起動」節（手順1〜4）を、現在の環境に合わせて実施する。
各手順は「現状確認 → 必要なら変更 → 確認」の順で進め、既に正しく設定済みの
手順は変更せずスキップする。手順の詳細が必要になったら README_DETAIL.md の該当見出し
だけを読む（全体は読まない）。

## 守ること

- **パスワード・秘密鍵の値を自分で決めない。** `.env` の `ADMIN_USERS`
  のパスワードはユーザーに入力してもらうか、ユーザー自身に編集してもらう。
- `.env` は絶対にコミットしない。ファイル内容を応答に貼り出さない。
- `config.ini` は git 管理下の既定値ファイル。書き換える前に必ずユーザーに確認する
  （管理ツールを使う運用なら、接続先は管理ツールのUIから設定するのが正規手順）。
- `admin.bat`・`app.bat` は末尾に `pause` があり常駐するため、自分では起動しない。
  起動はユーザーに案内する。

## 手順0: 運用方法を質問する

`AskUserQuestion`（`multiSelect: false`）で起動方法を聞く:
「管理ツール（admin.bat）経由（推奨）」/「app.bat で単体起動」

## 手順1: Python 環境

1. `python_env.bat` が無ければ `python_env.bat.example` をコピーして作る（`python_env.bat` は
   このマシンのパスを含むため git 管理外）。VS Code でデバッグするなら `.vscode/launch.json` も
   `.vscode/launch.json.example` から同じように作り、`python` のパスを下の2.で決めた環境に合わせる。
   `python_env.bat` の `PYTHON_DIR` を読み、`<PYTHON_DIR>\Scripts\python.exe`
   が存在するか確認する。
2. 存在しなければ、使う Python 仮想環境のディレクトリをユーザーに聞き、
   `PYTHON_DIR` の1行だけを書き換える。仮想環境自体が無ければ作成するか
   ユーザーに確認する（Python 3.11 系で `python -m venv <dir>`）。
3. 依存をインストールする:
   ```
   "<PYTHON_DIR>\Scripts\python.exe" -m pip install -r requirements.txt
   ```
   失敗したらエラー内容をそのままユーザーに報告し、勝手にバージョンを変えない。
4. Claude Code・Qwen Code も開発・テストでは `<PYTHON_DIR>\Scripts\python.exe` を使う（CLAUDE.md には
   パスを書かない）。`frontend/` をビルドする場合だけ、`python_env.bat` の `NODE_DIR` に Node.js の
   ディレクトリを設定する（`public/build` は同梱済みなので通常は不要）。

## 手順2: .env

1. プロジェクト直下に `.env` が無ければ `.env.example` をコピーして作り、
   サンプルユーザーの行 `AUTH_USERS=[["alice", "password1"]]` を
   `AUTH_USERS=[]` に書き換える。`AUTH_USERS` についてはユーザーに質問しない。
2. `ADMIN_USERS` が `.env.example` の初期値（`change-me`）のままなら、
   管理ツールのユーザー名/パスワードをユーザーに聞いて設定する
   （形式 `[["ユーザー名", "パスワード"]]`）。app.bat 単体運用なら不要。
3. `config.ini` の `[auth] enabled = true` の場合のみ:
   - `CHAINLIT_AUTH_SECRET` が空なら `"<PYTHON_DIR>\Scripts\chainlit.exe" create-secret`
     の出力値を設定する。

## 手順3: 推論サーバーの接続先

接続先はユーザーに質問して決める。`config.ini` の現在値を調べたり、
`curl` 等で推論サーバーを探したり接続確認したりはしない。

1. `AskUserQuestion`（`multiSelect: false`）で推論サーバーの種類を聞く:
   「llama-server（llama.cpp）」/「vLLM」/「その他のOpenAI互換」
2. 続けて base_url（例 `http://localhost:8080/v1`）とモデル名（llama-server の
   `--alias`、vLLM の `--served-model-name` の値）を文章で質問し、回答を待つ。
3. 回答した接続先を反映する:
   - 管理ツール運用: 起動後に「インスタンス」→「default」→「設定」→ `config.ini` タブで
     `main_url`/`sub_url` を変更・保存し再起動する手順を案内する。
   - app.bat 単体運用: `config.ini` の `main_url` と `sub_url` の
     `base_url`/`model`/`provider` を回答の値に書き換える。`provider` は llama-server なら
     `"llama_cpp"`、vLLM なら `"vllm"`、それ以外は省略する。

## 手順4: 検証

1. 設定が読み込めるか確認する（エラーなら内容を報告して修正する）:
   ```
   "<PYTHON_DIR>\Scripts\python.exe" -c "from src.config import load_config; c = load_config(); print('OK')"
   ```
2. pytest は llama-server 未起動だとハングするテストがあるため、セットアップでは実行しない。

## 手順5: 起動方法の案内と報告

実施した変更（書き換えたファイルと項目）・スキップした手順・未解決事項を
短く報告し、起動方法を案内する。推論サーバーは Locohane より先に起動しておく
必要がある旨も添える（起動例は README_DETAIL.md「2. 推論サーバー（llama.cpp / vLLM）の起動例」）。

- 管理ツール運用: `admin.bat` を起動 → `http://127.0.0.1:8001` に `ADMIN_USERS`
  でログイン。`default` インスタンス（既定 `http://127.0.0.1:8000`）が自動起動する。
- 単体運用: `app.bat` を起動 → `http://127.0.0.1:8000` を開く。
