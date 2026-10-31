---
name: release
description: Locohane の新しいバージョンをリリースする。README_DETAIL.md「バージョン管理とリリース手順」節に沿って、前回タグ以降のコミットから次の番号（MAJOR.MINOR.PATCH）を提案し、CHANGELOG.md の [Unreleased] を版の節へ確定・VERSION 更新・必要ならフロントエンドのビルド・pytest・「Release vX.Y.Z」コミット・注釈付きタグ・push・GitHub Release 作成までをユーザーに確認しながら行う。「リリースして」「バージョンを上げて」「タグを打って」「/release」等で使う。
---

# release: Locohane のリリース

README_DETAIL.md の「バージョン管理とリリース手順」節が正本。番号の上げ方・手順の詳細が
必要になったらその見出しだけを読む（全体は読まない）。

## 守ること

- `main` ブランチ・作業ツリーがクリーンな状態からだけ始める。そうでなければ止めて状況を伝える。
- push・タグの push・GitHub Release の作成は外部公開にあたる。**手順6 の前に必ずユーザーの承認を得る。**
- テストが失敗したらリリースしない。失敗内容をそのまま報告する。
- バージョン番号を書く場所は `VERSION` と `CHANGELOG.md` だけ。他のファイルに番号を書き足さない。

## 手順1: 現状確認

```bash
git status --short; git branch --show-current
cat VERSION
git describe --tags --abbrev=0
git log $(git describe --tags --abbrev=0)..HEAD --no-merges --format='%h %s'
```

前回タグ以降のコミットが無ければ「リリースする変更が無い」と伝えて終わる。
タグが1つも無く `git describe` が失敗するとき（このリポジトリで最初のリリース）は、`VERSION` の番号を前回の版とし、
`git log --no-merges --format='%h %s'` で全コミットを見る。

## 手順2: 次の番号を決める

`CHANGELOG.md` の `## [Unreleased]` と手順1のコミット一覧を読み、README_DETAIL の「番号の上げ方」に従って
MAJOR・MINOR・PATCH のどれを上げるべきかを判断する。特に次のものがあれば互換性のない変更として扱う:
`config.ini` のキーの移動・改名・削除、データ配置の変更、ツール・スキル・サブエージェントの廃止・改名。

`AskUserQuestion` で、推奨する番号（理由つき）と他の候補を示して選んでもらう。

## 手順3: CHANGELOG.md を確定する

- `## [Unreleased]` に書かれていない利用者・運用者向けの変更がコミット一覧にあれば補う
  （内部のリファクタリング・evals のチューニング記録・issue 追記は書かない）。
- 互換性のない変更は、旧キー → 新キーなどの具体的な手順を「移行時の注意」に書く。
- 内容を `## [X.Y.Z] - YYYY-MM-DD`（今日の日付）へ移し、空の `## [Unreleased]` を残す。
- 末尾の比較リンクを更新する:
  `[Unreleased]: .../compare/vX.Y.Z...HEAD` と `[X.Y.Z]: .../compare/v<前回>...vX.Y.Z`。
  このリポジトリ（Locohane_Agent）の履歴は v0.4.6 の後から始まっていて、v0.4.6 以前のタグは無い。
  前回の版のタグが無いとき（このリポジトリで最初のリリース）は、`[X.Y.Z]: .../commits/vX.Y.Z` にする。

書いた節をユーザーに見せて確認してもらう。

## 手順4: VERSION の更新とビルド・テスト

- `VERSION` を `X.Y.Z` の 1 行に書き換える。
- 前回タグ以降に `frontend/src/` の変更があれば、`frontend/` で `npm run build` を実行し、
  `public/build/` の更新を含める（Node.js は `python_env.bat` の `NODE_DIR`。`call python_env.bat` 後なら PATH に入っている）。
- `python_env.bat` の `PYTHON_DIR` の Python（`<PYTHON_DIR>\Scripts\python.exe`）で `-m pytest -q` を実行し、全件成功を確認する。

## 手順5: コミットとタグ（ローカル）

```bash
git add -A
git commit -m "Release vX.Y.Z"
git tag -a vX.Y.Z -m "Locohane vX.Y.Z"
```

コミットメッセージの末尾には、会話で指示されている Co-Authored-By 行を付ける。

## 手順6: push と GitHub Release（承認後）

ユーザーの承認を得てから実行する。

```bash
git push origin main vX.Y.Z
```

`CHANGELOG.md` の `## [X.Y.Z]` 節（見出し行を除く）を一時ファイルに書き出し、それを本文にする:

```bash
gh release create vX.Y.Z --title "Locohane vX.Y.Z" --notes-file <一時ファイル>
```

最後に、リリースの URL と、次の開発で `## [Unreleased]` へ追記していく運用をユーザーに伝える。
