# evals — プロンプト資産の自動ループテスト・チューニング

`system_prompt/system_prompt.md`・`skills/*/SKILL.md`・`src/tools/` パッケージ配下の各ツール
docstring といった「LLM に渡すプロンプト資産」を、実際のローカル LLM
（llama.cpp server）を動かして評価し、失敗があれば ClaudeCode が修正して
再評価する、というループを回すための仕組み。

## 前提

- llama.cpp server が `config.ini` の `[llm].main_url` に設定された接続先で
  起動していること（複数接続先・時間帯切替の設定もあり得るため、実際に
  使われる `base_url` は既定値を仮定せず `config.ini` を直接確認する）。
  起動していない場合、各ケースは `error: llm_unreachable` を返す。
- Chainlit サーバーは起動しない。`run_case.py` が `evals/headless_chainlit.py`
  で `chainlit` の UI 呼び出し（`cl.user_session` / `cl.Message` /
  `cl.AskActionMessage` / `cl.AskUserMessage`）をスタブに差し替え、
  グラフ（`src/graph.py`）を直接 `ainvoke` する。
- `recursion_limit`（`config.ini` の `[graph].recursion_limit`、既定50）・
  checkpointer（`AsyncSqliteSaver`、`:memory:` でファイルI/Oなし）は
  本番 `app.py` と同じ設定・実装を使う。`GraphRecursionError` /
  `ThinkingLoopDetected` が発生した場合も、本番同様そのターンだけ打ち切って
  会話を継続する（会話全体を `mid_turn_exception` として中断させない）。
  どのターンで打ち切りが起きたかは結果 JSON の `turn_cutoffs`
  （`[{"turn_index": ..., "reason": "recursion_limit"|"thinking_loop"}]`）に
  記録される。

## 実行方法

```
python evals/run_all.py system_prompt
```

`evals/cases/<target>/*.yaml` を全件、直列にサブプロセス実行する
（ローカル1台の llama.cpp server に同時多重リクエストをかけないため）。
結果は `evals/results/<target>/<timestamp>/results.json` /
`summary.md` に保存され、標準出力にも同じサマリが表示される
（`evals/results/` は再生成可能なデータのため `.gitignore` 対象）。

第3引数以降にケースID（ファイル名から拡張子を除いたもの）を空白区切りで
指定すると、そのケースのみをサマリ集計付きで実行できる
（`tune-prompt` スキルの対話でのケース選択に使用）:

```
python evals/run_all.py system_prompt 001_annual_schedule_investigation_before_plan
```

1ケースだけ試したい場合（サマリ集計不要、`run_case.py` を直接実行）:

```
python -m evals.run_case evals/cases/system_prompt/001_skill_routing_pdf.yaml
```

### 実行対象インスタンスの指定（`--instance`）

設定ダッシュボードで作ったインスタンス（`instances/<name>/`）の設定で評価する
場合は、`run_all.py`・`run_case.py` のどちらにも `--instance <name>` を付ける:

```
python evals/run_all.py system_prompt --instance <name>
python -m evals.run_case evals/cases/system_prompt/001_skill_routing_pdf.yaml --instance <name>
```

管理ツールがインスタンスを起動するときと同じく、`LOCOHANE_INSTANCE`・
`CONFIG_OVERRIDES_PATH`・`LOCOHANE_INSTANCE_ENV` を設定し、インスタンス別
`.env` を `override=True` で読み込んでから `load_config()` する
（`evals/instance.py`）。そのため `config_overrides.json` の上書きだけでなく、
インスタンス別 `.env` に書いた `LLM_MAIN_URL` 等の環境変数も反映される。
ケース yaml の `env:` と、メモリー等の一時ディレクトリへの隔離はさらにその上から
適用される。省略時は環境変数 `LOCOHANE_INSTANCE`、それも無ければ `default`。
存在しないインスタンス名を指定するとケースを1件も実行せずにエラー終了する。
どのインスタンスで実行したかは `summary.md` 先頭と `results.json` の各要素の
`instance` に記録される。

適用後の実効設定（LLM接続先・ログ出力先等）だけを確認したい場合:

```
python -m evals.instance <name>
```

### スキル安定化トライアウト（`--repeat`）

1回の合格が偶然でないかを確かめるため、各ケースを N 回ずつ直列に繰り返す:

```
python evals/run_all.py system_prompt --repeat 10 --instance <name>
```

ケースを一巡してから次の回へ進む（途中で止めても全ケースの回数が揃う）。各結果には
`repeat_index` が付き、`summary.md` 末尾の「スキル安定化トライアウト」節と `tryout.json` に
ケースごとの合格回数と判定を出す。判定は、全ケースが N 回すべてルール合格なら `pass`、
1回でも不合格・エラーがあれば `fail`、それ以外で judge 付きのケースがあれば `needs_judge`
（全回の transcript を読んで、全回合格と判断できたときだけ合格とする）。

