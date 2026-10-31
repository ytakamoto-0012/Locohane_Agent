# 引継ぎプロンプト（2026-07-20時点、tune-prompt iter27完了後）

次回このタスクを再開するときは、以下をそのままClaudeCodeへの最初の指示として使う。

---

`Locohane` の安定性向上を引き続き進めてほしい。前回セッション
（iter27）で、iter26のフル回帰で見つかった3つの根本原因（dispatch_agent
打ち切り時の情報損失・thinking_loopリトライ未統合・pathメモリーレジストリ
のrace condition）と停止ボタンのコード対応を実施済み（コミット前）。
今回はユーザーが実運用で見つけた課題を `memo.md` にまとめてもらったので、
そこに記載された項目から優先度の高いものに着手したい。

## 前提（読む前に必ず把握しておくこと）

- 直前のセッション（iter27）の内容は `evals/tuning_log.md` の
  「## iter27: 4課題の根本原因調査・修正（重複検知過剰ブロック・
  リトライ統合・pathレジストリrace condition・停止ボタン）」節に詳しく
  記録済み。作業前に必ず読むこと。
- `c:\DT_Python\Locohane\memo.md` にユーザーが実運用テストで
  見つけた課題がまとめられている。**これが今回の主な作業対象**。
  下記「次に着手する作業」に内容を転記済みだが、ニュアンスや優先度は
  `memo.md` の原文も直接確認すること。
- 計画ファイル `C:\DT_Python\claudecode\.claude\plans\iridescent-pondering-graham.md`
  は前回の4課題（重複検知・リトライ統合・pathレジストリ・停止ボタン）
  設計で使い切った状態（次回は新しい計画を作成する）。参考にする場合の
  み読めばよい。
- memory `project_evals_production_parity.md`・`project_path_memory_feature.md`・
  `qwen36_recommended_params.md` も背景把握に有用。

## 前回確定した重要事項（変更しないこと）

- **モデルパラメータ（`config.ini [llm]`）は確定済み**: temperature=0.6,
  top_p=0.95, top_k=20, repeat_penalty=1.0, dry_multiplier=未指定
  （Unsloth/Qwen3.6公式準拠）。iter22で実測確認済みのため、使用モデルが
  変わらない限り再チューニング対象にしないこと。
- **`thinking_loop_guard`はmatch_ratio方式（iter25で確立）のまま**。
  `config.ini [thinking_loop_guard]`の`max_history_chars=4000`・
  `match_ratio_threshold=0.2`。単純な閾値調整だけで済ませようとせず、
  変更する場合は`evals/tuning_log.md` iter25の経緯（固定長ウィンドウの
  周期依存性問題）を踏まえること。
- **`src/graph.py`の`ainvoke_ensuring_final_text`はiter27で単一forループへ
  統合済み**（ループ検知・無言終了の両リトライが同じ予算
  `total_budget = max_retries + loop_max_retries`を共有し、どちらの
  フェーズで`ThinkingLoopDetected`が発生しても正しくリトライされる）。
  旧実装（whileループとforループが分離）に戻さないこと。
  **ただし`memo.md`にある「ループ検知はできているがリトライまでいかず
  フリーズする」という本番環境（Chainlit UI経由）での報告は、evalハーネス
  （`evals/run_case.py`が`ainvoke_ensuring_final_text`を直接呼ぶ経路）とは
  別に、`app.py`の`on_message`内にある類似だが独立した統合ループ
  （astream_events を使うため`ainvoke_ensuring_final_text`をそのまま
  流用できず、Plan調査時点で存在が確認されていた別実装）に、今回は
  手を入れていない。次回はここを最優先で調査すること。**
- **`_record_and_check_duplicate`（file-tools/view_imageの重複呼び出し
  検知）はiter27では変更していない**。iter27で判明した「サブエージェント
  打ち切り時の情報損失」を根本対策（`_collect_tool_results_summary`拡張・
  `max_iterations`緩和）したことで再訪問自体が減り、重複検知に起因する
  実害は021の検証で解消したが、検知の仕組み自体（会話全体で一度見た
  ファイルは二度と見せない）は変更していない。
- `skills/path-memory/scripts/_registry.py`の`register()`はiter27で
  `msvcrt`ファイルロックを追加済み（並列`glob_file.py`呼び出し時の
  race conditionでの登録消失を解消、15プロセス並列テストで検証済み）。
