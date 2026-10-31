# path_memory.register(): サンドボックスガードによるPermissionErrorでexcel-edit/excel-readが全滅

- **区分**: 問題点（重大・回帰）→ 修正済み
- **検知日時**: 2026-08-13 17:30:00
- **対象ログファイル**: data/logs/app_20260813_162817.log

## 経緯

`excel-edit`/`excel-read` スキルが `run_script` 経由で実行するたび、出力ファイルを
path_memory へ登録しようとする `register_output_path()`（`skills/*/scripts/_common.py`）
の内部で `path_memory.register()` が `PermissionError` を送出し、スクリプトが
毎回クラッシュ（終了コード1）していた。

サブエージェントは17:09〜17:26の間、原因を特定できないまま `edit_excel.py` /
`read_excel.py` の再実行、`_common.py`/`path_memory.py` のソース読解、
`../src/path_memory.py` への直接アクセス試行（`read_skill_file` が skills
ディレクトリ外アクセスとして拒否）を繰り返し、最終的に「スクリプトを修正すれば直るが
編集権限がない」という堂々巡りでLLM応答がループし、ループ検知により強制打ち切りとなった
（17:26:36）。この間 Excel ファイルの読み書きは一度も成功していない。

## ログ引用

```
2026-08-13 17:09:52,125 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-edit', 'script_filename': 'edit_excel.py', ...} -> [終了コード] 1
[標準エラー]
...
  File "C:\DT_Python\Locohane\src\path_memory.py", line 58, in _locked
    f = open(lock_path, "a+b")
  File "...\agent_fs_guard_oiysod0o\sitecustomize.py", line 17, in _guard_check
    raise PermissionError(
PermissionError: [書き込みサンドボックスガード] 作業ディレクトリ配下以外は書き込みできません: C:\DT_Python\Locohane\data\path_memory\f58292f2-ee75-492b-b1f2-6fb624165d8a.json.lock
作業ディレクトリ（またはdefault_workdir）配下のみ書き込み・削除可能です。
```

同一トレースが `read_excel.py` 側でも発生（17:12:59, 17:13:05, 17:13:15, 17:13:29,
17:13:45, 17:13:52, 17:14:23〜再試行を含め計10回以上）。

サブエージェント自身も原因を正しく言語化していた（17:20:21〜17:20:44）：

```
The issue is that `pm_dir` is `C:\DT_Python\Locohane\data\path_memory\` which is
outside the sandbox.
```

## 推定原因（根本原因を特定済み）

直近コミット `48f736d run_script/execute_python_codeの書き込みをサンドボックス限定に
強化` の回帰。

- `src/tools.py` の `_run_script_guard_env()`（876行目付近）は
  `allowed_roots = [workdir, _DEFAULT_WORKDIR]` のみをサブプロセスの書き込み
  サンドボックスガード（`_python_fs_guard_preamble`）に許可している。
