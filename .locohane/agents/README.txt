# .locohane/agents/

ビルトインの `agents/` を汚さずに置ける、ユーザー独自のエージェント種別
（`dispatch_agent` の `agent_type`）定義用のディレクトリ。`agents/` とマージ
走査され、同名定義が両方に存在する場合はこちら側が優先される
（`src/agent_types.py` の `scan_agent_types()` 参照）。

`*.md` 1ファイル = 1種別。frontmatter で `name` / `description` / `tools`
（省略時は既定のツール一式を継承）を指定する。書き方の詳細は
`agents/explore.md` を参照。