- `config.ini [subagent] max_iterations`は10→50に緩和済み（iter27）。
- `src/subagent.py`の`_collect_tool_results_summary`は、`ToolMessage`
  だけでなく直後の`AIMessage`（画像の説明等）も1回だけ併記するよう
  拡張済み（iter27）。
- `system_prompt/system_prompt.md`に、`dispatch_agent`結果に含まれる
  ファイルパスは`glob_file.py`で`@N`を取り直してから使うよう促す一文を
  追記済み（iter27）。
- `app.py`に`@cl.on_stop`を実装し、停止ボタン押下時に
  `aclose_active_llm_clients()`（`src/llm.py`、生成済み
  `httpx.AsyncClient`を`weakref.WeakSet`で登録し一括強制クローズ）を
  呼んだ後、`_rebuild_graph()`でグラフを再構築するようにした（iter27、
  **ユーザーの実機確認で問題解決を確認済み**）。

## 次に着手する作業（優先順、`memo.md` 原文も参照）

`memo.md`にユーザーが実運用テストで見つけた課題がまとめられている。
優先度は次回セッション開始時にユーザーと相談して決めること（下記は
memo.mdの記載順）。

1. **exploreサブエージェントの使用を必須化**: 現在のsystem_prompt.mdの
   記述は「使えるなら使う」という程度の弱い表現になっており、低パラメータ
   モデルは結局ほとんど使わない。大量ファイルの有無に関わらず、
   もっとシンプルに「必ず使う」という強い指示に書き換える。
2. **実行計画の提示・承認をLLM任せにしない**: 現行の「複数の手順、特に
   複数回のrun_script/execute_python_code実行を伴うタスクに着手する前は、
   以下の流れに従うこと」という記述はLLMの判断に委ねる書き方になっており
   徹底されない。より強制力のある記述に見直す。
3. **実行計画が否認された場合の処理見直し**: 現状、計画を否認しても
   LLMが計画を若干変更して勝手に進めてしまうことがある。否認時は
   （ClaudeCode同様）応答も含めて処理を完全に終了し、次のチャット指示
   待ちの状態にする、という仕様に見直す必要がある。`src/tools.py`の
   `approve_plan`まわり、`system_prompt.md`の該当記述の両方を確認する。
4. **ask_user_textをask_user_choiceより優先させない**: ユーザーへの
   自由記述質問（`ask_user_text`）は最終手段とし、可能な限り選択式
   （`ask_user_choice`）を使うよう促す記述をsystem_prompt.mdに強化する。
5. **パスメモリー（`@N`）機能の使用を必須化**: 現状は「使える」という
   程度の扱いになっており、使わないケースが多すぎる。iter27で
   dispatch_agent結果のパス取り扱いは追記したが、それ以外の一般的な
   ファイルパス操作全般について、必須化する記述への見直しが必要。
6. **Core Missionセクションの消失を確認・復元**: `memo.md`に記載された
   以下のブロック（Qwen3.6向けに具体的な基本動作フローを記述したもの）が
   現在のsystem_prompt.mdに存在しない。過去のセッションで意図せず削除
   された可能性が高い。`git log -p -- system_prompt/system_prompt.md`等で
   削除された経緯を確認した上で、復元するか、現行の記述と統合するか判断
   すること。

   ```
   # Core Mission（中核使命）

   ユーザーからタスクの要求（対象データ、チェック内容、文書データ作成・編集など）を受け取り、
   1. 要求を分析して必要なチェックを特定
   2. 利用可能なツールを組み合わせ実行計画を策定
   3. 計画をユーザーに提示して承認を得る
   4. 承認された計画に従って検図を実行
   5. レポートを生成し、エラー項目を分析して納品する

   ## 基本動作フロー

   ```
   ユーザー指示 → 現状把握 → 関連情報収集 → 実行計画作成 → ユーザー承認待ち
                                                         ↓ Yes
                                                    計画を実行 → 成果物チェック → 作業完了
                                                         ↑ NG          ↑ OK
                                                         └─────────────┘
   ```
   ```

   ※「検図」「納品」等の語はQwen3.6向け生成時の想定ドメイン（金型設計等）
   の名残の可能性があるため、そのまま復元するのではなく、現在の
   Locohaneの汎用的なタスク内容に合わせて言い回しを調整すべきか
   ユーザーに確認するとよい。

