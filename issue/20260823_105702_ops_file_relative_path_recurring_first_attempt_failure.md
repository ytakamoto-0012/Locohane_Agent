# --ops-fileに相対パスを渡して1回失敗するパターンが複数セッションで再発（自己修復するが毎回1往復のロス）

- **区分**: 改善点
- **検知日時**: 2026-08-23 10:57:02

- **対象ログファイル**: data/logs/app_20260823_104959.log

## 経緯

excel-vbaマクロブック作成タスク（2回目の再起動）で、workerが
`execute_python_code`で`ops1.json`を作成した直後、`run_script`の
`--ops-file`に相対パス`"ops1.json"`をそのまま渡し、「opsファイルが
見つかりません」で失敗した（`execute_python_code`のcwdと`run_script`の
cwdが異なるため）。8秒後、`os.path.abspath("ops1.json")`で絶対パスを
取得し再実行して成功。以降のops2〜ops5・ops_vbaはいずれも最初から
絶対パスで渡され、同種の失敗は起きなかった。

同種の事象は2026-08-12にも別セッションで発生している
（[issue/20260812_121000](20260812_121000_excel_edit_mergedcell_attributeerror.md)
の経緯1点目、ただしそちらは別バグの前振りとして記録されており本事象
自体は独立に追跡されていなかった）。SKILL.mdには既に
「`execute_python_code`のcwdは`run_script`の作業ディレクトリとは別
（セッション専用の`_tmp_<thread_id>`）」「単純に相対パスで書き、
`os.path.abspath("ops.json")`で得た絶対パスをそのまま`--ops-file`に
渡せばよい」という案内があるが、タスクの最初の1回目だけこれを踏まえずに
相対パスを直接渡し、毎回1往復（数秒〜十数秒）のロスをして自己修復する
パターンが繰り返し観測されている。

## ログ引用

```
2026-08-23 10:57:02,467 DEBUG src.subagent: subagent tool=run_script args={'skill_name': 'excel-edit', 'script_filename': 'edit_excel.py', 'script_args': ['E:\\user_b\\vba-test\\収支計算表.xlsm', '--ops-file', 'ops1.json']} -> '[終了コード] 1\n[標準エラー]\nopsファイルが見つかりません: ops1.json'
2026-08-23 10:57:10,278 INFO src.subagent: subagent tool=run_script args={'skill_name': 'excel-edit', 'script_filename': 'edit_excel.py', 'script_args': ['E:\\user_b\\vba-test\\収支計算表.xlsm', '--ops-file', 'C:\\DT_Python\\Locohane\\data\\temp\\_tmp_8bd8a017-44a3-48e8-8d17-f2c32e717182\\ops1.json']} -> [終了コード] 0
```

## 推定原因

SKILL.md記載のガイダンス自体は正しいが、プロンプト文中の説明として
埋もれており、低パラメータモデルがタスクの最初の1回だけ読み落とし、
「ops.jsonを作ったのでそのファイル名をそのまま渡す」という直感的な
（しかし誤った）選択をしてしまう。実害は無く自己修復するため緊急性は
低いが、タスクごとに再発する定型パターンであり、削減できれば毎回
数秒〜十数秒のロスと1往復分のトークン消費を防げる。

## 追記（2026-08-23 11:37）

同一セッション内の別タイミングで、今度は`_tmp_<thread_id>`のthread_id部分を
書かずに`E:\user_b\vba-test\_tmp_\ops.json`という存在しないパスを直接
指定して失敗する変種が発生。30秒後、`--ops-file`をやめて`--ops-json`に
opsをインライン指定する方法へ切り替えて成功した。

```
2026-08-23 11:37:44,372 DEBUG src.subagent: subagent tool=run_script args={'skill_name': 'excel-edit', 'script_filename': 'edit_excel.py', 'script_args': ['E:\\user_b\\vba-test\\収支計算表.xlsm', '--ops-file', 'E:\\user_b\\vba-test\\_tmp_\\ops.json']} -> '[終了コード] 1\n[標準エラー]\nopsファイルが見つかりません: E:\\user_b\\vba-test\\_tmp_\\ops.json'
```

引き続き実害・頻度は低い（自己修復済み）。

## 推奨対応（未実装）

- `edit_excel.py`/`edit_vba.py`の「opsファイルが見つかりません」エラー
  メッセージに、「相対パスの場合はexecute_python_codeの
  `os.path.abspath()`で得た絶対パスを渡してください」という具体的な
  次の一手を追記する（エラーメッセージ自体で自己修復を後押しする、
  列幅超過警告等と同じ「その場で解決策を示す」設計）。
- 実害・頻度は低い（1タスクにつき最大1回、10秒未満で自己修復）ため、
  対応は次回以降の再発頻度を見て判断する。

## ユーザー回答

ここにはユーザーの回答が記述される
