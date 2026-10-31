# execute_python_codeでopen()にencoding未指定だとcp932エラー、さらに簡体字「实」が日本語ヘッダーに混入していた

- **区分**: バグ（未修正）
- **検知日時**: 2026-08-23 10:57:54, 10:58:08

- **対象ログファイル**: data/logs/app_20260823_104959.log

## 経緯

excel-vbaマクロブック作成タスク（2回目の再起動）で、workerが「長期
キャッシュフロー」シート用のops（`ops4.json`）を`execute_python_code`で
生成する際、ヘッダー行に`"实际額"`という文字列を含めていた。この`实`は
**簡体字**（本来の日本語表記は`実際額`の`実`）であり、Windowsの既定
ANSIコードページ`cp932`（Shift-JIS、日本語のみ対応）ではエンコードできない
文字。1回目の`open("ops4.json", "w")`呼び出しが`encoding`引数省略のまま
だったため、`json.dump`が書き込み時に`UnicodeEncodeError`で失敗した。

workerは15秒後、`open(..., encoding="utf-8")`を追加して再実行し成功した。
しかし**`实际額`という簡体字混入テキスト自体は修正されず、そのまま
Excelへ書き込まれた**。同一タスク内の他のops生成コード（ops1/ops2/ops3/
ops5）はいずれも最初から`encoding="utf-8"`を指定しており、この回だけ
省略されていた。

## ログ引用

```
2026-08-23 10:57:54,574 DEBUG src.subagent: subagent tool=execute_python_code args={'code': '...\n        "rows": [\n            ["項目", "予定額", "实际額", "差分", "備考"],\n...\nwith open("ops4.json", "w") as f:\n    json.dump(ops, f, ensure_ascii=False)\n...'} -> '[終了コード] 1\n[標準エラー]\nTraceback (most recent call last):\n  File "...\\tmpz8lovnnu.py", line 228, in <module>\n    json.dump(ops, f, ensure_ascii=False)\n  File "...\\json\\__init__.py", line 180, in dump\n    fp.write(chunk)\nUnicodeEncodeError: \'cp932\' codec can\'t encode character \'\\u5b9e\' in position 3: illegal multibyte sequence'

2026-08-23 10:58:08,068 DEBUG src.subagent: subagent tool=execute_python_code args={'code': '...\n            ["項目", "予定額", "实际額", "差分", "備考"],\n...\nwith open("ops4.json", "w", encoding="utf-8") as f:\n    json.dump(ops, f, ensure_ascii=False)\n...'} -> '[終了コード] 0\n[標準出力]\nTotal ops: 18\n...'
```

## エラー原文

```
UnicodeEncodeError: 'cp932' codec can't encode character '\u5b9e' in position 3: illegal multibyte sequence
```

## 推定原因

1. **直接原因（クラッシュ）**: `execute_python_code`で生成されたPython
   コードが`open("ops4.json", "w")`を`encoding`引数無しで呼んでおり、
   Windows既定のANSIコードページ（日本語ロケールでは`cp932`）に
   フォールバックしていた。同タスク内の他のops生成コードは
   `encoding="utf-8"`を明示していたため、この回だけ省略された単発の
   ばらつきとみられる。
2. **根本原因（内容の誤り、未修正）**: `実際額`と書くべきところを
   `实际額`（簡体字の`实`+`际`）と生成していた。ローカルLLM
   （llama-server経由、reasoning_contentに中国語が混ざる場面が
   別issueでも観測済み）が日本語漢字と簡体字を混同した可能性が高い。
   `cp932`エンコードエラーという形で1回目は偶然表面化したが、
   2回目は単に`encoding="utf-8"`を追加しただけで**誤字はそのまま**
   Excelファイルへ書き込まれた。verifier・read_excel.pyの`warnings`
   機構のいずれも、漢字の字体（日本語字体/簡体字/繁体字）の妥当性を
   検証する仕組みを持たない。

## 推奨対応（未実装）

- **短期・低コスト**: `skills/office_shared/excel_common.py`や
  `execute_python_code`関連のガイダンス（SKILL.md/system_prompt）に
  「一時ファイルへ書き出す際は`open(..., encoding="utf-8")`を必ず指定する」
  旨を明記する（cp932クラッシュ自体の再発は防げる）。
- **本質的な対応は困難**: 簡体字/日本語字体の混同を機械的に検知するには
  Unicode正規化・異体字変換テーブル等が必要で、汎用的な誤検知なく実装する
  コストは高い。今回は実害が限定的（見出し1箇所の誤字）だったため、
  まずは事例として記録し、頻度が高まるようなら対策の要否を再検討する。

## ユーザー回答

ここにはユーザーの回答が記述される