7. **本番環境（Chainlit UI）でのループ検知後フリーズの調査（最優先）**:
   `memo.md`に「ループ検知は正常にできているがリトライまでいかず
   フリーズしている。evalはうまくいっているなら、本番環境に問題ありか？」
   という報告がある。iter27で`src/graph.py`の`ainvoke_ensuring_final_text`
   （eval経路）は修正済みだが、`app.py`の`on_message`内にある別の統合
   ループ（`astream_events`を使うため`ainvoke_ensuring_final_text`を
   そのまま使えない実装）には手を入れていない。ここに同種の欠陥
   （無言終了リトライ中のthinking_loop未捕捉、または別の要因）が
   残っている可能性が高い。`app.py`の`on_message`実装を精査し、
   `graph.py`の統合ループ設計と同じ考え方で修正すること。

## 保留・対応不要と決めたこと

- `view_image`の画像リサイズ/圧縮対策: ユーザー判断で保留（継続）。
- `match_ratio_threshold=0.2`・`max_history_chars=4000`の値自体は
  合成データ・実測1例での決定であり、閾値の単純な微調整だけを目的に
  再チューニングしない（効果測定するなら必ず合成データ検証を経ること）。
- `_record_and_check_duplicate`の重複検知の仕組み自体（サブエージェントと
  メインエージェントでの状態共有）は、iter27で「再訪問自体を減らす」
  根本対策を優先したため変更していない。実害が再発するようなら次回検討。

## 未対応で持ち越しの既存課題（handoff優先度、iter26/iter27時点）

- 無言化・長考化系の失敗（`turn_cutoffs`もthinking_loopも記録されないまま
  短い応答で終わる失敗モード）: 原因未調査のまま。
- dispatch_agent使用率向上の評価ケース改善: 010番ケースは`read_skill`
  直接呼び出しでも合格する基準になっており、委譲判断の検証力が弱い。
  019番はユーザー発話がツール名を明示する疎通確認ケースで自律判断を
  測れていない。評価ケース自体の見直しは複数セッションにわたり持ち越し中。
- 014（memory_excluded_content）の除外判定: iter26で一度FAIL、iter27の
  再実行ではPASSしていたが、今回の4修正が直接影響したものではないため
  引き続き経過観察が必要。

## 作業の進め方

- llama.cpp server（`config.ini`の`[llm].base_url`、現在は
  `http://localhost:12430/v1`）が起動しているか確認してから始める。
- コード変更前は必ず対象ファイルをコピーで退避してから編集する
  （`evals/history/`配下、iter27の例:
  `evals/history/path_memory/_registry.py.before_iter27`・
  `evals/history/config_ini/iter27_before.ini`・
  `evals/history/src/subagent.py.before_iter27`・
  `evals/history/system_prompt/iter27_before.md`・
  `evals/history/src/graph.py.before_iter27`・
  `evals/history/src/llm.py.before_iter27`・
  `evals/history/app_py/app.py.before_iter27`）。
- 021のような重量級ケースは単体実行
  （`C:\DT_Python\Python311\env_local_agent_system\Scripts\python.exe
  evals/run_case.py <path>`）だと1回あたり数分かかる。効果検証は複数回
  （最低3〜5回）実行して再現性を見ること（iter27では021を5回・020を
  3回実行し4/5・完走を確認した）。
- ロジック変更（graph.py・subagent.py・_registry.py等）は、可能な限り
  モック/単体スクリプトでの検証も併用するとよい（iter27では
  `ainvoke_ensuring_final_text`の統合ループをモックgraphで、
  `_registry.py`のロックを並列サブプロセスで、それぞれ修正前後の
  挙動差を実証した）。
- 本番環境（`app.py`経由のChainlit UI）でしか再現しない不具合
  （優先度7の本番フリーズ等）は、evalハーネスだけでなく実際に
  `chainlit run app.py`で動かして確認する必要がある。
- `evals/cases/system_prompt_scale/`（実データ規模の大規模フィクスチャ版）
  はまだ未検証。021の安定率がさらに上がってから着手するのがよい。

---
