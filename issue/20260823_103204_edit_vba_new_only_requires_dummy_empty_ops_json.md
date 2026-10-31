# edit_vba.py --new のみ（VBAコード無しでマクロ有効ブックの器だけ作りたい）が--ops-json/--ops-file必須のため1回失敗する

- **区分**: 改善点 → 対応済み
- **検知日時**: 2026-08-23 10:28:18

- **対象ログファイル**: data/logs/app_20260823_102118.log

## 経緯

excel-vbaマクロブック作成タスク（再々開後）で、plannerが作成した計画の
Step 1は「空のxlsmファイルを作成（`excel-vba-edit --new`）」という説明
だった（`excel-edit`の`--new`を先に使うとマクロ無効ファイルになってしまう
ため、SKILL.md通り`excel-vba-edit --new`を先に実行してから`excel-edit`で
シート・データを追記する設計）。

これを受けてworkerが最初に実行したのは、ops無しの
`edit_vba.py cashflow.xlsm --new --overwrite`だったが、
`--ops-jsonまたは--ops-fileのいずれかが必要です`で終了コード1になった。
5秒後、workerは自己判断で`--ops-json '[]'`を追加して再実行し、今度は
成功した（`applied_ops: 0`でファイルの器だけ作成）。実害は無く、
自己修復まで5秒程度で完了している。

## ログ引用

```
2026-08-23 10:28:18,118 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-vba-edit', 'script_filename': 'edit_vba.py', 'script_args': ['E:\\user_b\\vba-test\\cashflow.xlsm', '--new', '--overwrite']} -> [終了コード] 1
2026-08-23 10:28:18,118 DEBUG src.subagent: subagent tool=run_script args={'skill_name': 'excel-vba-edit', 'script_filename': 'edit_vba.py', 'script_args': ['E:\\user_b\\vba-test\\cashflow.xlsm', '--new', '--overwrite']} -> '[終了コード] 1\n[標準エラー]\n--ops-jsonまたは--ops-fileのいずれかが必要です'
2026-08-23 10:28:25,489 INFO src.subagent: subagent tool=run_script args={'skill_name': 'excel-vba-edit', 'script_filename': 'edit_vba.py', 'script_args': ['E:\\user_b\\vba-test\\cashflow.xlsm', '--new', '--overwrite', '--ops-json', '[]']} -> [終了コード] 0
```

## 推定原因

`edit_vba.py`の`main()`は`--recover-locks`指定時を除き、常に
`--ops-json`/`--ops-file`のどちらかを必須とする（2026-08-23の
ロック問題修正時に、旧`argparse`の`required=True`グループから手動チェック
へ置き換えたが、必須である点自体は変えていない）。

一方、SKILL.mdが示す「新規作成」の主要ユースケース（VBAプロジェクトを
持つ器だけを`excel-vba-edit --new`で先に作り、その後`excel-edit`で
シート・データを追記する）はops無し（＝VBAコードをまだ書かない）が
自然であり、実際に今回のplannerもops指定を明記せずworkerへ渡した。
`--ops-json '[]'`という「空配列を明示的に渡す」workaroundはLLM・SKILL.md
のどちらにも文書化されておらず、workerは自力で編み出す必要があった
（低パラメータモデルでは毎回自力で気づけるとは限らない）。

## 推奨対応（未実装）

以下のいずれかで、この1往復のロスを無くせる:
- `--new`指定時に限り`--ops-json`/`--ops-file`省略を許可し、省略時は
  `ops=[]`として扱う（`--new`なし時は従来通り必須のまま）。
- または、SKILL.mdの新規作成の説明（44行目付近）に
  「ops無しで器だけ作りたい場合は`--ops-json "[]"`を明示的に付ける」旨を
  一文追加する（コード変更不要、ドキュメントのみ）。

実害・頻度ともに小さい（1タスクにつき1回、5秒で自己修復）ため、
次回同種の事象が積み重なった場合に対応要否を判断する。

## 追記（2026-08-23 11:07）

同一セッション内の別タスク実行（`収支計算表.xlsm`再作成時、VBAプロジェクト
消失からの復旧中）で3回目の再発を確認。

```
2026-08-23 11:07:39,481 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-vba-edit', 'script_filename': 'edit_vba.py', 'script_args': ['E:\\user_b\\vba-test\\収支計算表.xlsm', '--new', '--overwrite']} -> [終了コード] 1
```

再発頻度が「次回積み重なった場合」の閾値に達したため対応した。

## 対応（実装済み・2026-08-23）

`skills/excel-vba-edit/scripts/edit_vba.py`の`main()`を変更し、
**`--new`指定時に限り`--ops-json`/`--ops-file`の省略を許可**、省略時は
`ops=[]`として扱うようにした（`--new`なし時は従来通り必須のまま）。
`skills/excel-vba-edit/SKILL.md`の引数一覧・新規作成の説明にも明記した。

テスト: `tests/test_excel_vba_edit_save_verification_and_recover_locks.py`に
`test_new_without_ops_defaults_to_empty_ops_list`・
`test_new_without_ops_but_with_output_flag_still_works`を追加。
`pytest tests/` 424件全通過。

## ユーザー回答

ここにはユーザーの回答が記述される
