# web-search スキルの導入手順（人間の管理者向け）

このファイルはLLMには読み込まれません（`read_skill` が読み込むのは `SKILL.md` の
本文のみで、`README.md` は progressive disclosure の対象外）。APIキー設定など、
導入時に一度だけ行う人間向けの作業をここにまとめます。LLMへ伝える運用ルール
（呼び出し方・エラー時の挙動等）は `SKILL.md` を参照してください。

## APIキー設定

このスキルは `TAVILY_API_KEY` を、プロジェクトルートの `.env` ではなく
**このスキル専用の `scripts/.env`** から読み込みます。

1. `scripts/.env.example` を同じ場所に `.env` としてコピーする。
2. https://app.tavily.com でアカウント登録し、APIキーを取得する（無料枠あり）。
3. `.env` の `TAVILY_API_KEY=` に取得したキーを設定する。

未設定のまま使うと、スクリプトは外部への通信を一切行わず、この設定手順を
案内するエラーメッセージを返すだけで終了します（LLMがそのメッセージを
そのままユーザーに案内します）。

## 危険サイト対策（ドメインフィルタ）

`scripts/.env.example`（＝`.env`の初期状態）は**`WEB_SEARCH_ALLOWED_DOMAINS`が
最初から有効な状態**で同梱されています。そのため手順通りに`.env.example`を
`.env`としてコピーしただけでも、Web検索結果は既定でgithub.com/pypi.org等の
開発系ドメインのみに限定されます（＝ホワイトリストモードがデフォルトON）。
「危険サイトを避けたいだけで、幅広く検索させたい」場合は、コピー後に
`WEB_SEARCH_ALLOWED_DOMAINS`を`[]`にするか、運用するプロジェクトの用途に
合わせてドメインを追加・削除してください（例: 本プロジェクトの実運用の
`.env`では、レシピアプリという用途に合わせて`cookpad.com`等の食品・栄養系
ドメインを追加してあります）。

組織のポリシーで検索結果からブロック/許可したいドメインがある場合は、
`scripts/.env` に以下を設定してください（書式・既定値のひな形は
`scripts/.env.example` を参照）。

- `WEB_SEARCH_BLOCKED_DOMAINS`: 既定で除外するドメインのリスト。
- `WEB_SEARCH_ALLOWED_DOMAINS`: 指定した場合、検索結果をこのドメイン群のみに
  限定する（ホワイトリストモード）。**既定で値が入っており有効化済み**（上記参照）。
- `WEB_SEARCH_TIMEOUT_SECONDS`: Tavily APIへの接続タイムアウト秒数（既定30）。

`WEB_SEARCH_BLOCKED_DOMAINS`と`WEB_SEARCH_ALLOWED_DOMAINS`を両方指定した場合、
実装は両者をAND条件で適用します（許可リストに含まれていても、除外リストにも
一致すれば結果から除外されます。片方が優先されるわけではありません）。

これらとLLMからの一時的な上書き（`--exclude-domains`/`--include-domains`）との
優先順位・合算ルールは `SKILL.md` に記載しています。
