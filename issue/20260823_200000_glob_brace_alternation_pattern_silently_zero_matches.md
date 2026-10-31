# Globツールが`{a,b,c}`選択展開パターンを解釈できず常に0件になる（大文字小文字問題ではない）

- **区分**: バグ → 修正済み
- **検知日時**: 2026-08-23 19:52:57
- **対象ログファイル**: data/logs/app_20260823_195217.log

## 経緯

ユーザーがE:\共有\写真配下（615ディレクトリ・10680ファイル）の画像を探すよう
依頼したところ、LLMが`Glob(pattern='**/*.{jpg,jpeg,png,gif,bmp,webp}')`を実行し、
`total_matches: 0`が返った。LLMは「拡張子の大文字・小文字（`.JPG`）が原因」と
自己診断し、`{jpg,JPG,jpeg,JPEG,...}`と大文字小文字両方を列挙して再実行しようと
したが、メインエージェントのGlob呼び出し上限（1ターン1回）に達してエラーになり、
結局「画像が見つからないのでチャットに直接貼ってほしい」とユーザーに回答して
しまった。

ユーザーからも「大文字小文字問題のようだ」との報告があったが、実際に検証した
ところ原因は別だった。

## ログ引用

```
2026-08-23 19:52:57,066 INFO src.tools: Glob: pattern=**/*.{jpg,jpeg,png,gif,bmp,webp} base=E:\共有\写真
2026-08-23 19:52:57,066 DEBUG src.tools: tool_result: name=Glob content='{"base": "E:\\\\共有\\\\写真", "base_contents": {"directory_count": 615, "file_count": 10680}, "total_matches": 0, ...}'
...
2026-08-23 19:59:11,488 WARNING src.tools: tool_result: name=Glob content='エラー: Glob はメインエージェントとして既に呼び出し上限（1回）に達しています。...'
```

## 推定原因（検証済み）

[src/file_tools.py:171](../src/file_tools.py)の`glob_search()`は`base.glob(pattern)`
（`pathlib.Path.glob`）へパターンをそのまま渡している。Pythonのglob/fnmatchは
シェルのブレース展開（`{a,b,c}`）を一切解釈せず、`{`/`}`を単なるリテラル文字
として扱う。そのためファイル名が文字通り`.{jpg,jpeg,png,gif,bmp,webp}`という
拡張子（braceを含む）でない限り、常に0件になる。

実機検証（Python 3.11、Windows）:
```python
p.glob('*.{jpg,jpeg,png,gif,bmp,webp}')  # -> [] （常に空）
p.glob('*.jpg')                           # -> a.JPG と b.jpg の両方にマッチ
p.glob('*.JPG')                           # -> 同上（Windowsは大文字小文字を区別しない）
```
Windows上のパス照合はOSレベルで大文字小文字を区別しないため、そもそも
LLMが疑った「大文字小文字問題」は発生しない。真因はブレース展開の未対応で、
LLMの自己診断（および今回のユーザー報告）は結果が一致していたための誤診断
だった。

`Glob`ツールのdocstring（[src/tools.py:2064](../src/tools.py)）は`"**/*.py"`の
例しか示しておらず、`{}`構文が使えるとも使えないとも書いていなかったため、
LLMが（他のGlobツールの一般的な慣習から）自然に`{}`構文を使い、サイレントに
0件という誤解を招く結果を受け取っていた。

## 対応（実装済み・2026-08-23）

[src/file_tools.py](../src/file_tools.py)に`_expand_braces()`を追加し、
`glob_search()`が`base.glob(pattern)`を呼ぶ前に`{a,b,c}`を複数パターンへ
展開してそれぞれをglobし、結果を重複排除して合算するよう修正した。
`Glob`ツールのdocstring（[src/tools.py:2064](../src/tools.py)）にも
`{}`構文が使えること、大文字小文字はどちらか一方を書けば両方一致することを
明記した。

テスト: `tests/test_file_tools.py`に`test_brace_alternation_expands_to_union`
を追加。`pytest tests/test_file_tools.py`30件全通過。

## ユーザー回答

ここにはユーザーの回答が記述される