- 一方、`skills/*/scripts/_common.py` の `register_output_path()` は
  `AGENT_SRC_DIR` 経由で `src/path_memory.py` を import し、`path_memory.register()`
  を呼ぶ。この関数は排他ロック用に `<AGENT_PATH_MEMORY_DIR>/<thread_id>.json.lock`
  （既定 `C:\DT_Python\Locohane\data\path_memory\`）へ `open(..., "a+b")` で
  書き込む（`src/path_memory.py:58` の `_locked()`）。
- `data/path_memory/` は Locohane 内部のシステムディレクトリであり、
  `allowed_roots`（work_dir / default_workdir）のどちらにも含まれないため、
  サンドボックスガードが誤って正規の内部書き込みをブロックしている。
- `register_output_path()`（`_common.py`）はこの呼び出しを try/except で
  囲んでいないため、`PermissionError` がそのままスクリプト全体をクラッシュさせる。

同様の呼び出しパターン（`_python_fs_guard_preamble([workdir.parent, _DEFAULT_WORKDIR])`
で `_PATH_MEMORY_DIR` を含めていない）は `execute_python_code` 側（tools.py 2576,
2589, 2717, 2730行目付近）にも存在し、`execute_python_code` 経由で
`path_memory.register()`/`path_memory.resolve()` を直接呼ぶコードも同じ理由で
失敗しうる（今回のログでは未確認だが同一コードパスのため要修正）。

[[locohane_write_sandbox_principle]] の原則（書き込みはwork_dir/default_workdir限定）
自体は妥当だが、Locohane自身の内部状態ディレクトリ（`data/path_memory/`）への
書き込みはユーザーの成果物ではなくアプリ基盤の一部であり、原則の対象外として
allowed_roots に追加するか、`register_output_path()` 側で例外を握りつぶして
`None` を返す（path_memory登録は失敗してもスクリプト本体は継続できる設計）の
どちらか、またはその両方で対応する必要がある。

## 既存issueとの関係

`20260813_172500_excel_read_returncode_1_no_sheet_specified.md` は同一事象
（`read_excel.py` の終了コード1）を「シート指定なしが原因」と推定していたが、
実際のスタックトレースを確認した結果、シート指定の有無に関わらず
`register_output_path()` → `path_memory.register()` の `PermissionError` が
根本原因だった。上記issueの推定原因は誤りとして本issueで訂正する。

## 追記（2026-08-13 17:30）

- 初回検知・根本原因特定（スタックトレース確認済み）

## 追記（2026-08-13 17:45）— 同系統の不具合を追加調査

ユーザーからの指摘を受け、「サンドボックスガードが許可リスト外への正規の
内部書き込みを誤ってブロックする」という同じ不具合カテゴリが他にもないか
横断調査した。

- `create_memory`/`update_memory`（永続メモリー）・`create_plan` の
  `detail_markdown` 保存は、LLMが直接呼ぶネイティブツールとしてメイン
  プロセス内で実行されるため `run_script`/`execute_python_code` の
  サブプロセス限定ガード対象外。影響なし。
- 全16スキルの `_common.py` を確認したが、`AGENT_SRC_DIR` 経由でimport
  しているのは `path_memory` のみ。他の内部ディレクトリへの同様の
  書き込みパターンは無し。
- **`skill-creator` スキルの `scripts/run_isolated_eval.py`
  （`tune-prompt`のスキル改善ループで使用）に2件目の同カテゴリ不具合を
  発見。** `_build_isolated_env()`（60-95行目）が
  `tempfile.mkdtemp(prefix="skill-creator-eval-")`（`dir=`未指定＝OS既定の
  一時フォルダ、`workdir`にも`default_workdir`にも含まれない）でディレクトリを
  作成し、`mkdir`/`copytree`/`copy2`で書き込む。`--mode without_skill` /
  `--mode old_skill` で実行時に発火し、`SKILL.md`記載の通り`run_script`
  経由で呼ばれるため同じ`PermissionError`で失敗する。同スキル内の
  `propose_description.py` は同じOS一時フォルダ問題を
  `tempfile.mkstemp(dir=str(workspace))` と明示的に`--workspace`配下へ
  指定することで正しく回避しており、これと同じ方式で直せる。
- 全 `skills/` 配下で `mkdtemp(`/`gettempdir(` を横断検索した結果、
  上記1件のみが該当（他は誤検知）。

ユーザー確認の上、この2件目もあわせて同じ修正計画に含めることを決定。

## 追記（2026-08-13 18:00）— 修正完了

以下2件を修正し、全テスト（245件）通過を確認:

1. `src/tools.py`
   - `_run_script_guard_env()`（876-919行目）: `allowed_roots` に
     `_PATH_MEMORY_DIR`（None時は追加しない）を追加。
   - `_python_fs_guard_preamble` 直前に共通ヘルパー `_exec_guard_roots(workdir)`
     を新設し、`execute_python_code`/`execute_python_code_background` の
     4箇所（旧2595, 2608, 2736, 2749行目付近）の重複していた
     `_python_fs_guard_preamble([workdir.parent, _DEFAULT_WORKDIR])` を
     `_python_fs_guard_preamble(_exec_guard_roots(workdir))` に統一。
   - `tests/test_tools_run_script_guard_env.py` に
     `test_subprocess_write_inside_path_memory_dir_succeeds` を追加
     （`_PATH_MEMORY_DIR`配下へのロックファイル書き込みがサブプロセス
     経由で成功することを検証）。
2. `skills/skill-creator/scripts/run_isolated_eval.py`
   - `_build_isolated_env()` に `workspace: Path` 引数を追加し、
     `tempfile.mkdtemp(prefix="skill-creator-eval-")`（OS既定の一時
     フォルダ）を `tempfile.mkdtemp(prefix="skill-creator-eval-",
     dir=str(workspace))` に変更（`propose_description.py` と同じ方式）。
   - `_cmd_start()` で `workspace` の計算を `_build_isolated_env()` 呼び出し
     より前に移動し、引数として渡すよう変更。

検証: `pytest tests/` 245件全通過。`run_isolated_eval.py` は
`py_compile` による構文確認のみ（既存の自動テストが無いため、実機での
`--mode without_skill` 実行確認は未実施）。

## ユーザー回答
