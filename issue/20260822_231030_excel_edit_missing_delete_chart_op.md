# excel-editにグラフ削除の手段（delete_chart）が無く、LLMがSKILL.mdの内容を誤って思い込んで失敗を繰り返した

- **区分**: バグ（機能未実装） → 修正済み（`delete_chart` opを追加）
- **検知日時**: 2026-08-22 23:08:18〜23:10:18
- **対象ログファイル**: data/logs/app_20260822_230818.log

## 経緯

excel-vbaマクロブック作成タスクの終盤、add_chartの再試行によりシート3
「長期キャッシュフロー」に同種のグラフが4つ重複生成されてしまい、
不要な3つを削除しようとした。サブエージェントは以下のように
「SKILL.mdにdelete_chartが記載されている」と誤って思い込み、
存在しないopを呼び出し続けて失敗した。

```
まず、delete_chart opの仕様を確認します。SKILL.mdによると:
delete_chart: シートからチャート（グラフ）を削除。index（0始まり）またはtitleで特定。
```

```
2026-08-22 23:09:54,800 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-edit', 'script_filename': 'edit_excel.py', ...} -> [終了コード] 1
```

`dispatch_agent`の結果報告（23:10:18）:
```
### 失敗内容
**`delete_chart` opがexcel-editスキルで未対応**
```

## 推定原因（特定済み）

`skills/excel-edit/SKILL.md`・`skills/excel-edit/scripts/_ops.py`のいずれにも
`delete_chart`は一切存在しなかった（Grepで0件確認）。サブエージェントの
「SKILL.mdによると」という記述は事実と異なる幻覚だったが、その背景には
実際の設計上の非対称性があった。既存のadd/delete対を確認すると：

- `add_sheet`/`delete_sheet`
- `insert_rows`/`delete_rows`、`insert_cols`/`delete_cols`
- `add_table`/`remove_table`
- `merge_cells`/`unmerge_cells`

のように追加系オブジェクトには一貫して削除系opが用意されているが、
`add_chart`/`update_chart`だけには対になる削除opが存在しなかった
（`add_image`も同様に削除opが無いが、今回のタスクでは実害は出ていない）。
グラフは`run_script`の再試行やLLMの誤操作で重複生成されやすく、
削除できないことは実運用上の障害になりうる。

## 対応（修正済み）

`skills/excel-edit/scripts/_ops.py`に`op_delete_chart()`を追加し、
`chart_index`（0始まり、`update_chart`と同じ基準）または`title`で
対象を指定してシートから削除できるようにした。

- `title`指定時は`_chart_title_text()`ヘルパーで一致するグラフを検索する。
  openpyxlの`chart.title`は`str()`しても本文が取れないリッチテキストの
  入れ子オブジェクトのため、`title.tx.rich.p[0].r`から実テキストを
  個別に取り出す実装にした（単純に`str(chart.title)`で比較すると
  常に不一致になるバグを実装中に発見・回避）。
- `title`が0件/複数件一致の場合はエラー（`chart_index`での指定を促す）。
- 内部実装は`ws._charts`（openpyxlがグラフを保持する内部リスト）から
  該当要素を`del`するのみ。

`skills/excel-edit/SKILL.md`のopsテーブルに`delete_chart`を追記。

`tests/test_excel_edit_delete_chart.py`を新規作成し、以下を検証:
- `chart_index`指定で対象のグラフだけが削除され、他のグラフは残る
- 範囲外の`chart_index`はエラー
- `title`指定で一致するグラフが削除される
- `title`が0件一致／複数件一致はいずれもエラー
- `chart_index`/`title`両方省略はエラー

検証: `pytest tests/` 370件全通過。

## ユーザー回答

ここにはユーザーの回答が記述される
