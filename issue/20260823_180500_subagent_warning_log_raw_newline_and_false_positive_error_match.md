# src.subagentのWARNINGログが%s整形で改行をエスケープせず、かつ_contains_errorが正常系を誤検知する

- **区分**: バグ
- **検知日時**: 2026-08-23 18:05:00
- **対象ログファイル**: data/logs/app_20260823_175334.log

## 経緯

worker サブエージェントが docx-create スキルの `SKILL.md` を `read_skill`
で読み込んだ際、本来は成功しているだけの呼び出しにも関わらず `WARNING`
レベルでログ出力され、しかも本文中の改行がエスケープされずそのまま
書き出されたため、ログファイル上で**タイムスタンプの無い生の複数行**
（758〜771行目）に分断された。

## ログ引用

```
2026-08-23 18:05:00,309 WARNING src.subagent: subagent tool=read_skill args={'skill_name': 'docx-create'} -> ---
name: docx-create
description: JSON仕様を渡すだけでWord文書（.docx）を新規生成するスキル。...
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# docx-create

JSON仕様からWord文書（`.docx`）を新規生成するスキルです。`create_docx.py` を
`run_script` ツールで実行して結果を得ます。

このプロジェクトには汎用のファイル書き込みツールが無い
2026-08-23 18:05:00,309 DEBUG src.subagent: subagent tool=read_skill args={'skill_name': 'docx-create'} -> '---\nname: docx-create\n...'
```

（DEBUG側は`%r`整形により1行に収まっているが、WARNING側だけ複数行に分断されている）

## 推定原因

[src/subagent.py:107-120](../src/subagent.py) で、ツール結果を`WARNING`/`INFO`
レベルへログ出力する際に

```python
if is_execute_python or has_error:
    logger.warning(
        "subagent tool=%s args=%s -> %s",
        call["name"], call["args"], content[:500],
    )
else:
    logger.info(
        "subagent tool=%s args=%s -> %s",
        call["name"], call["args"], content[:500],
    )
if logger.isEnabledFor(logging.DEBUG):
    logger.debug("subagent tool=%s args=%r -> %r", call["name"], call["args"], tool_message.content)
```

同じ関数内の`DEBUG`ログ（最終行）だけが`%r`（repr、改行等をエスケープして
1行に収める）を使っており、`WARNING`/`INFO`側は`%s`（str、改行をそのまま
出力）を使っている。このため、ツール結果に改行を含む文字列（SKILL.md本文、
ファイル内容など）が渡ると、WARNING/INFOログだけがタイムスタンプ行と
生テキスト行に分断されてしまう。

`src/tools.py`側の同等処理（[src/tools.py:4818-4829](../src/tools.py)、メイン
グラフの`tool_result`ログ）は`WARNING`・`DEBUG`いずれも`%r`で統一されており
この問題は起きない。`src/subagent.py`側だけ`%s`のまま残っている実装の
不整合。

加えて、`WARNING`へ格上げする条件である`_contains_error()`
（[src/subagent.py:34-41](../src/subagent.py)）は文字列に`"エラー"`/`"error"`/
`"ｴﾗｰ"`が**単純に含まれるか**だけで判定しており、今回のように
`read_skill`が返すSKILL.md本文が**スキルの仕様として「エラーメッセージ」
「エラー原文」等の語を含んでいるだけ**でも誤って「エラーが発生した」と
判定され、成功しているのにWARNING扱いになる。この誤判定と改行未エスケープ
が重なった結果、今回のような分断ログが生じた。

この改行未エスケープの分断は、本監視スキルのようなタイムスタンプ・
ログレベルの行頭パターンに依存したgrep/正規表現ベースのログ解析全般に
とっても悪影響がある（対象行の後続の生テキスト行は`WARNING`扱いされないが、
次のタイムスタンプ付き行が来るまで独立した行として埋もれてしまう）。

## 追記（2026-08-23 18:08）

同一セッション内で再発（`read_skill(docx-create)`を再度呼び出した際も同様）。

```
2026-08-23 18:08:24,267 WARNING src.subagent: subagent tool=read_skill args={'skill_name': 'docx-create'} -> ---
```

（以降も同じくSKILL.md本文が生の複数行としてログに分断されている）

## 追記（2026-08-23 18:09）

`docx-read`・`docx-render`スキルの`read_skill`でも同じ現象を確認（同一セッション、verifierサブエージェント）。

```
2026-08-23 18:09:49,858 WARNING src.subagent: subagent tool=read_skill args={'skill_name': 'docx-read'} -> ---
2026-08-23 18:09:49,858 WARNING src.subagent: subagent tool=read_skill args={'skill_name': 'docx-render'} -> ---
```

## 追記（2026-08-23 20:15）

`pptx-create`スキルの`read_skill`でも再発（別セッション、対象ログファイル
data/logs/app_20260823_195217.log）。

```
2026-08-23 20:12:17,008 WARNING src.subagent: subagent tool=read_skill args={'skill_name': 'pptx-create'} -> ---
```

SKILL.mdの内容に依存せず、改行を含むSKILL.mdであれば対象スキルを問わず
再現することが確定的になった。

## 追記（2026-08-23 20:34）

`pptx-create`スキルの`read_skill`で再発（別セッション、対象ログファイル
data/logs/app_20260823_195217.log）。

```
2026-08-23 20:33:14,403 WARNING src.subagent: subagent tool=read_skill args={'skill_name': 'pptx-create'} -> ---
```

なお同一ログファイル内の3535行目（`src.tools`経由・メイングラフからの
`read_skill`呼び出し）は同じ`pptx-create`のSKILL.mdを返しているが、
こちらは`%r`整形により正しく1行に収まっており対照的（`src/tools.py`側は
バグの影響を受けないことの再確認）。

`pptx-render`・`pptx-read`スキルの`read_skill`でも同時刻に再発（同一セッション）:

```
2026-08-23 20:35:40,313 WARNING src.subagent: subagent tool=read_skill args={'skill_name': 'pptx-render'} -> ---
2026-08-23 20:35:40,313 WARNING src.subagent: subagent tool=read_skill args={'skill_name': 'pptx-read'} -> ---
```

`read_skill`で読み込むSKILL.mdが改行を含む限り、対象スキル名を問わず
必ず発生する（`_contains_error`の誤検知条件＝SKILL.md本文に「エラー」
「error」の語が含まれるスキルであれば、どのスキルでも再現しうる）。

## 追記（2026-08-23 18:18）

`docx-read`の`read_skill`で再度発生（同一セッション、別のexploreサブエージェント）。

```
2026-08-23 18:17:32,062 WARNING src.subagent: subagent tool=read_skill args={'skill_name': 'docx-read'} -> ---
```

## 追記（YYYY-MM-DD HH:MM）

## ユーザー回答

ここにはユーザーの回答が記述される
