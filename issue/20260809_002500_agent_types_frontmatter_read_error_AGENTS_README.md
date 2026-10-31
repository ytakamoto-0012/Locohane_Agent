# agents_dir の AGENTS_README.md で frontmatter パース失敗

- **区分**: 改善点
- **検知日時**: 2026-08-09 00:25:00
- **対象ログファイル**: data/logs/app_20260809_000102.log, data/logs/app_20260809_002425.log

## 経緯

アプリ起動時（`app.py` の `initialize()` 経由）に `agents_dir`（既定 `./agents`）配下のエージェント種別定義ファイルを読み込んでいる際、`AGENTS_README.md` の frontmatter がパース失敗してスキップされている。このWARNINGが起動のたびに発生している。

## ログ引用

```
2026-08-09 00:01:02,551 WARNING src.agent_types: frontmatter を読めないためスキップ: AGENTS_README.md
2026-08-09 00:24:25,153 WARNING src.agent_types: frontmatter を読めないためスキップ: AGENTS_README.md
```

## 推定原因

`agents/AGENTS_README.md` の frontmatter 形式（`---` で囲まれたメタデータ部分）が壊れている、または `src/agent_types.py` の frontmatter パーサーが対応していない形式で記述されている可能性がある。

## 追記（2026-08-09 00:25）

2回目の起動でも同様のWARNINGが発生確認。

## ユーザー回答

ここにはユーザーの回答が記述される