開始時にケース（フォルダ直下の `*.yaml` 全部）と `--skill-overlay` のスキルを一時フォルダへ写し、
全回をその内容で評価する（途中でドラフトが編集されても回ごとに中身が変わらない）。`tryout.json` は
`--repeat 1` でも出力し、評価した内容のハッシュ（`cases_sha256`・`skill_overlays[].sha256`。規則は
`evals/skill_tree.py`）と実行したケース（`case_files`）も残す。promote-skill の昇格は、このハッシュが
今のドラフトと一致し、全ケースを `[skill_creator] tryout_repeats` 回以上評価した `tryout.json` でなければ
受け付けない（ドラフトに修正が入れば合格回数は0に戻る）。

### 任意のケースフォルダ・スキルを重ねた評価（`--cases-dir` / `--skill-overlay`）

ドラフトスキル（`data/<インスタンス名>/skill_drafts/<ユーザー名>/<スキル名>/`）の評価に使う:

```
python evals/run_all.py --cases-dir <ドラフト>/evals --skill-overlay <ドラフト> --repeat 10 --instance <name> --results-dir <出力先>
```

- `--cases-dir`: `evals/cases/<target>/` の代わりにそのフォルダ直下の `*.yaml` を実行する（target は省略でき、位置引数はケースIDとして扱う）。
- `--skill-overlay`（複数可）: 本番のスキル構成（`skills_dir`・`project_locohane_dir`）をそのまま残し、
  指定したスキルフォルダを一時ディレクトリへコピーして最優先で重ねる（`run_case.py` にも同名オプションがある）。
  同名の正式スキルがあれば上書きした状態で評価される。重ねたスキルの `scripts/*.py` は、昇格時に
  promote-skill がインスタンスの `config_overrides.json` へ登録するのと同じく、計画承認と
  `[main_agent_tool_guard]` を免除した設定（`plan_approval_exempt_scripts`・`allow_entries` に max_calls=-1 で
  追加した状態）で評価する（`src/skill_drafts.py` の `guard_exempt_entries_for_skill`）。
- `--exclude-skill`（複数可）: スキル一覧から除く（baseline 用）。
- `--results-dir`: 結果の出力先ルート（既定 `evals/results/<target>/`）。

skill-creator（`run_isolated_eval.py`）と promote-skill はこの形で呼んでいる。

### スキル研究室の評価（`--agent-overlay` / `--config-patch` / `--llm-from-instance`）

管理ツールのスキル研究室（`admin/skill_lab/evaluation.py`）は、研究テーマの全資産を重ねて評価する:

```
python evals/run_all.py --cases-dir <テーマ>/cases --instance <対象インスタンス> --repeat N --results-dir <出力先> \
  --skill-overlay <テーマ>/assets/skills/<スキル> ... --agent-overlay <テーマ>/assets/agents/<名前>.md ... \
  --config-patch <テーマ>/config_patch.json --llm-from-instance <スキル調整ワーカー>
```

- `--agent-overlay`（複数可）: サブエージェント定義（`agents/*.md` 形式のファイル）を、本番のエージェント種別
  （`agents_dir`・`project_locohane_dir`・`instance_locohane_dir` の `agents/`）の上に最優先で重ねる。
- `--config-patch`: `[subagent] agent_type_run_script_allowlist` 等のリスト型の設定へ、今の実効値に項目を足して評価する
  （`evals/config_patch.py`。昇格時に `config_overrides.json` へ書くのと同じ値になる）。
- `--llm-from-instance`: 構成は `--instance` のまま、LLM の接続先（`[llm] main_url`/`sub_url`/`*_routing_strategy`）だけを
  指定インスタンス（研究室）のものにする（環境変数 `LLM_MAIN_URL` 等で差し込む）。
- ケースの `work_dir` は、ケースのファイルからの相対パス（`fixtures/<フォルダ>`）でも書ける。その場合 `fixtures/` は
  ケースと一緒に開始時に固定され、`cases_sha256` にも含まれる。
- `tryout.json` には `agent_overlays`（ハッシュ）・`config_patch`（ハッシュ）・`llm_from_instance` も残る。

## ケースの書き方（`evals/cases/<target>/*.yaml`）

