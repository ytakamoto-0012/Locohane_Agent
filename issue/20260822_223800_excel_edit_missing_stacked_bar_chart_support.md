# excel-editのadd_chartに積み上げ棒グラフを指定する手段が無く、思考ループを誘発していた

- **区分**: 問題点（機能未実装） → 修正済み（`grouping`パラメータを追加）
- **検知日時**: 2026-08-22 22:33:17〜22:38:09
- **対象ログファイル**: data/logs/app_20260822_203542.log

## 経緯

excel-vbaマクロブック作成タスクの終盤、長期キャッシュフローシートに
「積み上げ棒グラフ」を作成する作業で、サブエージェントが
`skills/excel-edit/scripts/_ops.py`を`Read`/`Grep`（`stacked|Stacked|add_chart`）
で調査し始め、その後LLM応答のループ検知で強制打ち切りになった。

```
2026-08-22 22:33:17,072 WARNING src.subagent: subagent tool=Grep args={'pattern': 'stacked|Stacked|add_chart', 'path': 'C:\\DT_Python\\Locohane\\skills\\excel-edit\\scripts\\_ops.py', 'context': 10} -> {"matched": true, ...
2026-08-22 22:38:09,212 WARNING src.llm: LLM応答のループを検知したため生成を打ち切ります（直近テキスト: 'ラフに修正する\n成果物: シート3に1つの積み上げ棒グラフが存...
```

## 推定原因（特定済み）

`skills/excel-edit/SKILL.md`の`add_chart`の`type`は`bar`/`line`/`pie`/`scatter`
のみで、積み上げの指定手段が無かった。実装（`_ops.py`の`op_add_chart`）を
確認したところ、`chart_cls()`でopenpyxlの`BarChart`を生成するだけで
`grouping`（積み上げ設定）属性を一切触っていなかった。

openpyxlの`BarChart`自体は`grouping`属性
（`clustered`/`stacked`/`percentStacked`）と、積み上げ時に系列を正しく
重ねて表示するための`overlap`属性を持っており、機能自体は
openpyxl側に既に存在する。excel-editスキルがこれをopとして公開して
いなかっただけで、根本的な技術的制約ではなかった。

サブエージェントはこの機能が無いことに気づき、ソースコードを読んで
回避策を探す（=Grep調査）→見つからず堂々巡りする、という展開になった。

## 対応（修正済み）

`skills/excel-edit/scripts/_ops.py`に`_apply_grouping()`ヘルパーを追加し、
`add_chart`/`update_chart`の両opへ`grouping`パラメータ
（`clustered`/`stacked`/`percentStacked`、`type:bar`限定）を公開した。
`stacked`/`percentStacked`指定時は`overlap=100`も自動設定し、系列が
正しく積み上がって見えるようにした。`type:bar`以外へ指定した場合や
未知の値を渡した場合はops適用前にエラーで弾く。

`skills/excel-edit/SKILL.md`のopsテーブルおよび「画像・グラフの追加と
調整」セクションに`grouping`の説明・使用例を追記。

`tests/test_excel_edit_chart_grouping.py`を新規作成し、以下を検証:
- `add_chart`で`grouping:stacked`/`percentStacked`指定時に
  `chart.grouping`・`chart.overlap=100`が正しく設定される
- `clustered`指定時・省略時は`overlap`を触らない
- `type:bar`以外への`grouping`指定、未知の値の指定はいずれも
  `ValueError`になる
- `update_chart`で既存の棒グラフへ後から`grouping`を適用できる
  （`grouping`単体でも必須引数チェックを通過する）

検証: `pytest tests/` 364件全通過。

## 追記（2026-08-22 22:44）— 修正後もこのサブエージェントは旧SKILL.mdのまま（想定内）

修正後、同一会話内で2回目の思考ループが検知された（22:44:49、計画の
組み立てで迷走）。このサブエージェントは`grouping`追加前の
`excel-edit`SKILL.mdを既に読み込んでコンテキストに保持しているため、
修正の恩恵を受けるのは**次にexcel-editスキルを新規に読み込む
サブエージェント/会話から**になる（
[issue/20260822_215000](20260822_215000_execute_python_code_cwd_diverges_from_workdir.md)
のdocstring修正と同じ制約）。実害は無いため新規issueは起票しない。

## ユーザー回答

ここにはユーザーの回答が記述される
