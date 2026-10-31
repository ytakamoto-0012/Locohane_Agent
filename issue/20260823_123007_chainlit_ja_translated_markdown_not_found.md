# chainlit起動時に「Translated markdown file for ja not found」警告が毎回出る

- **区分**: 改善点
- **検知日時**: 2026-08-23 12:30:07
- **対象ログファイル**: data/logs/app_20260823_123006.log

## 経緯

アプリ再起動（ログローテーションで新規ファイルに切り替わったタイミング）
直後、6回連続で同じchainlit警告が出力された。

## ログ引用

```
2026-08-23 12:30:07,646 WARNING chainlit: Translated markdown file for ja not found. Defaulting to chainlit.md.
```

## 推定原因

`chainlit.md`（既定の起動時ウェルカム画面）はプロジェクトルートに存在するが、
日本語ロケール向けの`chainlit_ja.md`が存在しないため、chainlitが起動のたびに
警告を出しつつ既定の`chainlit.md`にフォールバックしている。機能への実害は
無い（正しくフォールバックしている）が、日本語UIを主に使う運用であれば
`chainlit_ja.md`を用意すれば警告自体を解消できる。

## 追記（2026-08-23 13:58）

対象ログファイル: data/logs/app_20260823_135730.log（アプリ再起動）

```
2026-08-23 13:57:34,165 WARNING chainlit: Translated markdown file for ja not found. Defaulting to chainlit.md.
2026-08-23 13:57:45,733 WARNING chainlit: Translated markdown file for ja not found. Defaulting to chainlit.md.
```

再発。原因・対応は上記から変わらず。

## 追記（2026-08-23 17:59）

対象ログファイル: data/logs/app_20260823_175334.log（アプリ再起動）

```
2026-08-23 17:53:35,617 WARNING chainlit: Translated markdown file for ja not found. Defaulting to chainlit.md.
2026-08-23 17:54:55,119 WARNING chainlit: Translated markdown file for ja not found. Defaulting to chainlit.md.
```

再発。原因・対応は上記から変わらず。

## 追記（2026-08-23 19:53）

対象ログファイル: data/logs/app_20260823_175334.log（複数タブ/再接続）

```
2026-08-23 19:51:53,880 WARNING chainlit: Translated markdown file for ja not found. Defaulting to chainlit.md.
2026-08-23 19:51:56,131 WARNING chainlit: Translated markdown file for ja not found. Defaulting to chainlit.md.
2026-08-23 19:51:59,555 WARNING chainlit: Translated markdown file for ja not found. Defaulting to chainlit.md.
2026-08-23 19:52:07,351 WARNING chainlit: Translated markdown file for ja not found. Defaulting to chainlit.md.
```

対象ログファイル: data/logs/app_20260823_195217.log（ローテーション後、新規ファイル冒頭）

```
2026-08-23 19:52:41,549 WARNING chainlit: Translated markdown file for ja not found. Defaulting to chainlit.md.
2026-08-23 19:52:45,822 WARNING chainlit: Translated markdown file for ja not found. Defaulting to chainlit.md.
```

再発。原因・対応は上記から変わらず。

## 追記（2026-08-23 20:29）

対象ログファイル: data/logs/app_20260823_195217.log（複数タブ/再接続）

```
2026-08-23 20:27:12,967 WARNING chainlit: Translated markdown file for ja not found. Defaulting to chainlit.md.
2026-08-23 20:27:14,317 WARNING chainlit: Translated markdown file for ja not found. Defaulting to chainlit.md.
```

再発。原因・対応は上記から変わらず。

## ユーザー回答

ここにはユーザーの回答が記述される
