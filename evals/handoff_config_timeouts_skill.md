# 引き継ぎ: config_timeouts 自動チューニングSKILL実装

計画: `C:\DT_Python\claudecode\.claude\plans\bubbly-imagining-castle.md`
worktree: `C:\DT_Python\Locohane\.claude\worktrees\config-timeout-tuning-skill`
（branch: `worktree-config-timeout-tuning-skill`）

## 目的

`config.ini` の timeout系設定3項目（`[llm].request_timeout_seconds` /
`[llm].stream_chunk_timeout_seconds` / `[scripts].timeout`）を、実行環境の
ハードウェアスペックに応じて実測ベンチマークし、適正値を自動算出・適用できる
仕組みを作る。既存の `tune-prompt`（プロンプト資産の**テキスト品質**を評価する
ループ）と同じアーキテクチャ（evals専用ケース + SKILL.mdの反復手順）を流用しつつ、
対象が**実測タイムスタンプに基づく数値パラメータ**である点が異なる。

対象外: `[timeouts]` セクション（人間の応答待ち、ハードウェアスペックと無関係）。

## 実装済みファイル

- `evals/timing_callbacks.py`（新規）: `LatencyCallbackHandler`
  （`BaseCallbackHandler`）。LLM呼び出し（`on_llm_start`/`on_chat_model_start`/
  `on_llm_new_token`/`on_llm_end`/`on_llm_error`）と `run_script`/
  `execute_python_code` の実行時間（`on_tool_start`/`on_tool_end`/
  `on_tool_error`、対象ツール名でフィルタ）を計測し、`summary()` で
  `max_llm_total_seconds`/`max_stream_chunk_gap_seconds`/`max_script_seconds`
  を返す。`reset()` でターンごとにクリアする設計。
- `evals/run_case.py`（変更）: `run_config["callbacks"] = [timing_handler]` を
  追加し、ターンループ内で `timing_handler.reset()`（各ターン開始前）→
  `timing_handler.summary()` を `turn_timings` リストへ蓄積（各ターン終了後、
  `GraphRecursionError`/`ThinkingLoopDetected` による打ち切りパスでも同様に
  記録）→ 結果 `out` 辞書に `out["turn_timings"] = turn_timings` を追加。
  既存の `token_usage_by_turn` 構築ロジックとは独立しており、他ターゲット
  （`system_prompt` 等）の挙動・出力には影響しない（`turn_timings` という
  新規キーが追加されるのみ）。
- `evals/cases/config_timeouts/001_llm_heavy_investigation.yaml`:
  `work_dir: evals/fixtures/annual_schedule_large100`（10年分・129ファイル、
  未文書化だが既存の重量級フィクスチャ）で大量ファイル探索を発生させ、
  LLM呼び出し所要時間・チャンク間隔の実測用。`timeout_seconds: 3600`。
- `evals/cases/config_timeouts/002_script_heavy_excel_generation.yaml`:
  `system_prompt_scale/001_..._large.yaml` と同じパターン
  （`work_dir: evals/fixtures/annual_schedule_large`、年間行事予定表xlsx生成）
  を流用し、`run_script`（excel-tools/edit_excel.py）の実行時間計測用。
- `evals/cases/config_timeouts/003_long_generation_response.yaml`:
  work_dir不要の軽量ケース。単一LLM呼び出し内の持続的なストリーミング生成で
  チャンク間隔の安定性を計測する。`timeout_seconds: 600`。
- `evals/analyze_timing.py`（新規）: `evals/results/config_timeouts/<timestamp>/
  results.json` を集計し、`configparser` で読んだ `config.ini` 現在値と比較した
  推奨値テーブル（Markdown、標準出力）を出す。推奨値算出式:
  - `request_timeout_seconds` = `ceil(max_llm_total_seconds × 1.5)`（下限60秒）
  - `stream_chunk_timeout_seconds` = `ceil(max_stream_chunk_gap_seconds × 2.0)`（下限30秒）
  - `scripts.timeout` = `ceil(max_script_seconds × 1.5)`（下限60秒）
  - 現在値との差が ±20% 未満なら「変更不要」。
  `config.ini` は直接書き換えない（`recommendations.json` を結果ディレクトリへ
  書き出すのみ）。
- `.claude/skills/tune-config-timeouts/SKILL.md`（新規）: `tune-prompt/SKILL.md`
  の構成（前提確認→ベースライン退避→評価実行→判定→修正→再評価ループ、
  安全策）を踏襲。イテレーション上限3回、`evals/history/config_timeouts/` へ
  スナップショット退避、`evals/tuning_log.md` へ `### config_timeouts iterNN`
  見出しで記録、gitコミットは一切行わない方針を明記。
- `evals/README.md`・`README.md`: `config_timeouts` ターゲット・
  `turn_timings`・`analyze_timing.py`・新スキルへの追記（小規模）。

## 設計上の判断根拠（詳細は計画ファイル本体を参照）

- 計測はLangChainコールバック（`run_config["callbacks"]`）を使う。本番グラフ
  実装（`config.ini` の `[graph].implementation` 既定 `prebuilt` =
  `create_react_agent`）はLangGraph標準のconfig伝播に乗っているため、
  `run_case.py` の変更のみで計測でき、本番コード（`src/graph.py` 等）は
  一切変更していない。
- `graph_impl=handwritten` の場合は `_build_handwritten_graph` の
  `call_model` ノードが `config` をLLM呼び出しに渡していないため計測が
  効かない既知の制約がある。本番既定値が `prebuilt` のため対応せず、
  `SKILL.md` に明記するのみとした。
- `EvalCase`/`Expect`（`evals/case_schema.py`）は変更していない。計測は
  結果側の追加情報であり、ケースの合否判定ルールに数値しきい値フィールドを
  増やす必要はない（しきい値の算出自体が本タスクの目的のため）。

## 残タスクチェックリスト（未実施・要検証）

- [ ] `python -m evals.run_case evals/cases/config_timeouts/001_llm_heavy_investigation.yaml`
      相当を単体実行し、`turn_timings` が結果JSONに正しく含まれることを確認。
      まず軽量な `003_long_generation_response.yaml` で疎通確認してから
      重量級ケースを回すことを推奨。
- [ ] `python evals/run_all.py config_timeouts` を実行し、3ケースとも `error`
      なく完走することを確認。
- [ ] `python evals/analyze_timing.py config_timeouts` を実行し、推奨値
      テーブルが妥当な数値で出力されることを確認。
- [ ] `.claude/skills/tune-config-timeouts/SKILL.md` の手順に従い、一連の
      ループ（実行→分析→必要ならconfig.ini更新→再検証）を通せることを確認。
- [ ] 既存の `evals/run_all.py system_prompt` 等、他ターゲットの実行に
      副作用がないことを確認（`run_case.py` の変更が他targetの結果を変えないこと）。

上記チェックリストは、実装作業を行ったのと同一セッション内で検証を継続する
場合は本ファイルへの追記は不要（会話内のTodoWriteで追跡する）。別セッションで
検証を再開する場合はこのチェックリストを起点にすること。
