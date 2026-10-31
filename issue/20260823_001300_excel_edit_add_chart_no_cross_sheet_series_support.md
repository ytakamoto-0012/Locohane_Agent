# excel-edit: 複数シートにまたがる系列を1グラフにまとめる手段が無く生のopenpyxlコードに逃げて失敗・ループ検知

- **区分**: 問題点
- **検知日時**: 2026-08-23 00:13:00
- **対象ログファイル**: data/logs/app_20260822_235526.log

## 経緯

excel-vbaマクロブック作成タスク中、「1月」〜「12月」の12シートそれぞれに
書き込んだ支出合計・収入合計・収支差額（各シートのB11/B16/B18セル）を
横軸12ヶ月・系列3本の1つの棒グラフにまとめようとした。

`excel-edit`スキルの`add_chart`opは`data_range`/`categories_range`が単一の
矩形範囲（同一シート内）を前提としており、複数シートに散らばったセルを
1系列としてまとめる手段が無い。そのためサブエージェントはスキルのopsを
使わず`execute_python_code`で直接openpyxlの`BarChart`/`Reference`/`Series`
を操作するコードを書いたが、不慣れな内部APIの誤用で2回連続失敗した。

1回目: `Reference`オブジェクトに`.append()`は無い（`AttributeError`）。
2回目: `openpyxl.chart.series.Series`のコンストラクタ第1引数は`idx`（int）で
あり、値のリストや`Reference`をそのまま渡すものではない（`TypeError`）。

2回目の失敗直後、LLMが「openpyxl 3.1.5での正しい使い方」を延々と自己解説
し始め、`src.llm`のループ検知（`LLM応答のループを検知したため生成を
打ち切ります`）が発動、1回目の再試行に入った。

## ログ引用

```
2026-08-23 00:10:30,986 WARNING src.subagent: subagent tool=run_script args={'script_filename': 'read_excel.py', ...
2026-08-23 00:11:13,070 DEBUG src.subagent: subagent tool=execute_python_code args={'code': '...vals_expense.append(Reference(ws, min_col=2, min_row=11, max_row=11))...'} -> '[終了コード] 1\n[標準エラー]\n...AttributeError: \'Reference\' object has no attribute \'append\''
2026-08-23 00:12:04,633 DEBUG src.subagent: subagent tool=execute_python_code args={'code': '...series1 = Series(series1_val)...'} -> '[終了コード] 1\n[標準エラー]\n...TypeError: expected <class \'int\'>'
2026-08-23 00:13:27,496 WARNING src.llm: LLM応答のループを検知したため生成を打ち切ります（直近テキスト: 'openpyxl 3.1.5での正しい使い方：...'）
2026-08-23 00:13:27,498 WARNING src.subagent: subagent: LLM応答のループを検知（1回目の再試行）: ...
```

## 推定原因

`excel-edit`スキルの`add_chart`/`update_chart`が単一シート内の
`data_range`/`categories_range`しか受け付けないため、複数シートの値を
集約する「サマリー表を別途作ってからそこを`data_range`にする」という
定石をSKILL.mdが案内していない。LLMは代わりに生のopenpyxl APIへ逃げ、
不慣れなAPI（`Series`コンストラクタ等）の誤用で連続失敗し、長い自己解説
テキストの生成がループ検知に引っかかった。

openpyxl自体はグラフの1系列が複数シートにまたがることを許容していない
（Excelのグラフも1系列は連続範囲が前提）ため、この制約自体はopenpyxlの
仕様であり、対応するなら「まず月別サマリー用の1シート（またはこの用途の
非表示補助シート）に12ヶ月分の値を集約してから`add_chart`する」という
回避パターンをSKILL.mdに明記するのが妥当な対応と考えられる（開発の余地、
未対応）。

## 追記（2026-08-23 00:20）

同一原因で再発。前回の失敗（`Reference.append`不在、`Series`コンストラクタ
誤用）を受けてループ検知の再試行後も、サブエージェントは依然として生の
openpyxl APIで解決しようとし続け、さらに2回失敗した。

1. `chart1.set_categories(month_names)`（`month_names`はPythonの文字列
   リスト）→ `set_categories`は内部で`Reference(range_string=labels)`を
   呼ぶため文字列以外は渡せず`TypeError: expected string or bytes-like
   object, got 'list'`。
2. 直後、`openpyxl.chart.data_source.StrData`のシグネチャを`inspect`で
   調べる（`execute_python_code`、成功だが本質的な解決には至っていない）。

```
2026-08-23 00:18:58,230 DEBUG src.subagent: ...chart1.set_categories(month_names)... -> '[終了コード] 1\n[標準エラー]\n...TypeError: expected string or bytes-like object, got \'list\''
2026-08-23 00:20:02,475 DEBUG src.subagent: ...inspect.signature(StrData.__init__)... -> '[終了コード] 0\n...'
```

計4回連続失敗＋1回のループ検知という実害が出ている（約10分間、生の
openpyxl内部API探索に費やされた）ため、`skills/excel-edit/SKILL.md`の
`add_chart`セクションに、複数シートの値を集計用の1シートへ`set_range`＋
他シート参照数式（`"='1月'!B11"`）で集約してからチャート化する回避パターンを
明記した（コード修正ではなくドキュメント追記で対応。openpyxl自体の制約
"1系列は連続範囲が前提"は変更できないため）。

## ユーザー回答

対策必要（2026-08-23確認）。→ 上記「追記（2026-08-23 00:20）」の通り、
同日中に`skills/excel-edit/SKILL.md`へ回避パターンを追記済み。