```yaml
id: skill_routing_pdf              # 一意な識別子
target: system_prompt              # チューニング対象カテゴリ（ディレクトリ名と一致させる）
turns:                              # 1スレッドの中でユーザーが順に送るメッセージ
  - "この請求書PDFからテーブルを抜き出してExcelにして"
expect:                              # ルールベース判定（省略可、以下はすべて省略可）
  tool_called_any: [read_skill]      # このいずれかが1回以上呼ばれていれば合格
  tool_not_called: [execute_python_code]   # これらが1回も呼ばれていなければ合格
  tool_call_args_contains:           # 該当ツールの呼び出しに指定引数が含まれるか
    read_skill: {skill_name: "pdf-tools"}
  response_contains: ["Excel"]       # 最終回答に含まれるべき文字列
  response_not_contains: ["申し訳ありません"]  # 含まれてはいけない文字列
judge: |                             # 自由記述の判定基準（省略可）。
  ClaudeCode が transcript を読んで合否判定する。expect と併用可、
  どちらか一方でもよいが両方無いケースは無効。
auto_approve: true                   # run_script/execute_python_code/approve_plan の
                                      # 承認ダイアログを自動承認(true)/拒否(false)するか
scripted_text_answers: []            # AskUserQuestion が labels 省略で呼ばれるたびに1件ずつ消費して返す回答
work_dir: "evals/fixtures/xxx"       # run_script/execute_python_code/analyze_image の既定
                                      # 作業ディレクトリをこのケース専用に固定したい場合の
                                      # プロジェクトルート相対パス（省略可、既定は config.ini
                                      # の [default_workdir].dir）
memory_seed: "evals/fixtures/xxx"    # ケース開始時に使い捨てのメモリーディレクトリへ写す
                                      # メモリー一式（<type>/<name>.md。省略可、既定は空の
                                      # メモリー。work_dir と同じ規則で解決する）
timeout_seconds: 3600                # run_all.py がサブプロセス実行する際のタイムアウト秒数
                                      # （省略可、既定は run_all.py の CASE_TIMEOUT_SECONDS=900。
                                      # 大量ファイルを扱う重量級ケースの上書き用）
notes: "人間向けの補足メモ（判定には使わない）"
```

- `expect` はルールベースで `run_case.py` がその場で pass/fail を出す。
- `judge` は ClaudeCode（人間の代わりに読む側）が transcript を読んで判断する
  自由記述の指示。ツール呼び出しの機械的な有無では判定しづらい「捏造していないか」
  「委譲判断が妥当か」といった観点に使う。

## 対象カテゴリ（`target`）を増やす場合

`evals/cases/<新しいtarget名>/` にケースを追加すれば、`run_case.py` /
`run_all.py` は変更なしでそのまま動く（`system_prompt` に限定した実装は無い）。
ただし現状 `run_case.py` は `system_prompt.md` を毎回ディスクから読み直す
前提の設計なので、`skill` や `tool_docstring` を対象にする場合も同様に
「チューニング対象ファイルは常にディスク上の現在の内容を読む」という設計を保つこと。

`system_prompt_scale`（`evals/cases/system_prompt_scale/`）は
`system_prompt/system_prompt.md` を対象にする点は `system_prompt` と同じだが、
`evals/fixtures/annual_schedule_large`（実データ規模を再現した大量ファイル
フィクスチャ、`python evals/fixtures/answer/system_prompt/generate_annual_schedule_fixture.py
--preset large` で生成）を使う重量級ケース専用のカテゴリで、
`/tune-prompt system_prompt` の自動ループ（毎イテレーション全件実行）には
含めない。`python evals/run_all.py system_prompt_scale` で手動実行する。

## チューニングループ本体

`.claude/skills/tune-prompt/SKILL.md` が、評価の実行・失敗分析・
対象ファイルの修正・スナップショット退避・再評価というループの手順書。
ClaudeCode で `/tune-prompt system_prompt` のように実行する。

- 編集前のスナップショットは `evals/history/<target>/` に退避される。
- 何を・なぜ変えたかは `evals/tuning_log.md` に追記される。
- イテレーション上限（既定10回）に達したら、途中経過を報告して停止する。
- git へのコミットは行わない（スナップショットとログのみで変更履歴を追える）。

## `config_timeouts` ターゲット（timeout系設定のチューニング）

`system_prompt` 等がプロンプト資産の**テキスト品質**を対象にするのに対し、
`config_timeouts`（`evals/cases/config_timeouts/`）は `config.ini` の
`[llm].request_timeout_seconds` / `[llm].stream_chunk_timeout_seconds` /
`[scripts].timeout` という**実測タイムスタンプに基づく数値パラメータ**を
対象にする、別系統のチューニングターゲット。

- `run_config`（`RunnableConfig`）に `evals/timing_callbacks.py` の
  `LatencyCallbackHandler` を `callbacks` として渡し、LLM呼び出し・
  `run_script`/`execute_python_code` の所要時間をターンごとに実測する
  （本番コード `src/` は変更しない）。
- 結果 JSON の各ケースに `turn_timings`（`token_usage_by_turn` と並列の構造、
  ターンごとの `max_llm_total_seconds` / `max_stream_chunk_gap_seconds` /
  `max_script_seconds` / 生データ）が追加される。
- 推奨値算出:
  ```
  python evals/analyze_timing.py config_timeouts
  ```
  最新の `evals/results/config_timeouts/<timestamp>/results.json` を集計し、
  現在値・実測最大値・推奨値・差分のテーブルを表示、
  同ディレクトリに `recommendations.json` を書き出す。**config.ini は
  直接書き換えない**（推奨値の提示のみ）。
- 推奨値の適用は手動で行う（`[user_response_timeouts]` セクションは
  人間の応答待ちでありハードウェアスペックと無関係なので対象外）。
  管理ツールで運用しているインスタンスでは、`config.ini` ではなく
  管理ツールから該当インスタンスの値を変更すること（`config_overrides.json`
  に同じキーがあると `config.ini` の変更は反映されないため）。
