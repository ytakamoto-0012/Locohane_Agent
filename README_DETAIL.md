# Locohane-Agent 詳細ドキュメント（ローカルAIエージェント基盤 / SKILL.md 標準準拠）

> 概要・かんたんな導入手順・基本的な使い方は [README.md](README.md) を参照。
> 本書は設計・ディレクトリ構成・設定項目・運用の詳細をまとめた、管理者・開発者向けの資料。

完全オフラインで動作する AI エージェント基盤。スキル定義は Anthropic 公開の
**Agent Skills 標準（SKILL.md 形式）** に準拠する。
仕様: <https://agentskills.io/specification>

設計方針は「薄く・透明に」。賢い仕掛け（動的 import・ホットリロード・メタクラス）は使わない。
起動時に `skills/` を走査して読む、それだけ。ファイルの保存・削除のパスとタイミングが
コードから明確に追える状態を最優先する。

![Locohane](public/settings/icon.png)

名前の由来：**Lo**cal（ローカル環境）+ 小羽（**cohane** / 軽量さ・和名っぽさ）

## このプロジェクトの目的

このプロジェクトは、セルフホストによるサーバー駆動で職場やグループ単位で複数のユーザーが同時に使えて、
高性能な大規模パラメータモデルではなく、低パラメータモデルでも安定して Agent がタスクをこなせるようにすることを目指す。

想定する用途は、設計業務のようにミスが許されない専門業務である。そのため、性能の伸びよりも
結果の再現性（ばらつきの小ささ）を優先する。スキル・プロンプトの育成も、エージェントが自分の
スキルを自動で書き換える「自己進化」型は採らない。製造の量産立ち上げで同じ工程を何十〜百回と
繰り返して安定性を確かめるのと同じ考え方で、変更のたびに実際のローカルLLMで同じ評価を繰り返し
回し（**スキル安定化トライアウト**）、全回通ることを確かめてから取り込む
（「プロンプト資産の自動チューニングループ（evals/）」参照）。エージェント自身が行うのは
永続メモリーへの記録（課題と対策）と、作成者本人だけが使えるドラフトスキルの作成までで、
それを正式な手順へ反映するかどうかは、外側の評価とスキル開発者の承認を通して判断する
（「ドラフトスキル（ユーザー別）と昇格」参照）。

- **パスメモリー機能**（`src/path_memory.py`、`system_prompt.md` の
  Tool Usage Guidelines）: `Glob`/`Grep`/`Read` の結果に短い参照番号 `@N`
  を自動付与し、LLM が長い絶対パスを自分で組み立て直して失敗を繰り返すことを防ぐ。
  フォルダの `@N` の後ろに子パスを続けた `@N/photo_01.png`（`\` 区切りも可）は、そのフォルダ内のパスとして解決する
  （ファイルの `@N` に子パスを続けるとエラー）。
- **ループ検知・ガード・リトライ**（`config.ini` の `[thinking_loop_guard]`、
  `src/llm/chat_model.py` の `ChatLlamaCpp`、`src/llm/loop_guard.py` の
  `ThinkingLoopDetected`）: 応答が反復ループに
  陥ったことを検知してナッジメッセージを注入・自動リトライする。上限を超えても
  フリーズしないよう、`app.py` 側で打ち切りメッセージを出して終了する。
- **glob 機能の拡張**（`src/tools/glob_tool.py` の `Glob`、共通ロジックは
  `src/tools/_file_tools_common.py`）:
  通常の glob 結果に加え、一致件数・ディレクトリ一覧・パスメモリー参照を返し、
  LLM が探索範囲を自己修正しやすくする。
- **read-only 権限境界と explore サブエージェントへの強制委譲**（`src/tools/registry.py`
  の `_SUBAGENT_TOOLS`、`agents/explore.md`、`system_prompt.md` の
  Task Delegation）: 探索作業を読み取り専用のサブエージェントへ切り出し、
  リクエスト1回あたりのトークン使用量を分散させて安定化する。加えて
  `config.ini` の `[main_agent_tool_guard]` が、メインエージェント自身による
  `Read`/`Grep`/`analyze_image` 等トークン消費の大きい重量系ツール・スキル
  スクリプトの直接呼び出しをホワイトリストで制限し、委譲を事実上強制する。
- **メインエージェントのトークン量上限設計**（`agents/worker.md`、
  `src/main_token_guard.py`、`src/context_trim.py`、`config.ini` の
  `[graph] token_guard_*`）: メインエージェントは司令塔であり、不安定化すると
  タスク全体が破綻するため「1リクエストあたりのトークン数を目安128,000以下に
  保つ」ことを設計原則としている（当初は64,000未満を目安としていたが、以下の
  対策の改善が進み128,000まで安定して扱えるようになった）。大量ファイル処理でサブエージェントの読み取り
  結果をメインへ全文返させると、トークン量が単調増加し無応答のまま停止する
  （実測: 297枚の画像処理で1リクエストあたり24,833→128,000トークンまで増加し、
  当時のコンテキスト上限に達して停止した事例あり。目安の128,000はこの事例の
  停止点ではなく、現在の構成（`--ctx-size 256000`）で安定して扱える範囲として
  決めたもの。`[graph] token_guard_soft_threshold` は目安ではなく、それを超えて
  増え続けた場合の安全装置として、コンテキスト長から余裕を引いた値にしている）。対策として、読み取りから書き出しまでを内部で完結させ
  メインへは処理件数・失敗分のみ返す書き込み可能サブエージェント `worker` を
  用意している。`worker`が正常終了する前提の対策のため、途中でトークン上限に
  達して打ち切られた場合の保険として `write_scratch_note` があり、サブエー
  ジェントが調査の節目ごとに分かった内容をスクラッチファイルへ追記しておく
  ことで、打ち切り時も委譲元へファイルのパスだけ引き継げば続きを判断できる。
  加えて `context_trim` による切り詰めと `main_token_guard` による引継ぎ
  プロンプト自動生成で多重に補強している。
- **bash/npm 相当のツールを持たせない設計**（`src/tools/` の `run_script`
  系ツール、`create_plan`/`approve_plan`、`_GUARD_BLOCKED_CMDS`）: 低パラメータ
  モデルに任意コマンド実行を許すと暴走・誤操作のリスクが高いため、汎用シェル・
  bash・npm に相当するツールは一切公開しない。コマンド実行はスキル配下に
  事前定義された `run_script`（ホワイトリスト方式）のみに限定し、書き込み系
  ツールはすべて `create_plan`→`approve_plan` によるユーザー承認を得るまで
  実行をブロックする。任意コード実行が可能な `execute_python_code` でも
  git/npm/pip の起動とプロジェクト外への書き込みをガードで遮断し、最低限の
  破壊的操作を防いでいる。
- **文書生成スキルの「ops JSON DSL」設計**（`skills/pptx-create`・`skills/excel-edit`・
  `skills/docx-create` の `scripts/_*.py`）: 上記「bash/npm相当のツールを持たせない
  設計」を xlsx/pptx/docx 生成にも一貫して適用したもの。`python-pptx`/`openpyxl`/
  `python-docx` の生コードを LLM に書かせるのではなく、有限個の `op`（`set_range`/
  `add_chart`/`format_table` 等）と JSON スキーマへ閉じ込めることで、低パラメータ
  モデルでも構文エラーやファイル破損なく確実に動く操作だけに絞っている（Anthropic
  公式の `skills/pptx`・`skills/xlsx` 等は逆に、Claude自身の事前知識に頼って
  `python-pptx`/`openpyxl` の生コードを直接書かせる設計で、SKILL.md はライブラリ
  API そのものではなく「落とし穴」だけを補足する差分ドキュメントになっている）。
  見た目の一貫性のため、`pptx-create`/`excel-edit`/`docx-create`/`pdf-tools` は
  同じ8色の配色テーマ（`charcoal`/`navy` 等、Anthropic公式pptxスキルのDesign
  Ideas準拠）を共有している。`pdf-tools`（`create_pdf.py`）だけは例外的に
  セマンティックHTML（自前パーサーで`reportlab`のFlowableへ変換、JS実行なし）を LLM に直接書かせている —
  HTML は xlsx/pptx/docx の XMLベース形式と違い多少崩れても致命的にファイルが
  壊れないため、この原則の範囲内で安全に表現力を確保できる。
- **設定ダッシュボードによる複数インスタンス運用**（`admin/` パッケージ、
  `admin.bat`）: 「職場やグループ単位で複数のユーザーが同時に使える」を
  設定面でも支える独立した管理ツール。`config.ini`・`public/settings/`・
  ログインユーザーをテキストエディタなしでブラウザUIから変更できるのに加え、
  Locohane本体を**複数のインスタンスとして同時に起動・停止・再起動**できる
  （例: 別のモデル・別の接続先で動く2つ目のインスタンスを追加する等）。
  各インスタンスは会話履歴・永続メモリー・アップロード等のデータ、設定の
  上書き差分（`instances/<名前>/config_overrides.json`）、ログインユーザーが
  完全に分離され、多重起動によるデータ破損は排他ロック（`src/instance_lock.py`）
  で防いでいる。詳細は「設定ダッシュボード（管理ツール）」を参照。

---

## アーキテクチャ

```
                 ┌──────────────────────────────────────────────┐
   ブラウザ ───▶ │  app.py (Chainlit UI)                        │
                 │   - @cl.on_message でユーザー入力を受ける      │
                 │   - astream_events でトークンをストリーム表示  │
                 │   - ツールコールを cl.Step で可視化            │
                 │   - approve_plan の承認を                      │
                 │     cl.AskActionMessage で確認（承認/拒否）    │
                 └───────────────┬──────────────────────────────┘
                                 │ 実行
                 ┌───────────────▼──────────────────────────────┐
                 │  src/graph.py  ReAct ループ（実装2種を切替）   │
                 │                                               │
                 │  handwritten: START→[agent]⇄[tools]→END       │
                 │    （手書き StateGraph。挙動を自前配線）        │
                 │  prebuilt   : create_react_agent に委譲        │
                 │    （config.ini [graph] implementation で選択）│
                 └───┬───────────────────────────┬──────────────┘
                     │ モデル呼び出し             │ ツール実行
       ┌─────────────▼───────────┐   ┌───────────▼──────────────────────┐
       │ ChatOpenAI              │   │ src/tools/ (41ツール)               │
       │  → llama-server /v1     │   │  read_skill / read_skill_file /    │
       │  (OpenAI 互換)          │   │  run_script / execute_python_code /│
       └─────────────────────────┘   │  get_tool_source / check_work_dir_status /│
                                      │  Read / Glob / Grep / json_query / │
                                      │  search_path_memory / analyze_image /│
                                      │  dispatch_agent / create_plan /    │
                                      │  approve_plan / update_task_progress/│
                                      │  get_plan_status / lock_plan_mode /│
                                      │  AskUserQuestion / ask_user_choice /│
                                      │  provide_download /                │
                                      │  create_memory / update_memory /   │
                                      │  delete_memory / read_memory /     │
                                      │  search_memory / list_memories /   │
                                      │  list_document_collections /       │
                                      │  search_documents /                │
                                      │  search_document_paths / help      │
                                      │  （skills/ ファイル操作は配下限定、 │
                                      │   Read/Glob/Grep は任意の絶対パス） │
                                      └───────┬───────────────┬──────────┘
                                              │ 走査/読込/実行  │ 読み書き
       起動時 ┌─────────────────────────────┐  │   ┌────────────▼───────────┐
   Discovery │ src/skills.py                │◀─┘   │ src/memory.py           │
    (第1段階) │  skills/ を走査し             │      │  data/memory/ 配下に    │
             │  name+description を          │      │  User/Feedback/Project/│
             │  システムプロンプトへ注入      │      │  Reference の4種を保存 │
             └─────────────────────────────┘      │  MEMORY.md 索引を       │
                                                    │  {{memory}} へ差し込み │
   会話状態 ┌──────────────────────────────┐      └────────────────────────┘
           │ AsyncSqliteSaver              │   ┌────────────────────┐
           │  → data/checkpoints.sqlite   │   │ skills/            │
           └──────────────────────────────┘   │  <name>/SKILL.md   │
                                                │  <name>/scripts/   │
                                                │  <name>/references/│
                                                │  <name>/assets/    │
                                                └────────────────────┘
```

`dispatch_agent` のみ、`src/subagent.py` 内で独立した ReAct ループを回す特殊なツールで、
その内部のツール呼び出し（`read_skill`/`read_skill_file`/`run_script`/`execute_python_code` 等、
`agent_type` で選んだ種別が持つツールに限る）は親の会話履歴・グラフトレースには乗らない
（コンテキスト節約のため意図的な設計）。種別定義は `agents/*.md`（`name`/`description`/`tools`/`model`
の frontmatter、ClaudeCode の `.claude/agents/*.md` 相当）を起動時に走査して読み込む。

`model`（任意）には `[llm].sub_url` の接続先の `model` 名を書く。指定するとその種別のサブエージェントは、
`sub_url` のうち `model` が一致する（大文字小文字は区別しない）接続先だけを候補にして `sub_routing_strategy` に従って接続先を選ぶ
（`sub_url` 未指定でメインの接続先を継承する場合も、継承先が指定モデルでなければ指定モデルの接続先から選ぶ）。
`model: inherit`（ClaudeCode の定義ファイル由来）と空文字は指定なしとして扱う。
一致する接続先が無い（または全て `start`/`end` の時間帯外の）場合は下記 `[llm].sub_default_model`、それも使えなければ指定を無視し、通常のルーティング規則に従う。
`dispatch_agent`/`dispatch_agent_batch` の `model` 引数でも1回の委譲ごとに指定でき、frontmatter より優先される。
どちらも無い（または指定モデルが使えない）場合は `[llm].sub_default_model`（空欄なら従来どおりモデル名を見ずに選ぶ）が同じ扱いで使われる。
`sub_url` 未指定でメインの接続先を引き継ぐ設定でも `sub_default_model` は有効なため、メインが別モデルの接続先を
使っていると引き継がず、`main_url` のうち `sub_default_model` のモデルの接続先から選ぶ点に注意
（メインと同じ接続先を使わせたいなら空欄にする）。
ジョブ内の再構築・圧縮用のモデル構築にも同じ指定が適用される（`src/llm/routing.py` の `_PREFERRED_SUB_MODEL`）。

```markdown
---
name: explore
description: ...
tools: read_skill, Read, Grep
model: Qwen3.6-35B-A3B
---
```

起動時に `[paths].project_locohane_dir`（既定 `.locohane`）配下の `LOCOHANE.md`
（存在すれば）も読み込まれ、システムプロンプトの `{{project_instructions}}` へ
差し込まれる（ClaudeCode の CLAUDE.md 相当、`src/project_instructions.py` 参照）。

### progressive disclosure（3 段階）の実装場所

| 段階 | 内容 | 実装 |
|------|------|------|
| 1. Discovery | 起動時に各 `SKILL.md` の frontmatter（name/description）のみ注入 | `src/skills.py` |
| 2. Read | LLM がスキルを選び、`read_skill` で本文全体を読む | `src/tools/read_skill.py` |
| 3. Execute | 本文の指示に従い `read_skill_file`/`run_script` で必要時のみ読む・実行 | `src/tools/read_skill_file.py`/`src/tools/run_script.py` |

第2段階の `read_skill` まわりの補足:

- **スキル名の選択肢制約**: 起動時に `init_tools(skill_names=...)` が `read_skill` の `skill_name` 引数を
  実在スキル名の `Literal` 型へ差し替える（`apply_skill_name_enum`）。LLM へ送る tools スキーマに
  `"enum": [...]` が載り、一覧外の名前はツール本体に届く前に検証エラーとして LLM へ返る。
- **返す内容**: frontmatter を除いた本文を `<skill_content name="...">` で囲み、`references/` があれば
  ファイル一覧を `@N`（パスメモリー）付きで末尾に添える（中身は読まない）。`read_skill_file` は
  この `@N` をそのまま受け付ける（skills ルート外を指す `@N` は拒否）。フォルダの `@N` に子パスを続けた
  `@N/xxx.md` も受け付ける。
- **コンテキスト削減からの保護**: スキル名ごとに最後に読んだ `read_skill` 結果は `[context_trim]` で
  切り詰めない（`src/context_trim.py`）。`[context_compaction]` で要約対象に入った場合も要約させず、
  要約の後ろへ原文のまま再添付する（`src/context_compaction.py` の `_render_reattached_skills`、
  上限は `skill_reattach_max_chars_per_skill`/`skill_reattach_total_max_chars`）。

スキル読み込みは **すべて LangGraph のツールコール** として実装しており、グラフのトレースに乗り
Chainlit 側で「今このスキルを読んでいます」等のステップとして可視化される。

### ビルトインツール一覧とLLMへの公開方法

「ビルトインツール」とは、スキル本文とは独立して常時 LLM に公開される、状態を持たない
関数群を指す（対して SKILL.md は「知識・手順書」であり、それ自体はツールではない。
LLM は `read_skill`/`read_skill_file`/`run_script` という**ビルトインツール**を経由して
間接的にスキル本文やスクリプトへアクセスする）。

**LLM への認識のさせ方**: system_prompt.md 等のテキストへツールの説明を埋め込む
方式（プロンプトベース）では**ない**。OpenAI 互換 API の `tools` パラメータとして
リクエストのたびに構造化データで送信する方式を取る。

1. 各ツールは `src/tools/` パッケージ配下（ツール1つにつき原則1ファイル）で LangChain の
   `@tool` デコレータ付き関数として定義する。関数の docstring が `description`、
   型ヒント付き引数が JSON Schema の `parameters` に自動変換される
   （`langchain_core.tools.tool` の標準機能。本プロジェクト側に独自の変換コードはない）。
2. `get_all_tools()`（`src/tools/registry.py`）がビルトインツールと MCP 由来の動的ツールを
   合流させたリストを返す。
3. `src/graph.py` の `build_model(config).bind_tools(get_all_tools())`（サブエージェント側は
   `src/subagent.py`）が、このリストを `ChatOpenAI` 系クラス（`src/llm/chat_model.py` の
   `ChatLlamaCpp`）に紐付ける。
4. 実際の HTTP リクエスト送信時、`bind_tools` で渡した関数群が OpenAI Function Calling
   形式の `tools` 配列へ変換され、`base_url`（llama-server の `/v1` エンドポイント）
   宛のリクエストボディに含まれる。LLM 側の応答に含まれる `tool_calls` を
   LangGraph が受け取り、対応する Python 関数を実行する。

つまり LLM が「どんなツールが使えるか」を知る手がかりは、システムプロンプトの文章では
なく、**リクエストごとに送られる `tools` フィールドそのもの**である
（`name`/`description`/`parameters` 込みで、モデルの Function Calling 機能が解釈する）。
スキルの frontmatter（name/description）だけはこれとは別に、`src/skills.py` が
起動時にシステムプロンプトへテキスト注入する（Discovery 段階、こちらはプロンプトベース）。

上記3段階に加えて、スキルの読み込みとは独立したツールが以下の41個ある（いずれも `src/tools/` パッケージ配下、ツール1つにつき原則1ファイル）。

| ツール | 役割 |
|--------|------|
| `read_skill` | スキルの SKILL.md 本文全体を読み込む（progressive disclosure 第2段階） |
| `read_skill_file` | skills ディレクトリ配下のファイルを読み込む（references/assets 等。progressive disclosure 第3段階） |
| `run_script` | スキルの scripts/ 配下のスクリプトを実行する（要承認。`config.ini` で承認不要に切替可）。完了までブロックするため、タイムアウトに近い長時間実行が見込まれる場合は `run_script_background` を使う |
| `run_script_background` | `run_script` と同じスクリプトを起動する（要承認は同様）。完了までの間、進捗（経過秒数・途中出力）をチャットへ直接通知しながら待つため、LLM自身がポーリングする必要は無い。設定した安全上限（`[scripts].background_inline_wait_max_seconds`）を超えてもなお完了しない場合のみ `job_id` を返してターンを終える |
| `check_script_job` / `stop_script_job` | 上記の安全上限超過フォールバック時のみ使う、ジョブの状況確認・強制終了。実行中ジョブへの連続 `check_script_job` 呼び出しは `[scripts].background_min_poll_interval_seconds` 未満の間隔だとサーバー側で拒否される |
| `execute_python_code` | LLMが生成したPythonコードをその場で実行（要承認。`config.ini` で無効化可）。完了までブロックするため、タイムアウトに近い長時間実行が見込まれる場合は `execute_python_code_background` を使う。code内で `@N`（パスメモリ）を参照する場合は `AGENT_SRC_DIR` 環境変数経由で `path_memory.resolve()` を呼んで実パスへ展開する必要がある。実行前ガードにより `src/`・`app.py`・`config.ini`・`skills/` 等プロジェクトフォルダ配下への書き込み・削除・改名はブロックされる。書き込み先は、ユーザーが作業フォルダ（後述「作業ディレクトリの切り替え」参照）を指定していればそのフォルダ、未指定なら `default_workdir` 配下でも自セッション専用の一時領域 `_tmp_<name>` に限られる（`default_workdir` 直下への書き込みは、別セッションの誤動作を防ぐため許可しない） |
| `execute_python_code_background` | `execute_python_code` と同じコードを起動する（要承認・`config.ini` での無効化・パスメモリ展開・プロジェクトフォルダ保護ガードは同様）。`run_script_background` と同じく完了まで進捗通知しながら待ち、状況確認・停止（安全上限超過時のみ）は共通の `check_script_job`/`stop_script_job` を使う |
| `execute_python_code_readonly` | `execute_python_code` の読み取り専用版（ファイル書き込み不可、承認不要）。`explore` 等の読み取り専用サブエージェント種別（`agents/*.md` の `tools:`）にのみ付与され、メインエージェントは持たない |
| `write_scratch_note` | 調査中に分かった内容をスクラッチファイルへ追記する。計画未承認でも常に呼べ、書き込み先はツール自身が決めるため任意パスには書けない。トークン上限打ち切り時の引き継ぎ用途 |
| `write_thread_note` / `list_thread_notes` / `read_thread_note` | 同一スレッド内に閉じたスレッド共有ノート（後述「スレッド共有ノートとの違い」参照）。委譲元・委譲先間で調査結果の詳細を引き継ぐための、`write_scratch_note`とは別系統のメモ機構。主エージェント・全サブエージェントが読み書き可能 |
| `get_tool_source` | `run_script` がエラーになった際、原因調査用にスクリプトの絶対パスを返す（中身は返さない） |
| `check_work_dir_status` | 現在の作業ディレクトリの実際のアクセス状況を確認する |
| `Read` / `Glob` / `Grep` | ローカルファイルシステム上の任意の絶対パスに対する読込・ファイル名検索・全文検索（ClaudeCode の同名ツールに合わせた名前。読み取り専用のため計画未承認でも常に呼べる。ロジックは `src/tools/read_tool.py`/`glob_tool.py`/`grep_tool.py`、共通処理は `_file_tools_common.py`） |
| `json_query` | JSON/dict に対する JMESPath クエリ（読み取り専用） |
| `search_path_memory` | 現在の会話のパスメモリー（`@N`）から、ファイル名等に似たパスを類似検索する（文字bigram＋コサイン類似度、読み取り専用）。パスを受け取る各ツールは `@N` 単体に加え、フォルダの `@N` に子パスを続けた `@N/ファイル名` も解決する |
| `provide_download` | 既存のファイルをチャット画面にダウンロードボタンとして提示する |
| `analyze_image` | 画像ファイルをLLMへ視覚情報として見せ、LLM自身が内容を解析・説明・判断する（Vision対応モデル向け）。`show_in_chat=True` を指定すると、解析と同時にチャット画面へもプレビュー表示する（「表示して」「見せて」にはこちらを使う。表示だけして中身を見ない、という呼び方はできない）。回答本文（Markdownテーブルのセル等）の中に画像を組み込みたい場合は、ツールを使わず回答テキストへ直接 `![説明](絶対パス)` と書けばよい（送信直前に自動でブラウザから取得可能なURLへ変換される。`app.py` の `_embed_local_images_as_session_urls`） |
| `dispatch_agent` | タスクをサブエージェント（`src/subagent.py`）へ委譲し最終回答のみ受け取る。`agent_type` 引数でサブエージェントの種別を必ず指定する（暗黙の既定値は無い）。種別定義は `agents/*.md`（ClaudeCode の `.claude/agents/*.md` 相当）。`.locohane/agents/*.md` ともマージ走査され、同名は `.locohane/agents` 側が優先される。任意の `orchestrator_skill` 引数（複数SKILLを横断的に使うオーケストレーター役SKILL.mdのスキル名）を指定すると、そのSKILL.md本文全体がtaskの先頭へ機械的に注入され、必須ルール・禁止事項の要約時の脱落を防ぐ（詳細は `skills/SKILLS_README.md` 7節）。任意の `model` 引数で `[llm].sub_url` のモデル名を指定でき、`agents/*.md` の frontmatter `model` より優先される（存在しないモデル名は無視）。完了までの間、進捗（経過時間・反復回数）をチャットへ直接通知しながら待つため、LLM自身がポーリングする必要は無い。設定した安全上限（`[subagent].background_inline_wait_max_seconds`）を超えてもなお完了しない場合のみ `job_id` を返してターンを終える |
| `dispatch_agent_batch` | フォルダ内の多数のファイルへ同じ処理を行う作業を、複数のサブエージェントへ並列に委譲する。`pattern`（`path`からの相対。`/`を含まなければ直下のみ、`*/ocr_md/*.md`・`**/*.jpg`のように階層を含めればサブフォルダも探索。`{a,b}`展開可。`..`等で`path`の外に出るファイルは対象外）に一致するファイルを`path`からの相対パス順に取得し、`group_size`件ずつに分けて各グループの task 末尾へ担当ファイルの絶対パスを機械的に付け足し、`dispatch_agent` と同じジョブ基盤（`[subagent].max_parallel` のセマフォ・進捗通知・停止ボタン対応）で並列実行する。`pattern` の代わりに `files`（`@N`・絶対パスのリスト。前段の explore が検索で見つけた、複数フォルダに散らばるファイル）を渡すこともでき、その場合は渡された順に分け、各グループには「担当ファイルだけを開き、確認した事実を出典付きで返す」指示を付ける（情報探索の2段階目。`[subagent].lookup_files_per_agent` 参照）。LLMは1回呼ぶだけでよく、1応答で複数の `dispatch_agent` を並べる必要が無い（低パラメータモデルでは並列発行が安定しないため）。戻り値はグループごとの状態と最終回答の先頭400文字（`files` 指定時は1500文字）のみで、全文は thread note に保存する。「エラー」「打ち切り」「強制終了」「未実行」のグループには再委任用に担当ファイルの一覧も載せる。進捗通知はグループごとではなく全グループを集約した1件のみ。1回あたりのグループ数は `[subagent].batch_max_groups` まで（既定50、0以下で無制限。超える場合は起動前にエラー）。安全上限（`background_inline_wait_max_seconds`）超過時は完了済みグループの結果と実行中グループの `job_id` を返す（実行中グループのStepは「バックグラウンド」バッジで閉じる）。LLMのタイムアウト・通信エラーで打ち切られたグループが `[subagent].batch_stop_after_unreachable_groups` 件に達すると、残りのグループは起動せず「未実行」として返す。停止ボタンで中断した場合は、完了済みグループの結果と未完了グループの担当ファイルを thread note に保存し、次のターンの案内にその topic を載せる |
| `check_dispatch_agent_job` / `stop_dispatch_agent_job` | 上記の安全上限超過フォールバック時のみ使う、ジョブの状況確認・強制終了 |
| `create_plan` / `approve_plan` / `update_task_progress` | 複数ステップの実行計画を作成・承認・進捗更新（承認後は`run_script`の個別確認をスキップ）。各ステップは `content`（内容）と `activeForm`（実行中表示用の現在進行形）を持つ |
| `get_plan_status` / `lock_plan_mode` | 現在 Plan Mode（書き込み系ツールがブロックされたロック状態）か Edit Automatically（承認済み計画を実行できる状態）かを確認し、後者から前者へユーザー承認なしに手動で戻す |
| `AskUserQuestion` / `ask_user_choice` | 会話継続に必要な追加情報をユーザーへ質問（`AskUserQuestion` は自由記述。`labels` 省略時は単一入力、指定時は複数項目をまとめて提示。`ask_user_choice` は選択肢形式で、表示される選択肢には常に「✏️ その他（自由入力）」「❌ キャンセル」が自動で追加される） |
| `create_memory` / `update_memory` / `delete_memory` / `read_memory` / `search_memory` / `list_memories` | スレッドをまたぐ永続メモリー（`src/memory.py`）の保存・更新・削除・全文読込・検索・一覧。主エージェントは全6ツールを持つ。`dispatch_agent` のサブエージェントには種別ごとに絞って委譲し、`explore` は読み込み系（`read_memory`/`search_memory`/`list_memories`）のみ、`worker` は全6ツール（フルアクセス）を持つ（`agents/*.md` の `tools:` 参照） |
| `list_document_collections` / `search_documents` / `search_document_paths` | ベクターDB（`src/vector_db/`）に取り込んだ社内文書のコレクション一覧と意味検索（読み取り専用。`search_document_paths` はファイル名・フォルダ名で探す）。検索結果には出典のパス（`@N`）・ページ等を添え、全体を `[vector_db] max_result_chars` で切り詰める。`[vector_db] enabled = false` の間は「無効です」を返す（「ベクターDB（社内文書の意味検索）」参照） |
| `help` | ユーザー向けヘルプ本文（`system_prompt/help.md`）をそのまま返す |

`run_script`（`run_script_background` 含む）と `execute_python_code`（`execute_python_code_background`
含む）は書き込み系ツールのため、`create_plan`/`approve_plan` で計画がユーザー承認済み
（`cl.user_session["plan_approved"]` が True）でない限り実行できず、
未承認の場合はエラーを返す（Plan Mode）。承認自体は `cl.AskActionMessage` による
✅承認/🚫拒否ボタンで行い、タイムアウト時は安全側（拒否）に倒す。`execute_python_code`
（`execute_python_code_background` 含む）は `config.ini` の `[scripts] code_execution_enabled`
でツール自体を無効化できる。

上記の41ツール・スキルすべてがメインエージェントから直接呼べるわけではない。
`config.ini` の `[main_agent_tool_guard]`（既定 `mode = all`）が、メインエージェント
自身によるビルトインツール・`run_script` 配下スキルスクリプトの直接呼び出しを
`allow_entries` のホワイトリストで制限する（`dispatch_agent` 配下のサブエージェントは
対象外）。`Read`/`Grep`/`json_query`/`analyze_image`/`execute_python_code` 系や
大半のスキルスクリプトはこのリストに含めておらず、未登録＝呼び出し不可という
形で事実上 `dispatch_agent` への委譲を強制する（トークン消費の大きい重量系ツールの
連打によるトークン上限到達を防ぐ狙い。詳細は `config.ini` 内コメント参照）。

---

## ファイル構成

```
Locohane/
├── config.ini              # 全設定（LLM接続・保存先パス・スクリプト実行・グラフ実装・サブエージェント）
├── requirements.txt        # pip 依存（バージョン固定）
├── app.py                  # Chainlit エントリ
├── app.bat                 # Windows用起動バッチ（instances/default/のホスト/ポートに追従）
├── admin.bat               # 設定ダッシュボード（管理ツール）起動バッチ
├── python_env.bat.example  # Python 環境の指定の例（python_env.bat にコピーして PYTHON_DIR を直す。python_env.bat は git 管理外）
├── admin/                  # 設定ダッシュボード本体（詳細は「設定ダッシュボード」節）
│   ├── server.py            # FastAPIエントリー（`python -m admin.server`）
│   ├── auth.py               # 管理ツール自身のログイン・セッション・CSRF
│   ├── instances.py          # instances/ の作成・削除・複製・整合性チェック
│   ├── supervisor.py         # インスタンスごとの子プロセス起動・停止・再起動
│   ├── ini_catalog.py        # config.ini の読み取り専用パーサー（書き換えない）
│   ├── ini_catalog_en.py     # config.ini の説明の英訳カタログ（ダッシュボードの英語表示用）
│   ├── overrides.py          # config_overrides.json の検証・保存・バックアップ・監査記録
│   ├── env_files.py          # instances/<name>/.env の読み書き
│   ├── env_overrides.py      # 環境変数で上書き中のキーの検出
│   ├── settings_files.py     # 表示設定（public/settings/・instances/<name>/settings/）の読み書き
│   ├── audit.py              # 変更履歴ログ（JSON Lines）
│   ├── monitor.py            # モニター画面の読み取り処理（稼働状況・会話・トークン推移・ログ・LLM接続先）
│   ├── knowledge.py          # ナレッジ画面の HTTP API（ベクターDB のコレクション管理・取り込みジョブ）
│   ├── memories.py           # メモリー画面の HTTP API（インスタンスの永続メモリーの閲覧・編集）
│   ├── assistant.py          # 管理アシスタント（右上の「AIに相談」。設定を読んで助言するだけ）の HTTP API
│   ├── assistant_index.py    # 管理アシスタントの検索（config.ini・README_DETAIL.md・API リファレンスの簡易 RAG）
│   ├── i18n.py               # サーバー側の表示言語（エラーメッセージの英訳・X-Locohane-Lang を読むミドルウェア）
│   ├── ASSISTANT_GUIDE.md    # 管理アシスタントが毎回読み直す画面ガイド（編集すれば再起動不要で反映）
│   ├── skill_lab/            # スキル研究室（「設定ダッシュボード」の「スキル研究室」節）
│   │   ├── api.py             # 研究室の HTTP API（/api/instances/{name}/skill-lab/...）
│   │   ├── themes.py          # 研究テーマの置き場・ファイル操作・状態（対象インスタンスの data/<名前>/skill_lab/themes/）
│   │   ├── sources.py         # 取り込める利用者のドラフト・正式スキル・正式エージェントの一覧
│   │   ├── evaluation.py      # テーマの評価（evals/run_all.py に資産を重ね、研究室の LLM 接続先で実行）
│   │   ├── llm.py / fixer.py / judge.py / drafting.py  # 研究室の LLM による AI 修正・AI judge・下書き
│   │   ├── worker.py          # スキル調整ワーカーとして起動されるワーカー（評価 → AI 修正 → 再評価 → トライアウト）
│   │   └── promotion.py       # 昇格（インスタンス専用の置き場へ）・差し戻し・ドラフトのアーカイブ
│   └── static/                # フロントエンド（ビルド不要の素のHTML/JS。研究室の画面は skill_lab.js、ナレッジ画面は knowledge.js、表示言語は i18n.js・英訳は i18n_en.js）
├── instances/               # 管理ツールが管理するインスタンス別ディレクトリ（.gitignore済み、実行時生成）
│   └── <name>/               # インスタンス別の設定フォルダ。既定ではインスタンス専用の拡張ディレクトリ（[skill_lab] instance_locohane_dir。研究室で昇格した skills/・agents/・回帰テスト用の evals/）も兼ねる
├── chainlit.md             # Chainlit ウェルカム画面
├── README.en.md            # README.md の英語版
├── README_DETAIL.en.md     # README_DETAIL.md（本書）の英語版
├── CLAUDE.md               # プロジェクト固有の追加指示（Claude Code 形式）
├── QWEN.md                 # プロジェクト固有の追加指示（Qwen Code 形式、内容はCLAUDE.md参照の1行）
├── VERSION                 # バージョン番号の正本（1行。「バージョン管理とリリース手順」節）
├── CHANGELOG.md            # 変更履歴（Keep a Changelog 形式）
├── LICENSE                 # 本プロジェクトのライセンス（MIT）
├── THIRD_PARTY_LICENSES.md # 依存OSSライセンス一覧（tools/gen_licenses.pyで再生成）
├── memo.md                 # 開発者向けメモ
├── issue.md                # 既知の課題メモ（手動管理）
├── issue/                  # monitor-app-log スキルが app_*.log の異常検知時に自動起票するケース別Markdown
├── pytest.ini              # pytest設定
├── .env.example            # 環境変数サンプル
├── .gitignore              # Git除外設定
├── .mcp.json.example        # 独自スキル群をMCPサーバーとして配布する`mcp_server/`への接続設定の例（Claude Code等のMCPクライアント向け、project scope。`.mcp.json` にコピーしてパスを直す。`.mcp.json` はgit管理外）
├── .chainlit/
│   ├── config.toml         # Chainlit設定
│   └── translations/       # 多言語翻訳ファイル
├── .claude/
│   └── skills/             # Claude Code用スキル（開発支援。アプリ実行時には使わない）
│       ├── tune-prompt/           # system_prompt.md等のプロンプト資産自動チューニング
│       ├── create-eval-case/      # evals/cases/ へのevalケース新規作成
│       ├── consolidate-memory/    # 全インスタンスの永続メモリーの重複統合（日次）
│       ├── apply-memory-to-skills/ # 指定インスタンスのメモリーをSKILL.mdへ反映しtune-prompt実行
│       ├── promote-skill/         # ドラフトスキル1件をスキル安定化トライアウトに通してインスタンス専用の置き場へ昇格（通常は管理ツールのスキル研究室を使う）
│       └── monitor-app-log/       # app_*.log を定期監視し issue/ へ自動起票
├── .qwen/                  # Qwen Code用の `.claude/` 相当ディレクトリ（settings.json・skills/等）
├── .locohane/                # project_locohane_dir（既定）。配下を起動時に自動検知
│   ├── LOCOHANE.md.example  # LOCOHANE.md のサンプル（配置するとプロジェクト固有指示になる）
│   ├── settings.json        # MCPサーバー接続設定
│   ├── skills/              # skills_dir にマージ走査される追加スキル置き場（同名は優先。現状は README.md のみで中身は空）
│   └── agents/              # agents_dir にマージ走査される追加エージェント種別置き場（同名は優先。現状は README.txt のみで中身は空）
├── frontend/                # カスタムReactフロントエンド
│   ├── src/
│   │   ├── components/      # UIコンポーネント（AskFormBar, PlanCard, SidePanel等）
│   │   ├── i18n/            # 表示言語（日本語/英語の文言・LanguageProvider・useI18n）
│   │   └── utils/           # ユーティリティ
│   └── public/
├── public/                  # Chainlit公開ファイル
│   ├── build/               # フロントエンドビルド成果物
│   ├── settings/            # ヘッダー・アイコン・ウェルカム文言の設定
│   ├── icons/                # UIアイコン画像
│   ├── custom.css           # カスタムCSS
│   └── UI変更ガイド.md      # UIカスタマイズガイド
├── system_prompt/
│   ├── system_prompt.md     # メインエージェント用システムプロンプト
│   ├── help.md              # help ツールが返すユーザー向けヘルプ（英語。LLM が利用者の言語で伝える）
│   ├── compaction_prompt.md # 会話履歴の自動要約指示
│   ├── subagent_common.md   # サブエージェント共通プロンプト
│   ├── handoff_prompt.md    # トークン上限接近時にLLMへ注入する打ち切り・引き継ぎ指示
│   └── compressing_test/    # プロンプト圧縮の効果検証用ワークスペース（開発時の実験、tune-prompt関連）
├── tools/
│   └── gen_licenses.py      # THIRD_PARTY_LICENSES.md 再生成スクリプト
├── mcp_server/               # skills/ をMCPサーバーとして外部（Claude Code等）へ配布する側の実装（`.mcp.json`から起動。詳細は`mcp_server/MCP_README.md`）
│   ├── MCP_README.md         # 配布方式・クライアント接続設定の詳細ガイド
│   ├── server.py              # エントリーポイント（FastMCPインスタンス生成、Resources/Tools登録）
│   ├── config.py              # 設定値の集約（SKILLS_SRC/WORKDIR/タイムアウト秒数/除外ファイル名。環境変数で上書き可）
│   ├── publish.py              # Resources配布用の一時コピー作成（.env等の機密ファイルを除外）
│   └── skill_tools.py          # Tools方式の3ツール本体（list_skills/read_skill/run_skill_script）
├── evals/                   # プロンプト資産の自動評価・チューニングループ
│   ├── README.md            # 実行方法・ケースの書き方
│   ├── run_all.py           # 全ケース一括実行
│   ├── run_case.py          # 1ケース実行（Chainlit UI不要）
│   ├── case_schema.py       # ケースYAMLのスキーマ
│   ├── headless_chainlit.py # Chainlitスタブ
│   ├── timing_callbacks.py  # config_timeouts用の実測タイミング計測コールバック
│   ├── analyze_timing.py    # 実測タイミング結果の集計・分析
│   ├── analyze_investigation_order.py # チューニング検証用の一時的な集計スクリプト
│   ├── tuning_log.md        # チューニング履歴
│   ├── handoff_prompt.md    # tune-prompt再開時の引き継ぎメモ
│   ├── handoff_config_timeouts_skill.md # config_timeoutsターゲット開発時の引き継ぎメモ
│   ├── cases/               # 評価ケース（YAML）
│   │   ├── system_prompt/   # システムプロンプト用ケース
│   │   ├── system_prompt_scale/ # スケーリング用ケース
│   │   ├── config_timeouts/ # timeout系設定チューニング用ケース
│   │   └── excel-skills/    # Excel系スキル用ケース
│   ├── fixtures/            # 評価用フィクスチャデータ（work_dir に指定するデータ一式）
│   │   └── answer/<target>/ # cases/<target> 用の正解データ・フィクスチャ生成スクリプト
│   ├── history/             # チューニング前スナップショット
│   └── results/             # 実行結果（再生成可能、.gitignore済み）
├── src/
│   ├── config.py            # config.ini ローダー（frozen dataclass）
│   ├── version.py           # VERSION ファイルからバージョン番号を読む（__version__）
│   ├── skills.py            # スキル発見ミドルウェア（第1段階 Discovery）
│   ├── agent_types.py       # エージェント種別発見（agents/*.md）
│   ├── tools/                # LangGraph ツール39種（第2・第3段階＋独立ツール）。ツール1つにつき原則1ファイル
│   │   ├── registry.py       # get_all_tools()・MCPツール登録・_BASE_TOOLS/_SUBAGENT_TOOLS一覧
│   │   ├── tool_node.py      # ImageAwareToolNode・メインエージェント向けツールのフィルタリング
│   │   ├── _state.py         # モジュール共有状態（config値・セマフォ・レジストリ等）の一元管理
│   │   ├── _file_tools_common.py # Read/Glob/Grep の共通ロジック
│   │   └── （read_skill.py/run_script.py/dispatch_agent.py 等、ツールごとに1ファイル）
│   ├── path_memory.py       # パスメモリー（@N）レジストリの読み書き
│   ├── memory.py            # 永続メモリーの読み書き・索引再構築
│   ├── vector_db/            # ベクターDB（社内文書の意味検索。「ベクターDB（社内文書の意味検索）」節）
│   │   ├── extract.py / chunking.py # 文書からのテキスト抽出・チャンク分割
│   │   ├── render.py         # image_input 時のページ・画像ファイルの画像化（Office は COM で PDF 経由）
│   │   ├── embeddings.py     # /v1/embeddings による埋め込み計算
│   │   ├── catalog.py        # コレクション定義・取り込み済みファイルの記録（catalog.sqlite）
│   │   ├── store.py / sqlite_vec_store.py / qdrant_store.py # ベクターの保存先（sqlite-vec / Qdrant）
│   │   ├── keyword_index.py  # 本文のキーワード索引（keywords/<コレクション名>.sqlite、SQLite FTS5 の BM25）
│   │   ├── sqlite_batch.py   # SQLite の書き込みのまとめコミット（取り込み用。WAL・synchronous=NORMAL）
│   │   ├── journal.py        # 取り込みジャーナル（ingest_journal.sqlite。強制終了で書きかけになったファイルの片付け用）
│   │   ├── gc_pacer.py       # 取り込み・コピー中の循環参照のゴミ（httpx の応答）の定期回収
│   │   └── ingest.py / search.py # 差分取り込み（CLI: python -m src.vector_db.ingest）・検索
│   ├── project_instructions.py # .locohane/LOCOHANE.md の読込
│   ├── graph.py             # ReAct ループ（handwritten / prebuilt を切替）
│   ├── llm/                  # ChatOpenAI（llama-server接続）の構築
│   │   ├── chat_model.py     # ChatLlamaCpp・build_model()
│   │   ├── loop_guard.py     # 反復ループ検知（ThinkingLoopDetected）
│   │   ├── thinking_control.py # ステップごとの思考レベル切り替え（[thinking_control]）
│   │   ├── routing.py        # 接続先ルーティング（round_robin/random/priority_failover）
│   │   └── diagnostics.py    # 診断用ロギング
│   ├── context_trim.py      # 古い ToolMessage の切り詰め
│   ├── context_compaction.py # 会話履歴の自動要約・圧縮
│   ├── main_token_guard.py  # メインエージェントのトークン量ガード・引継ぎプロンプト自動生成
│   ├── tool_loop_guard.py   # 全く同じ応答を連続で繰り返すループの検知（[tool_loop_guard]）
│   ├── subagent.py          # dispatch_agent の内部ReActループ
│   ├── mcp_client.py        # MCPサーバー接続（stdio）・ツール変換
│   ├── chat_log.py          # 会話ログのテキストファイル記録
│   ├── thread_store.py      # 会話スレッド一覧（左サイドバー）・再開用の軽量ストア
│   ├── plan_persist.py      # plan/plan_approved変更をターン完了を待たず即座にthread_storeへ永続化
│   ├── ask_relay.py         # approve_plan/AskUserQuestion/ask_user_choice応答待ちをスレッド切替後の別セッションへ引き継ぐ状態
│   ├── ask_reconnect.py     # 同じセッションのWebSocket再接続後に、回答待ちの質問（ボタン・入力欄）を新しい接続へ送り直すパッチ
│   ├── instance_lock.py     # 同一データディレクトリへの多重起動を防ぐプロセス排他ロック
│   ├── cleanup.py           # 不要ファイルの自動削除
│   ├── files.py             # ファイルアップロード処理
│   ├── images.py            # 画像処理・Data URL変換
│   ├── tool_labels.py       # ツールの表示名（UIのStepバッジとサブエージェントの進捗表示で共有）
│   ├── i18n.py              # チャット画面向けの表示言語（システム通知・ボタン等の文言、応答言語の指示）
│   ├── log_rotation.py      # app.log の日時ローテーション
├── agents/
│   ├── README/AGENTS_README.md # エージェント種別定義（frontmatter）の書き方ガイド（*.mdスキャン時の警告を避けるためREADME/配下に退避）
│   ├── explore.md           # 読み取り専用の汎用調査・オフィス文書/PDF調査エージェント種別
│   ├── worker.md            # 承認済み計画に沿って読取り→書込みを内部完結させる書き込み可能エージェント種別
│   └── verifier.md          # 成果物検証用エージェント種別
├── skills/
│   ├── SKILLS_README.md    # スキル開発者向けガイド
│   ├── skill-creator/       # ユーザー別ドラフトスキルの作成・改善案の作成・eval検証を行うメタスキル
│   ├── archive-tools/       # zip/tar等の圧縮・解凍・中身の一覧（標準ライブラリのみ）
│   ├── docx-read/           # docx読込専用（段落・表・文書プロパティ・Track Changes有無）
│   ├── docx-create/         # docx新規生成（見出し/段落/表/画像/ページ設定等）
│   ├── docx-edit/           # 既存docxの編集（検索置換・Track Changes・段落追加削除）
│   ├── docx-render/         # Word文書ページの画像化
│   ├── excel-read/          # xlsx/xls/xlsm読込専用（シート一覧・セルデータ）
│   ├── excel-edit/          # xlsx/xlsm新規作成・編集（セル/書式/行列/グラフ等）
│   ├── excel-recalc/        # xlsx/xlsm/xls数式再計算・エラーセル検出
│   ├── excel-vba-read/      # xlsm/xls VBAマクロコード読込専用
│   ├── excel-vba-edit/      # xlsm VBAマクロコード追加/上書き/削除・実行
│   ├── excel-render/        # Excelシートの画像化
│   ├── excel-vba-knowledge/ # Excel VBAのコーディング作法・落とし穴のローカル知識ベース
│   ├── pdf-tools/           # PDF読込・ページ画像化・PDF生成
│   ├── pptx-read/           # pptx読込専用（テキスト・表・発表者ノート抽出）
│   ├── pptx-create/         # pptx新規生成（16:9テンプレート方式）
│   ├── pptx-inspect/        # 既存pptxテンプレートの構造読取（shape_index把握）
│   ├── pptx-edit/           # 既存pptxテンプレートの部分編集（デザイン保持）
│   ├── pptx-render/         # PowerPointスライドの画像化
│   ├── office_shared/       # docx/excel/pptx各スキルのscripts/が共有するPython共通処理（SKILL.mdを持たずLLMには公開されない）
│   └── web-search/          # Tavily APIによるWeb検索（要APIキー設定）
│       # Read/Glob/Grep/json_query/search_path_memory はネイティブツール化済み
│       # （src/tools/read_tool.py 等、src/path_memory.py）。
├── tests/                   # pytestテストケース
│   ├── conftest.py
│   ├── fixtures/
│   └── test_*.py            # 各モジュールのテスト
└── data/                    # 実行時生成（.gitignore 済み）
    ├── checkpoints.sqlite   # LangGraph の会話状態
    ├── logs/                # アプリ動作ログ（日時ローテーション）
    ├── logs_chat/           # 会話ログ（ユーザー発言＋AI応答）
    ├── memory/              # 永続メモリー（type別サブフォルダ＋MEMORY.md）
    ├── path_memory/         # パスメモリーレジストリ（.json）
    ├── plans/               # create_plan が detail_markdown を渡した際の詳細計画Markdown
    └── temp/                # 既定の作業ディレクトリ（スレッド専用フォルダ _tmp_*/。アップロードしたファイルもここ）
```

各モジュール冒頭に「そのファイルが仕様のどの段階を実装するか」をコメントで記載している。

---

## Agent Skills 仕様への準拠範囲

仕様: <https://agentskills.io/specification>

### 準拠している範囲

- **SKILL.md frontmatter**: `name`・`description`（必須）を検証。
  `license`・`compatibility`・`metadata`・`allowed-tools`（任意）は読み取り可能。
- **`name` 検証ルール**: 1〜64 文字 / 小文字英数字・ハイフン・アンダースコアのみ /
  先頭末尾は区切り文字不可 / 区切り文字の連続不可 / **親ディレクトリ名と一致**。
- **`description` 検証**: 非空・1024 文字以内。
- **ディレクトリ構造**: `scripts/`・`references/`・`assets/` を想定した読み込み・実行。
- **progressive disclosure の 3 段階**（Discovery / Read / Execute）。
- **SKILL.md本文の呼び出し例はコマンドライン形式で統一**: `scripts/` 配下の
  スクリプトを呼ぶ例は、Locohane固有の `run_script` JSON引数形式
  （`{"skill_name":..., "script_filename":..., "script_args":[...]}`）ではなく、
  他のAgent Skills対応環境（Claude Code等）と共通の `python <script>.py <args...>`
  形式で書く（`skills/SKILLS_README.md` 4-0節参照）。これにより `skills/<name>/`
  フォルダは他環境へそのまま持ち出せるだけでなく、逆にGitHub等で公開されている
  `python <script>.py <args...>` 形式のサードパーティ製Agent Skills（他環境向けに
  書かれたSKILL.md）も、呼び出し記法の差異という障壁なくLocohaneへ導入できる
  （依存ライブラリの導入等、環境固有の準備は別途必要）。この形式からLocohane
  固有の `run_script` 呼び出しへの変換は、SKILL.md側にもコード側にも実装を持たず、
  LLM側の共通プロンプト指示（`system_prompt/system_prompt.md`・
  `system_prompt/subagent_common.md` の「SKILL.md呼び出し例の変換ルール」節）が
  ツール呼び出し時に行う。
- 仕様違反の SKILL.md は **スキップし警告ログ** を出す（全体は落とさない）。
- **ドラフトスキル（Locohane 独自の拡張）**: skill-creator が作るドラフトも同じ SKILL.md 形式だが、
  起動時の走査対象ではなく、作成者本人の会話でだけ `ユーザー名/スキル名` として一覧に出る
  （「ドラフトスキル（ユーザー別）と昇格」参照）。昇格後のスキルは通常のスキルと同じ扱いになる。

### 範囲外（実装していない）

- **`allowed-tools`**: フィールドは読み取れるが、それに基づく自動承認は未実装（仕様上も実験的）。
- **公式バリデータ（skills-ref）非統合**: 自前の最小検証のみ。厳密検証が必要なら
  `skills-ref validate ./skills/<name>` を別途利用のこと。
- **深いネスト参照**: reference 参照は SKILL.md から 1 階層を想定。

---

## データの保存場所と手動削除の手順

すべて `data/` 配下（`config.ini` の `[paths]`/`[elements]`/`[log]`/`[default_workdir]` 等の `dir` 系キーで変更可）。`data/` は `.gitignore` 済み。保存先ルートをまとめて変更したい場合は、各キーを個別に書き換える代わりに `[paths] common_data_dir`（既定 `./data/${instance}`）だけを変更すればよい（対応するキーの値は `${common_data_dir}` を参照する形で書かれている）。`${instance}` は起動中のインスタンス名（`app.bat` で直接起動した場合は `default`）に置換されるため、既定ではインスタンスごとに `data/<インスタンス名>/` へ分かれる。下表の `data/` は、既定設定では `data/<インスタンス名>/`（例: `data/default/`）を指す。

**ネットワーク上には置けない:** `[admin] instances_dir`・`[paths] common_data_dir`・`checkpoint_db`・`[thread_store] db`・`[vector_db] dir`
（SQLite を置く場所）は、ローカルディスクに限る。UNC パス（`\\server\share\…`）・ネットワークドライブ（Z: 等）を指定すると、起動時と
設定ダッシュボードの保存時にエラーになる（ファイル共有上の SQLite はロックが遅く不確実で、「database is locked」で処理が失敗しやすく、WAL も使えないため）。
ファイル共有へは「データのバックアップ」（設定ダッシュボード節）で退避する。

**旧バージョン（データを `data/` 直下に保存していた版）からの移行:** 既定の保存先が `./data` から `./data/${instance}` に変わったため、そのまま起動すると過去のスレッド・永続メモリー等が見えなくなる（消えてはいない）。アプリ停止中に、`data/` 直下のファイル・フォルダ（`checkpoints.sqlite`・`chat_threads.sqlite`・`memory/`・`logs/`・`elements/`・`plans/`・`path_memory/`・`logs_chat/`・`temp/`・`app.lock` 等）を `data/default/` へ移動すれば引き継げる。移動せず旧来の場所を使い続けたい場合は、管理ツールの設定画面で default インスタンスの `[paths] common_data_dir` を `./data` にする。

| パス | 中身 | 削除してよいタイミング | 削除方法 |
|------|------|------------------------|----------|
| `data/checkpoints.sqlite` | LangGraph の会話状態（全スレッドの履歴） | 過去の会話履歴が不要になったとき | ファイルを削除（アプリ停止中に） |
| `data/chat_threads.sqlite` | 会話スレッド一覧（画面左サイドバー）・再開用のメタデータ（スレッド名・所有者・Chainlitの Step 履歴）。`checkpoints.sqlite` とは別ファイル（`[thread_store]`参照） | 一覧・再開機能が不要になったとき | ファイルを削除（アプリ停止中に。`checkpoints.sqlite` 自体は消えないため会話状態は失われない） |
| `data/logs/app.log` | アプリの動作ログ | いつでも | ファイルを削除 |
| `data/memory/` | 永続メモリー（共通の `user`/`feedback`/`project`/`reference` サブフォルダ＋`MEMORY.md`索引、ユーザー別の `users/<ユーザー>/`） | 蓄積した記憶が不要になったとき | フォルダ内を削除（`MEMORY.md`は次回保存時に再生成される）。特定のユーザーの分だけなら `users/<ユーザー>/` を削除 |
| `data/vector_db/` | ベクターDB の `catalog.sqlite`（コレクション定義・取り込み済みファイルの記録）と、`backend = sqlite_vec` 時のベクター `vectors.sqlite`、本文のキーワード索引 `keywords/<コレクション名>.sqlite`（コレクションごとに1ファイル）、強制終了で書きかけになったファイルを記録する取り込みジャーナル `ingest_journal.sqlite`（`[vector_db] dir`）。`catalog.sqlite` には別のインスタンスのコレクションの参照（`collection_refs` 表）も入る。取り込み元の文書そのものは複製しない | コレクションが不要になったとき | 設定ダッシュボードのナレッジ画面でコレクションを削除する（フォルダごと消す場合はアプリと管理ツールの取り込みを止めてから。`backend = qdrant` のベクターは Qdrant 側に残る） |
| `data/plans/` | `create_plan` が `detail_markdown` 引数を渡した場合の詳細計画Markdown（`[paths] plans_dir`） | 古い計画が不要になったとき | フォルダ内を削除 |
| `data/skill_drafts/<ユーザー名>/` | skill-creator が作ったユーザー別ドラフトスキル（`<スキル名>/`）と、その評価結果（`_workspace/`）。`[skill_creator] draft_dir`。不採用のドラフトも記録として残る（一覧には出ない） | 不要なドラフトがあるとき | ドラフトのフォルダを削除（作成者は skill-creator の `delete_draft.py` でも消せる）。自動削除はされない |
| `data/<インスタンス名>/skill_drafts_archive/<ユーザー名>/<スキル名>_<日時>/` | 昇格したドラフトの退避先（`_workspace/` ごと移る。`[skill_creator] archive_dir`） | 記録が不要になったとき | フォルダを削除 |
| `data/skill_lab/themes/<テーマID>/` | スキル研究室の研究テーマ（仕様・開発中の資産・ケース・入力ファイル・評価と AI 修正の記録 `runs/`）。対象インスタンスごとに分かれる | テーマが終わり、記録が不要になったとき | スキル研究室のテーマ一覧・テーマ詳細の「削除」（下記「スキル研究室」）。手で消す場合はテーマのフォルダを削除（実行中でないこと） |
| `data/temp/_tmp_<作成時刻>_<短縮ID>/` | `execute_python_code`/`run_script` の中間生成物・`write_scratch_note`/`write_thread_note` の書き出し先。チャットでアップロードしたファイル（`uploads/<送信日時>/`。作業フォルダを指定していてもここ）。自セッション専用（`[default_workdir]` 参照）。フォルダ名先頭の作成時刻（`YYMMDDHHMMSS`）はファイラー上で作成順に並べるためのもの。短縮IDは thread_id の SHA-1 先頭10桁（パス長上限 260文字を超えにくくするため、thread_id をそのまま使わない。旧形式 `_tmp_<作成時刻>_<thread_id>` のフォルダも再開時にそのまま使われる） | セッション終了後、不要になったとき | フォルダ内を削除（`[default_workdir] retention_days`/`cleanup_interval_hours` により自動削除もされる） |
| `data/elements/<thread_id>/` | 添付ファイル（`provide_download`/`analyze_image`の`show_in_chat=True`等）・回答本文への画像埋め込みの永続化先。スレッド再開・プロセス再起動後も表示できるようここへ実体をコピー保存する（`[elements]`参照、`src/thread_store.py`） | 添付ファイルが不要になったとき | フォルダ内を削除 |
| `instances/_assistant_index/` | 管理ツールの「AIに相談」が検索に使う、config.ini・README_DETAIL.md・API リファレンスの段落の埋め込みのキャッシュ（埋め込みの接続先・モデルごとに1ファイル。`data/` 配下ではない） | いつでも（次の相談で作り直す。初回は数十秒かかる） | フォルダを削除 |
| `.files/` | Chainlit自身のセッションファイル配信ディレクトリ（送信直後のライブ表示にのみ使う一時配信。プロジェクト直下、`data/`配下ではない） | いつでも | フォルダ内を削除 |
| `data/app.lock` | 同一データディレクトリへの多重起動を防ぐプロセス排他ロック（`src/instance_lock.py`）。空ファイルにOSのファイルロックをかけるだけで中身は使わない | アプリ停止中、削除しても実害はない | ファイルを削除（アプリ起動中は削除不可） |
| `data/runtime_status.json` | 接続中セッション・生成中スレッドのスナップショット（設定ダッシュボードのモニター表示用、`src/runtime_status.py`）。本体が数秒ごとに更新し、正常終了時に削除する | 異常終了で残った場合 | ファイルを削除（次回起動時に作り直される） |
| `instances/<name>/` | 設定ダッシュボード（管理ツール）が管理するインスタンス別ディレクトリ（`config_overrides.json`・`.env`・`settings/`・`backups/`・`app_stdout.log`・スキル研究室で昇格したそのインスタンス専用のスキル・サブエージェント `skills/`・`agents/`・回帰テスト用の `evals/`。データ本体は`data/<name>/`） | インスタンス自体が不要になったとき | 管理ツールの削除機能を使う（稼働中・`default`は不可。ログ・スレッド・default_workdir 等の永続データは、削除ダイアログで選択したものだけ一緒に削除される。詳細は「設定ダッシュボード」節） |

`data/temp/_tmp_*/` は `config.ini` の `[default_workdir] retention_days`（既定7日）を過ぎたフォルダを
アップロードしたファイルごと `cleanup_interval_hours`（既定1時間）おきに自動削除する。`retention_days` を0以下にすると
自動削除は無効化される。アップロードした画像は会話へ直接埋め込まず保存先のパス（`@N`）だけを LLM へ渡すため、
この日数を過ぎてから古いスレッドを再開すると、そのスレッドでアップロードしたファイルはもう開けない（再度アップロードする）。
旧版の `data/uploads/`（`[uploads]` は廃止）は使われなくなったため、不要ならフォルダごと削除してよい。
`data/elements/` も `[elements] retention_days`（既定7日）・
`cleanup_interval_hours`（既定1時間）で同様に自動削除される。`.files/` も `[chainlit_files]
retention_days`（既定7日）・`cleanup_interval_hours`（既定1時間）で同様に自動削除される
（Chainlit自身のパス管理下にあるため `dir` キーは無い。ディレクトリ単位＝セッションIDごとに
削除する点が他と異なる）。

手動全削除（PowerShell / cmd、アプリ停止中に実行）:

```powershell
Remove-Item -Recurse -Force .\data\*
```

「孤児ファイル」を作らない設計: 保存は `app.py` の `_save_uploads()`（`data/temp/_tmp_*/uploads/` へ）、
会話履歴は `AsyncSqliteSaver`（`data/checkpoints.sqlite`）に限定される。
どこに何が書かれるかはコード上で一意に追える。

---

## セットアップと起動

### 1. Python 環境の指定と依存インストール

プロジェクトで使う Python 仮想環境は、プロジェクト直下の `python_env.bat` の
`PYTHON_DIR` **1か所だけ**で指定する。最初に `python_env.bat.example` を `python_env.bat` に
コピーし、ここを実際の環境に書き換える（`python_env.bat` はこのマシンのパスを含むため git 管理外。
無いまま `admin.bat`・`app.bat`・`mcp_server.bat` を起動すると、コピーするよう案内して止まる）。
VS Code でデバッグする場合は、同じように `.vscode/launch.json.example` を `.vscode/launch.json` に
コピーし、`python` のパスを直す。

```bat
set PYTHON_DIR=C:\work\venv_locohane
```

| 変数 | 設定する値 |
|---|---|
| `PYTHON_DIR` | Locohane を実行する Python 仮想環境のディレクトリ。`admin.bat`・`app.bat`・`mcp_server.bat`（`.mcp.json` から起動）はこのファイルを `call` して使う。**管理ツールから起動する各インスタンスも同じ Python（`sys.executable`）で起動され**（`admin/supervisor.py` 参照）、`config.ini` の `[scripts].python` が空欄（既定）なら `run_script`/`execute_python_code` もこの Python を使う |

`python_env.bat` は `PYTHONUTF8=1` も設定し、英語版など日本語以外の Windows でも標準入出力と `open()` の既定を UTF-8 にする。

続けて、その環境に依存パッケージをインストールする（コマンドプロンプトで実行）。

```cmd
call python_env.bat
python -m pip install -r requirements.txt
```

### 2. 推論サーバー（llama.cpp / vLLM）の起動例

LLM接続先はOpenAI互換エンドポイントであれば何でもよいが、`config.ini`の
`[llm] main_url`/`sub_url`内の`provider`（`llama_cpp`/`vllm`/未指定＝
`openai_compatible`）を実際のサーバー種別と揃えると、拡張パラメータ
（`repeat_penalty`/`dry_*`/`reasoning_budget`等）の名称・送信可否を
サーバーごとの方言に自動で合わせてくれる（`src/llm/dialect.py`）。

**llama-server（llama.cpp）**

OpenAI 互換エンドポイントを `http://localhost:8080/v1` で公開する:

```bash
llama-server --model C:\path\to\model.gguf --alias local-model --host 127.0.0.1 --port 8080 -c 8192
```

- `--alias` はモデル名。`config.ini` の `[llm] main_url` 内の `model` と揃える。
- サンプリング（`top_p`/`top_k`/`repeat_penalty`/`frequency_penalty`/`presence_penalty`/`max_tokens`）は
  `--repeat-penalty` 等の起動時 CLI オプションでも既定値を指定できるが、`config.ini` の
  `[llm]` 側で値を指定した場合はリクエストごとにその値が優先される（未指定＝空欄の項目のみ
  llama-server 起動時の既定値が使われる）。

**vLLM**

OpenAI 互換エンドポイントを `http://localhost:8000/v1` で公開する:

```bash
vllm serve /path/to/model --served-model-name local-model --host 127.0.0.1 --port 8000
```

- `--served-model-name` はモデル名。`config.ini` の `[llm] main_url` 内の `model` と揃える。
- thinking（`<think>`ブロック）モデルを使う場合は、`--reasoning-parser`
  （例: `deepseek_r1`）を起動時に指定する必要がある（vLLMはllama-serverと
  異なり `reasoning_format` をリクエストでは切り替えられない）。
- `dry_*`（DRYサンプラー）・`reasoning_format`・`reasoning_budget_message`は
  vLLMには送られない（`src/llm/dialect.py`が自動で除外する）。

接続先・モデル名は `config.ini`（または環境変数 `LLM_MAIN_URL` / `LLM_SUB_URL`）で切り替え可能。
どちらのサーバーも、接続先・拡張パラメータは起動後に設定ダッシュボード
（管理ツール）のUIから変更できる（「設定ダッシュボード（管理ツール）」節参照）。

> **⚠️ 商用利用時のモデルライセンス**
> llama.cpp・vLLM自体はいずれもMITベースのライセンスですが、**動かすモデルの
> ライセンスはモデルごとに異なります**（基盤コードには含まれません）。
> 商用利用時は必ず確認してください。
> - Qwen2.5 系 / Mistral 系 → Apache 2.0（商用可・推奨）
> - Llama 系 → Meta Llama Community License（商用可だが月間7億MAU条項など独自制約あり）
> - Gemma 系 → 独自の Gemma Terms of Use
> 迷ったら Apache 2.0 のモデル（Qwen2.5 等）を選ぶのが安全です。

### 3. 環境依存パスの最低限の設定

推論サーバー（llama-server/vLLM）を起動したら、環境依存で必ず実際の値に合わせる必要がある
パス設定を行う。設定ダッシュボード（管理ツール）を使う場合、
**LLM接続先（`[llm] main_url`/`sub_url`）はログイン後にUIから設定できる**ため、
起動前に `config.ini` を直接編集する必要はない。Python 環境は手順1の
`python_env.bat` で指定済みなので、ここで事前にテキストで用意しておく
必要があるのは次の2ファイルだけ。

**`.env`（管理ツールのログイン情報）**

| キー | 設定する値 |
|---|---|
| `ADMIN_USERS` | 管理ツールへログインするユーザー名/パスワードの組。`.env.example` をコピーして `.env` を作成し設定する（未設定だと管理ツールは起動を拒否する） |

**`admin.bat`（管理ツール自身の起動設定）**

| 変数 | 設定する値 |
|---|---|
| `ADMIN_HOST` / `ADMIN_PORT` | 管理ツール自身の待受ホスト・ポート（既定 `127.0.0.1:8001` 相当）。ポートが他と競合する場合のみ変更する |

ここまで設定すれば `admin.bat` を起動でき、ログイン後はダッシュボードの
「インスタンス」→ 対象インスタンスの「設定」→ `config.ini` タブから
`main_url`/`sub_url` を検索して設定・保存し、インスタンスを
再起動すれば反映される（詳細は「設定ダッシュボード（管理ツール）」節）。
`run_script`/`execute_python_code` だけ別の Python で動かしたい場合は、
同じ画面で `[scripts].python` を指定する（空欄なら `python_env.bat` の環境）。

**Claude Code・Qwen Code でこのプロジェクトを開発・テストする場合の実行環境（アプリの実行には不要）**

マシンごとのパスは `CLAUDE.md`・`QWEN.md`（公開リポジトリにコミットされる）には書かず、git 管理外の
`python_env.bat` だけに書く。スキルや動作確認のコマンドは `<PYTHON_DIR>\Scripts\python.exe` を使う。

| 変数（`python_env.bat`） | 設定する値 |
|---|---|
| `PYTHON_DIR` | 手順1と同じ。開発・テストでも同じ仮想環境を使う |
| `NODE_DIR` | `frontend/`（package.json あり）をビルド・テストするときだけ、Node.js のディレクトリ。空欄なら PATH に足さない（`public/build` は同梱済みなので通常は不要） |

**管理ツールを使わず、テキストエディタで直接設定する場合**

`config.ini` の `[llm] main_url`/`sub_url` を直接書き換えれば（Python 環境は
手順1の `python_env.bat` で指定済み）、管理ツールを介さず
`app.bat` で単体起動できる（各項目の意味は後述の「設定リファレンス
（config.ini）」参照）。

### 4. アプリ起動

```cmd
admin.bat
```

管理ツール（設定ダッシュボード）が起動し、`default` インスタンス
（Locohane本体）を自動起動する。詳細は「設定ダッシュボード
（管理ツール）」節を参照。

開発時にホットリロードしながら単体で動かしたい場合は次のいずれか。

```cmd
rem 方法A: chainlitを直接起動（python_env.bat の環境を読み込んでから）
cd C:\work\Locohane_Agent
call python_env.bat
chainlit run app.py -w

rem 方法B: app.bat（instances/default/instance.json のホスト・ポートに追従）
app.bat
```

ブラウザで開き、例えば「この Excel ファイルの中身を要約して」と送ると、
`read_skill`（excel-read の本文読込）→ `run_script`（`read_excel.py` 実行）が
**ステップとして可視化** され、結果がストリーミング表示される。

### 5. HTTPS で待ち受ける（任意）

LAN 上の他端末やスマホから使う場合など、通信を暗号化したいときは
`config.ini` の `[admin]` にサーバー証明書と秘密鍵（どちらも PEM 形式）の
パスを両方指定する（環境変数 `SSL_CERTFILE` / `SSL_KEYFILE` でも可）。

```ini
[admin]
ssl_certfile = ./certs/server.crt
ssl_keyfile = ./certs/server.key
```

- 管理ツール（uvicorn の `ssl_certfile`/`ssl_keyfile`）と、管理ツールから起動する
  本体インスタンス・`app.bat`（`chainlit run` の `--ssl-cert`/`--ssl-key`）が、
  同じ証明書で HTTPS 待ち受けになる（インスタンスごとの切り替えはできない）。
  URL は `https://<ホスト>:<ポート>` になり、HTTP では接続できなくなる。
- 片方だけの指定や、ファイルが見つからない場合は起動を拒否する
  （HTTP へ黙ってフォールバックしない）。判定は `src/config.py` の `resolve_ssl_files()`。
- 管理ツールのセッションクッキーには HTTPS 時に `Secure` 属性が付く。
- 変更後は `admin.bat`（または `app.bat`）を起動し直す。管理画面からの
  インスタンス再起動だけでは、管理ツール自身は HTTPS にならない。

証明書は、社内 CA があればそこで発行したものを使うのが望ましい。検証用に
自己署名証明書を作る場合の例（Git for Windows 同梱の openssl。接続に使う
ホスト名・IP アドレスを `subjectAltName` に含めること）:

```cmd
openssl req -x509 -newkey rsa:2048 -nodes -days 365 ^
  -keyout certs\server.key -out certs\server.crt -subj "/CN=locohane" ^
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:192.168.1.10"
```

自己署名証明書ではブラウザに警告が出るため、利用する端末に証明書を
信頼済みとして登録する（スマホの「ホーム画面に追加」等も、信頼されて
いない証明書では正しく動かないことがある）。秘密鍵はリポジトリへ
コミットしないこと（プロジェクト直下の `certs/` は `.gitignore` 対象）。

---

## 開発環境

本プロジェクトの開発・動作検証に実際に使用している環境の一例。

### コーディングAgentツール

| ツール | 内容 |
|--------|------|
| ClaudeCode | Claude Pro Plan |
| QwenCode | llama-server（ローカル）+ Qwen3.6-35B-A3B |

### OS / ハードウェア

- OS: Windows 11
- CPU: Intel(R) Core(TM) Ultra 5 225（10コア / 10論理プロセッサ、ベース3.30GHz）
- メモリ: 32GB
- GPU: NVIDIA GeForce RTX 5060 Ti（専用GPUメモリ 16.0GB）

### llama-server 起動コマンド例

上記ハードウェアに合わせて以下の引数で `llama-server` を起動している。

```
  --model .\models\unsloth\Qwen3.6-35B-A3B-GGUF\Qwen3.6-35B-A3B-UD-Q4_K_M.gguf
  --mmproj .\models\unsloth\Qwen3.6-35B-A3B-GGUF\mmproj-F16.gguf
  --n-cpu-moe 25
  --n-gpu-layers 99
  --threads 10
  --threads-batch 10
  --threads-http 4
  --models-max 1
  --parallel 2
  --batch-size 4096
  --ubatch-size 2048
  --ctx-size 256000
  --n-predict 32000
  --temp 0.6
  --top-p 0.95
  --top-k 20
  --min-p 0.0
  --presence-penalty 0
  --repeat-penalty 1.0
  --cache-reuse 256
  --cache-ram -1
  --swa-full
  --cache-type-k q8_0
  --cache-type-v q8_0
  --flash-attn on
  --jinja
  --no-mmap
  --mlock
  --reasoning-format deepseek
  --reasoning-budget 8192
  --reasoning-budget-message "**Wait, I am overthinking this. I should answer now.**"
```

- `n-cpu-moe`/`n-gpu-layers` はGPUの専用メモリ容量（16GB）に収まるよう調整したMoEオフロード設定。
- `ctx-size`/`cache-type-k`/`cache-type-v` はコンテキスト長とKVキャッシュ量子化のバランスをとった値。

---

## 同梱スキル

| スキル | 配置場所 | 種別 | 内容 |
|--------|----------|------|------|
| `skill-creator` | `skills/` | スクリプト実行を伴う | 自分専用のドラフトスキルの作成・既存スキルの改善案の作成・本文の書き込み・evalハーネスによる繰り返し評価（スキル安定化トライアウト）・description のトリガー精度最適化を行うメタスキル。正式スキルは書き換えない（下記「ドラフトスキル（ユーザー別）と昇格」）。 |
| `pdf-tools` | `skills/` | スクリプト実行を伴う | PDFのテキスト抽出・ページ画像化（レイアウト/図表/スキャン内容の視覚把握）・PDF生成（日本語対応）。 |
| `docx-read` | `skills/` | スクリプト実行を伴う | docxの読込専用（段落・表・文書プロパティ・Track Changes有無・画像有無の取得）。 |
| `docx-create` | `skills/` | スクリプト実行を伴う | docxの新規生成（見出し/段落/箇条書き/表/画像/ページ設定/ヘッダーフッター等）。 |
| `docx-edit` | `skills/` | スクリプト実行を伴う | 既存docxの編集（検索置換、画像挿入・サイズ変更、Track Changes/変更履歴の付与・確定・却下を含む。画像トリミングは非対応）。 |
| `docx-render` | `skills/` | スクリプト実行を伴う | Word文書ページの画像化（レイアウト・表・画像配置・強調表現の視覚把握）。 |
| `excel-read` | `skills/` | スクリプト実行を伴う | xlsx/xls/xlsmの読込専用（シート一覧・セルデータ・画像有無）。 |
| `excel-edit` | `skills/` | スクリプト実行を伴う | xlsx/xlsmの新規作成・編集（セル/書式/行列/グラフ・画像追加と位置調整・条件付き書式・データ検証を含む。画像トリミングは非対応）。 |
| `excel-recalc` | `skills/` | スクリプト実行を伴う | xlsx/xlsm/xlsの数式再計算・エラーセル検出。 |
| `excel-vba-read` | `skills/` | スクリプト実行を伴う | xlsm/xlsのVBAマクロコードの読み込み専用。 |
| `excel-vba-edit` | `skills/` | スクリプト実行を伴う | xlsmのVBAマクロコードの追加/上書き/削除・実行。 |
| `excel-render` | `skills/` | スクリプト実行を伴う | Excelシートの画像化（罫線・書式・グラフ・レイアウトの視覚把握）。 |
| `excel-vba-knowledge` | `skills/` | 参照のみ | Excel VBAのコーディング作法・定石・落とし穴を蓄積したローカル知識ベース（`references/`参照）。 |
| `pptx-read` | `skills/` | スクリプト実行を伴う | pptxの読込専用（スライドのタイトル・本文・表・発表者ノートの抽出）。 |
| `pptx-create` | `skills/` | スクリプト実行を伴う | pptxの新規生成（16:9テンプレート方式）。 |
| `pptx-inspect` | `skills/` | スクリプト実行を伴う | 既存pptxテンプレートの構造読取専用（`pptx-edit`前の`shape_index`把握）。 |
| `pptx-edit` | `skills/` | スクリプト実行を伴う | 既存pptxテンプレートの部分編集（デザインを保った差し替え・複製・削除・並び替え・画像/グラフの新規追加・画像トリミング）。 |
| `pptx-render` | `skills/` | スクリプト実行を伴う | PowerPointスライドの画像化（レイアウト・図表・画像配置・強調表現の視覚把握）。 |
| `archive-tools` | `skills/` | スクリプト実行を伴う | ファイル・フォルダの圧縮（zip/tar/tar.gz/tar.bz2/tar.xz）、アーカイブの中身の一覧、解凍（左記に加え単体の .gz/.bz2/.xz）。日本語版Windowsで作った cp932 名の zip も文字化けせずに展開し、展開先の外へ書き出すエントリ（Zip Slip）は展開しない。7z/RAR・AES暗号化zipは非対応。 |
| `web-search` | `skills/` | スクリプト実行を伴う | Tavily APIによるWeb検索。スキル専用の`scripts/.env`にTAVILY_API_KEY設定時のみ動作（既定では通信なし）。 |

`.locohane/skills/` はユーザー独自スキルの置き場（`skills/` とマージ走査、同名は優先）で、現状は使い方を示す `README.md` のみを含む。
`.locohane/` は全インスタンス共通のため、組織・役割ごとに分けたいスキル・サブエージェントは、インスタンス専用の
`instances/<インスタンス名>/skills/`・`agents/`（`[skill_lab] instance_locohane_dir`、最優先で走査）に置く。
管理ツールのスキル研究室で昇格したものは、ここへ置かれる。

スキル開発の詳細な手順・規約は [`skills/SKILLS_README.md`](skills/SKILLS_README.md) を参照。

処理時間が `[scripts].timeout`（既定300秒）に近づく、または超えうるスクリプトを持つスキルは、
SKILL.md 側にその旨（実行に時間がかかりうる）を明記する（`run_script`/`run_script_background`
といったLocohane固有のツール名はSKILL.md本文には書かない。4-0節参照）。長時間化しうる
スクリプトかどうかを踏まえて `run_script` と `run_script_background` のどちらを使うかは、
LLM側の共通変換ルール（`system_prompt/system_prompt.md`・`system_prompt/subagent_common.md`
の「SKILL.md呼び出し例の変換ルール」節）が判断する。`run_script_background` は
（`[scripts].background_inline_wait_max_seconds` の安全上限に達しない限り）完了まで
自動的に待ち、`run_script` と同じ最終結果を直接返す（進捗は人間向けにチャットへ自動で
通知されるため、SKILL.md 側でポーリング手順を指示する必要は無い）。安全上限を超える
ごく長時間のスクリプトに限り `job_id` を含む案内が返るので、その場合のみ
`check_script_job`/`stop_script_job` の使い方をSKILL.md に明記すればよい。

### ドラフトスキル（ユーザー別）と昇格

利用者（スキル開発の専門家とは限らない）がチャットから skill-creator で作るスキルは、
正式スキルではなく**ドラフト**として `[skill_creator] draft_dir`（既定 `data/<インスタンス名>/skill_drafts/`）の
`<ユーザー名>/<スキル名>/` に置かれる。設計の要点:

- **本番への混入を構造的に防ぐ**: `draft_dir` は起動時のスキル走査対象（`skills_dir`・`agents_dir`・
  `project_locohane_dir`）と同一・包含関係にすると起動時にエラーになる（`src/config.py` の `_validate_skill_draft_dir`）。
  skill-creator が書き込めるのは、`skills/` 配下の skill-creator 本体の実行に限り、自分のドラフトフォルダだけ
  （`src/tools/_subprocess_env.py` の `_skill_creator_draft_roots`）。正式スキルを直したいときは
  `fork_skill.py` で改善案のドラフトを作り、元のハッシュを記録する。
- **作成者本人の会話でだけ使える**: ドラフトはセッションごとに、スキル名 `ユーザー名/スキル名` として
  システムプロンプトのスキル一覧と `read_skill` の選択肢に足される（`app.py` の `_apply_session_draft_skills`、
  `src/tools/session_tools.py`）。ドラフトを作成・削除・description 変更した次のメッセージから反映される
  （`app.py` の `on_message` が毎回ドラフトの状態を前回と比べ、変わっていればグラフを組み直す）。
  ユーザーの判定はログインユーザー名をフォルダ名にしたもの（`src/skill_drafts.py` の `draft_owner_name()`。
  会話ログと同じ置換に加え、小文字化・末尾のドット/空白の除去・`..` や予約デバイス名の回避を行う）で行うため、
  認証を無効にしている場合は全員が同じ `anonymous` のドラフトを共有する点に注意。
- **他ユーザーのドラフトの扱いは4段階**（`[skill_creator] other_users_drafts`）: `hidden`（既定。一覧に出ず、
  Read/Glob/Grep/execute_python_code 等からも読めない）／`listed`（一覧にだけ出る）／`readable`（読み取りまで）／
  `full`（実行・書き込み・評価まで）。判定は `src/skill_drafts.py` に集約し、`_safe_path`・`Read` 系のパス解決・
  Grep の再帰検索・Python 実行の書き込みガード（読み取り・一覧取得も禁止するルートと、作業ディレクトリが
  `draft_dir` を含んでいても書き込みを禁止するルート）の各所から呼ぶ。
- **ドラフトは承認なしで直接使える**: ドラフトのスクリプトと skill-creator 本体のスクリプトは、
  `[plan] plan_approval_exempt_scripts`・`[main_agent_tool_guard]` の設定に関係なく、計画承認なし・
  メインエージェントからの直接実行ができる（作成者は config.ini を編集できないため）。名前ではなく
  スクリプトの実体の場所で判定するため、同名スキルの偽装では免除されない。書き込み先は通常どおり作業ディレクトリに限られる。
  `[main_agent_tool_guard]` 有効時、メインエージェントへ渡す `run_script`/`run_script_background` は、
  `skill_name` を直接実行できるスキル（allow_entries 登録分・skill-creator・実行できるドラフト）の選択肢に
  制約した複製になる（呼べないスキルを試して拒否される往復を防ぐ）。
- **評価は本番の構成の上にドラフトを重ねて行う**: skill-creator の `run_isolated_eval.py` は
  `evals/run_all.py --cases-dir <ドラフト>/evals --skill-overlay <ドラフト>` を実行する。会社専用の
  `project_locohane_dir` を含む本番のスキル構成を残したまま、ドラフトだけを最優先で重ねる。
  重ねたスキルのスクリプトは、昇格時に登録されるのと同じく計画承認・`[main_agent_tool_guard]` を免除した設定で
  評価する（会話で試した挙動・評価した挙動・昇格後の挙動を揃える）。`--repeat N` でスキル安定化トライアウトになる。
- **修正が入ったら合格回数は0から**: トライアウトは開始時のドラフトとケースを一時フォルダに固定して全回を評価し、
  その内容のハッシュを `tryout.json` に残す（`evals/skill_tree.py`）。ドラフトを1か所でも直すと、`_draft_meta.json` の
  トライアウト記録は空になり、昇格時もハッシュが一致しないトライアウト結果は使えない。
- **正式化はスキル開発者が管理ツールのスキル研究室で行う**（下記「設定ダッシュボード」の「スキル研究室」）。
  開発者はドラフトを研究テーマに取り込み（取り込んだ時点の複製で作業するため、作成者は引き続き編集できる）、
  仕様とケースを整え、研究室の AI に不具合を自己修正させて、トライアウト（`[skill_creator] tryout_repeats` 回、既定10回）に
  全回合格させる。人は確認・軽い手修正・承認／否認をする。承認すると、ドラフトを作ったインスタンス専用の置き場
  （`[skill_lab] instance_locohane_dir`、既定 `instances/<インスタンス名>/skills/`）へ昇格し、元のドラフトは
  `[skill_creator] archive_dir`（既定 `data/<インスタンス名>/skill_drafts_archive/`）へ `_workspace` ごと移る。
  差し戻すと、理由が作成者の skill-creator（`list_drafts.py` の `returned_reason`）に出る（研究室での修正をドラフトへ写すこともできる）。
- **昇格先はインスタンス専用の置き場だけ**: 同梱の `skills/`・`agents/` や、全インスタンス共通の `project_locohane_dir`
  （`.locohane/` 等）には置かない。組織別・役割別に分けたほかのインスタンスへ混ざるのを防ぐため。同梱・共有のスキルを
  改善したものも専用の置き場に同じ名前で置き、そのインスタンスでだけ置き換わる（元のファイルと他のインスタンスは変わらない）。
  スクリプトを持つスキルの免除設定（`[plan] plan_approval_exempt_scripts`・`[main_agent_tool_guard] allow_entries`）も、
  そのインスタンスの `config_overrides.json` にだけ登録する（管理ツールと同じ検証・バックアップ・変更履歴つき）。
  昇格したスキルと設定が効くのは再起動後。
- **Claude Code の promote-skill も残している**: ドラフト1件をそのまま判定して昇格させたいときの経路。昇格先・アーカイブ・
  設定の登録は研究室と同じ規則（`promote_helper.py install` は `--instance` 専用の置き場にだけ置く）。記録はどちらも `evals/promotion_log.md`。

### 新しいスキルの追加方法

`skills/<name>/SKILL.md` を作るだけ（`<name>` はフォルダ名 = frontmatter の `name`）。
ビルトイン一式を汚したくない場合は `.locohane/skills/<name>/SKILL.md` に置いてもよい
（`skills/` とマージ走査され、同名スキルがあれば `.locohane/skills` 側が優先される）。
最小の SKILL.md:

```markdown
---
name: my-skill
description: 何をするスキルか、いつ使うかを具体的に書く。
---

# my-skill

ここに手順を書く。必要なら scripts/ references/ assets/ を追加する。
```

`scripts/` 配下のスクリプトを呼び出す例を本文に書く場合は、Locohane固有の
`run_script` JSON引数形式ではなく `python <script>.py <args...>` というコマンド
ライン形式で書く（詳細は [`skills/SKILLS_README.md`](skills/SKILLS_README.md) 4-0節参照）。
実際の`run_script`呼び出しへの変換はLLM側の共通指示が行う。

アプリを再起動すると起動時走査で自動的に発見される（動的リロードはしない）。

---

## MCPサーバー接続

Anthropic公式のModel Context Protocol（[仕様](https://modelcontextprotocol.io/specification)）に
準拠し、外部のMCPサーバー（stdioトランスポートのみ対応）をLLMのツールとして動的に
利用できる。実装は `src/mcp_client.py`（公式Python SDK、PyPI: `mcp` を直接使用）。

- **設定ファイル**: プロジェクト直下 `.locohane/settings.json`（git管理対象）。
  Claude Code / Qwen Code の `mcpServers` 設定形式を参考にしている。
  ```jsonc
  {
    "mcpServers": {
      "my-server": {
        "command": "npx",
        "args": ["-y", "@some/mcp-server"],
        "env": { "API_KEY": "${MY_SERVER_API_KEY}" },
        "cwd": null,
        "disabled": false
      }
    },
    "mcp": {
      "enabled": true,
      "connectTimeoutSeconds": 15,
      "callTimeoutSeconds": 60
    }
  }
  ```
  トップレベルの `"mcp"` ブロックは `config.ini` の `[mcp]` セクション（既定値）を
  上書きする任意項目。`env` の値に `${ENV_VAR_NAME}` と書くと、接続時に
  `os.environ` から展開される。**`.locohane/settings.json` はgit管理対象のため、
  APIキー等の機密情報を直接書かず、必ずこの `${ENV_VAR_NAME}` 形式で参照すること。**
  未解決の環境変数がある場合、そのサーバーのみ接続をスキップする（警告ログ）。
- **接続方式**: アプリ起動時（`@cl.on_app_startup`）に `mcpServers` の全件へ自動
  接続する。Chainlit UIから都度追加する機能は提供しない。
- **ツール名の命名規則**: 衝突を避けるため `mcp__<サーバー名>__<ツール名>`
  （64文字以内に正規化）としてLLMに公開する。
- **エラー時の挙動**: 個別サーバーへの接続失敗・タイムアウトはそのサーバーのみ
  警告ログを出してスキップし、アプリ全体の起動は継続する。ツール呼び出し自体の
  失敗も例外を送出せず `"Error: ..."` 形式の文字列として返す。
- **既知の限界**: 接続後にサーバープロセスがクラッシュした場合の自動再接続は
  行わない。応答はテキストブロックのみ抽出する（画像等のリソースは未対応）。
- **`[main_agent_tool_guard]`との関係**: MCP動的ツールは名前が実行時にしか
  分からず`allow_entries`へ事前登録できないため、既定の`mode=all`のままだと
  メインエージェントから一切呼び出せない。事前登録なしで常に許可したい場合は
  `mode=tools_skills_only`を使う（詳細は`config.ini`内コメント参照）。

**MCPサーバー接続は、実装のみで動作テストしていないのでまともに動くか保証できません**

### 逆方向：Locohaneのスキルを配布する側（サーバー）

上記は Locohane が外部 MCP サーバーのツールを**使う側**（クライアント）の話。
逆に、Locohane の `skills/` に溜めた SKILL.md 群を Claude Code / Claude Desktop
等の外部 MCP クライアントから発見・取得・**実行**できるよう配布する側（サーバー）
の実装は `mcp_server/` パッケージにあり、こちらは動作確認済み。設定ファイルは
プロジェクト直下 `.mcp.json`（`command`/`args` にこのマシンのフルパスが書かれる
ため git 管理外。`.mcp.json.example` をコピーして `mcp_server.bat` のパスを直す）。詳細な仕組み・クライアント接続手順は
[`mcp_server/MCP_README.md`](mcp_server/MCP_README.md) を参照。

---

## 永続メモリー（スレッドをまたぐ記憶）

ClaudeCode のメモリー機能相当。会話（スレッド）が変わっても引き継ぎたい事実を、
`data/memory/`（`config.ini` の `[paths] memory_dir` で変更可）配下に
YAML frontmatter 付き Markdown ファイルとして保存する。ロジックは `src/memory.py`
に集約し、`src/tools/memory_tools.py` の6ツール（`create_memory`/`update_memory`/`delete_memory`/
`read_memory`/`search_memory`/`list_memories`）が薄いラッパーとして公開する。

- **4種類の type**: `user`（ユーザーの役割・選好）／`feedback`（訂正・確認済みの
  アプローチ）／`project`（進行中の作業の背景）／`reference`（外部リソースの所在）。
  それぞれ `<name>.md` として1ファイル保存される（置き場所は次の「ユーザー別のメモリー」）。
  `name` はファイル名になるため ASCII のスラッグ（英数字・`-`・`_`、64文字まで）に限る。日本語は `description` に書く。
  説明・本文は会話の表示言語で書かせる（`src/i18n.py` の `response_language_directive`。プロンプトが英語のため、
  指示しないと英語と日本語が混ざる）。言語が混ざった既存のメモリーは、設定ダッシュボードの「AIで翻訳」で揃えられる。
- **ユーザー別のメモリー**: 新しく保存するメモリーは、会話のログインユーザーのものとして
  `data/memory/users/<ユーザー>/<type>/<name>.md` に置かれる（`<ユーザー>` はスキルのドラフトと同じ規則で
  整えたフォルダ名。`[auth] enabled = false` なら全員 `anonymous`）。ユーザー別になる前からある
  `data/memory/<type>/<name>.md` は**共通**のメモリーとして残り、全ユーザーが検索・読み取り・更新できる。
  `name` は所有者ごとに一意（自分と共通・他のユーザーで同じ名前があってもよい）。
- **公開範囲（`visibility`）**: ユーザーのメモリーは frontmatter に `private`（本人だけ）か `shared`（全ユーザーが
  検索・読み取りできる。書き換え・削除は本人だけ）を持つ。決め方は `config.ini` の `[memory]` で変えられる
  （設定ダッシュボードの「メモリー」グループ。インスタンスごと）。社内のローカル運用が前提のため、既定は
  「本人・家族のことだけ個人用、ほかは共有」。
  - **LLM の判断**: LLM は、システムプロンプトの Memory System 節に差し込まれる振り分け基準（`shared_criteria` /
    `private_criteria`。`{{memory_visibility}}` を `src/memory.py` の `render_visibility_block` が組み立てる）に沿って、
    種類ではなく内容で `visibility` を選ぶ。省略すると `shared`。基準は言語を問わず、改行して複数の条件を書ける
    （プロンプトの「- "shared" for:」「- "private" for:」の下に字下げして入る）。設定画面の「AIで清書」で、下書きを
    LLM にダッシュボードの表示言語の箇条書きへ整えさせられる（`admin/memories.py` の `polish-criteria`。「AIに相談」と
    同じく「管理者設定」の「回答に使う LLM」の接続先を使い、保存はしない）。
  - **種類ごとの公開範囲**（`<type>_visibility`）: `private` の種類は、指定や内容に関係なく常に `private`
    （既定は `user` だけ）。`shared` の種類は上の LLM の判断に従う。
  - **強制的に private にする目印**（`force_private_checks`）: 有効にした目印が説明・本文・元ファイルのパスにあれば、
    指定に関係なく `private` で保存する。`email`（メールアドレス）・`phone`（電話番号）・`card_id`（カード／ID 番号）・
    `credential`（パスワード・口座番号・マイナンバー等の語）・`confidential_label`（社外秘等の表示）・
    `personal_path`（`C:\Users\<名前>\` の個人フォルダのパス）から選ぶ（`src/memory.py` の `_SENSITIVE_PATTERNS`）。
    既定は `card_id` と `credential` だけ。
  - 規則は `load_config()` が `memory_dir` ごとに登録し（`memory.set_policy`）、ツール・索引・設定ダッシュボードの
    編集が同じ規則を使う。変更はインスタンスの再起動後に効く（設定ダッシュボードの編集には保存直後から効く）。
    保存済みのメモリーの `visibility` は書き換えないが、`private` にした
    種類・有効にした目印に当たる `shared` のメモリーは、他のユーザーには見せなくなる。
  - `visibility` が無い・壊れているユーザーのメモリーも `private` とみなす。ログ（`create_memory: ... visibility=...
    (requested=...)`）には、LLM が指定した値（省略なら `None`）と実際に保存した値の両方が出る。
- **検索と優先順位**: `search_memory`/`list_memories`/索引は、自分のメモリー → 共通 → 他のユーザーの `shared` の
  メモリーの順に並べる（`(mine, ...)`・`(common)`・`(user: <ユーザー>)` を付ける）。他のユーザーの `private` の
  メモリーは LLM に一切渡さない（LLM の判断に任せず、`src/memory.py` の `is_visible_to` で機械的に除く）。
  `shared` でも、今の内容に個人情報・機密の目印があれば他のユーザーには見せない（保存後に手で書き換えられた場合の
  備え）。`read_memory(name)` は同じ名前なら自分のものを読み、他のユーザーのものには食い違ったときの従い方を添える
  （ランクを使っていなければ「自分のものに従う」、使っていれば下の「ユーザーのランク」の規則）。`update_memory`/`delete_memory` は自分のもの（無ければ共通のもの）だけが対象で、
  他のユーザーのものは拒否する（ランクが上でも他のユーザーのメモリーは書き換えられない）。
- **ユーザーのランク**（`[memory] user_ranks`・`default_rank`）: 組織で使うときに、上司のメモリーを部下のものより
  優先させるための数字（0以上の整数。大きいほど上位）。設定ダッシュボードの「ユーザー」画面のランクの列でも設定でき、
  `[memory] user_ranks` に `{"ユーザー名": ランク}` として保存される（パスワードと違い機密ではないため、プロジェクト
  直下 `.env` から継承中のユーザーにも設定できる。インスタンスの再起動後に効く）。ランクを1人でも設定すると、
  一覧・索引は所有者のランクが高い順に並び（同じランクの中は上の 自分 → 共通 → 他のユーザー の順）、各行に
  `rank N` が付く。共通のメモリーと、`user_ranks` に無いユーザーのランクは `default_rank`（既定1）。
  内容が食い違うメモリーは、所有者のランクが高い方に従う（同じなら自分のもの）よう、システムプロンプトと
  `read_memory` の注記（相手と自分のランクを示す）で LLM に指示する。`user_ranks` が空なら従来どおり自分の
  メモリーを優先し、ランクは表示しない。
- **作成者・更新者の記録**: メモリーの frontmatter に、作ったユーザー（`author`）と、`update_memory` で最後に本文を
  書き換えたユーザー（`updated_by`）を残す（共通のメモリーをだれが直したか分かるように）。`read_memory` は、
  所有者と違う作成者・更新者がいれば `Author:`・`Last updated by:` として表示する。`author` の無い既存の
  ユーザーのメモリーは所有者が作ったものとみなす。
- **ファイルを直接開かせない**: 他のユーザーのメモリーフォルダ（`data/memory/users/<他のユーザー>/`）は、
  `Read`/`Grep`/`Glob`/`execute_python_code` 等からも読めない（`src/skill_drafts.py` の `access_error`・
  `unreadable_dirs` 等。他のユーザーのドラフトスキルを隠す仕組みと同じ）。
- **索引 `MEMORY.md`**: 共通の分は `data/memory/MEMORY.md`、ユーザーの分は `data/memory/users/<ユーザー>/MEMORY.md`。
  create/update/delete のたびに全再構築される（差分更新はしない設計）。
  システムプロンプトの `{{memory}}` には、会話のユーザーから見えるメモリーの一覧（自分 → 共通 → 他のユーザーの
  共有のもの）が、会話の開始（グラフの組み立て）ごとに差し込まれる（`app.py` の `_apply_session_draft_skills`。
  本文は `read_memory`/`search_memory` で都度取得）。新しく保存したメモリーは次の会話から載る。
- 200行を超える場合はシステムプロンプトへの差込時に切り詰められる（自分のものが先に残る。`src/memory.py`
  の `render_memory_block`）。
- **元ファイル付きのメモリー（`source_files`）**: ファイルから読んだ値・表の行・ページ付きの要点を
  記録するときは、`create_memory`/`update_memory` の `source_files` に元ファイル（絶対パスまたは `@N`）を
  渡す。ツールがその時点の sha256 とサイズを frontmatter の `sources` に記録する（最大10ファイル、
  本文は4000文字まで）。`read_memory` は本文の後ろに「[Source files]」として元ファイルごとの照合結果
  （`unchanged`／`CHANGED`／`MISSING`）を付ける。サイズが違えば変わったとみなし、同じなら sha256 を
  計算して比べる（更新日時は見ない）。全部 `unchanged` なら、LLM は元ファイルを開き直さずに内容を使い、
  そのファイルを出典として答える。ナレッジで一度調べた値を、次から explore に委譲せずに答えるための仕組み。
  `update_memory` で `source_files` を省くと記録済みの `sources` をそのまま残し、`[]` を渡すと外す。
  ハッシュが保証するのは「元ファイルが記録した時点から変わっていない」ことだけで、記録した内容の
  読み取りが正しいことは保証しない（間違って記録した内容は、ファイルが変わるまで信用され続ける。
  気づいたら該当のメモリーファイルを直すか消す）。
- `dispatch_agent` のサブエージェントは、種別ごとに絞ったメモリー系ツールを持つ（`explore`・`verifier` は読み取り系だけ、
  `worker` は6つすべて。`agents/*.md` の `tools:`）。サブエージェントも会話のユーザーとして動く。
- **設定ダッシュボードでの編集**: インスタンス詳細の「メモリー」サブタブで、全ユーザー・共通のメモリーを公開範囲に関係なく
  一覧・閲覧・作成・編集（説明・種類・本文・公開範囲）・削除できる（`admin/memories.py`。書き換えは `src/memory.py` の
  `edit_memory` で、`created` と `sources` は残し、索引も再構築する。公開範囲の規則は上と同じで、そのインスタンスの
  `[memory]` の設定を使う）。所有者で絞り込める。
  一覧のチェックボックスで表示中のメモリーを選んでまとめて削除できる（1件でも見つからなければ何も削除しない。
  絞り込みを変えると選択は外れる）。
  同じく選んだメモリー（最大20件）から**ドラフトスキルを作成**できる（「選択したメモリーからドラフトスキルを作成」。
  `admin/memories.py` の `draft-skill`）。LLM（下の「AIで翻訳」と同じ接続先）がメモリーの内容を1つの SKILL.md に
  まとめ、作成先ユーザーのドラフト置き場 `[skill_creator] draft_dir/<ユーザー>/<スキル名>/` へ skill-creator の
  `scaffold_skill.py` と同じ構成（`references/`・`evals/`・`_draft_meta.json`。元のメモリーは `source_memories` に記録）で保存する。
  作成先ユーザーは既定 `anonymous` で、ログインユーザー・既存のドラフト／メモリーの所有者から選べる。スキル名は
  空欄なら LLM が決める（正式スキル・既存のドラフトと同じ名前は拒否）。ドラフトはそのユーザーの次のメッセージから
  会話で使え、正式化はほかのドラフトと同じくスキル研究室で行う。他のユーザーの個人用のメモリーを選ぶと、その内容が
  作成先ユーザーのドラフトに入る（作成後に件名を表示する）。変更履歴には `memory_draft_skill` で残る。
  編集画面の「AIで翻訳」は、説明と本文をダッシュボードの表示言語（日本語 / English）へ LLM に訳させて入力欄へ
  入れるだけで、保存はしない（確かめてから「保存」を押す）。LLM は「AIに相談」と同じく「管理者設定」の「回答に使う LLM」
  （空なら対象インスタンス自身）の `[llm]` 接続先を使う。編集・削除の前の内容は
  `instances/<インスタンス名>/backups/memory/<所有者（共通は _common）>/<メモリー名>/` に `[admin] backup_keep` 世代まで残り、
  変更履歴に `memory_create`／`memory_update`／`memory_delete` として記録される。変更は次の会話から反映される
  （稼働中でも再起動は要らない）。

保存・削除の判断基準（何を保存すべきで何を保存すべきでないか）は
`system_prompt/system_prompt.md` の「Memory System」セクションに記載している。

### スレッド共有ノート（thread notes）との違い

上記の永続メモリーはスレッドをまたぐ長期記憶用だが、これとは別に
**同一スレッド内に閉じた一時的な共有メモ**として `write_thread_note`/
`list_thread_notes`/`read_thread_note` の3ツールがある（主エージェント・
サブエージェント（`dispatch_agent`の全 agent_type）の両方が読み書き可能）。

- 保存先は `data/memory/` ではなく、作業ディレクトリ配下の
  `_tmp_<name>/_thread_notes.md`（`execute_python_code`/`run_script`の
  中間生成物と同じ一時領域。`write_scratch_note` が使う
  `_scratch_notes_<run_id>.md` の隣に置かれる）。
- 用途は「委譲先サブエージェントの調査結果を、最終回答を肥大化させずに
  他のエージェント（委譲元や以降の別のサブエージェント）へ引き継ぐ」こと。
  永続メモリーのように次回以降のスレッドへは引き継がれない。
- スレッド共有ノートは自動索引化されず、システムプロンプトにも差し込まれない
  （`list_thread_notes`で都度トピック一覧・文字数を確認してから読む設計。
  詳細は `system_prompt/subagent_common.md`・`system_prompt/system_prompt.md`
  参照）。
- ライフサイクルは `_tmp_<name>` 全体と同じで、`[default_workdir]`の
  `retention_days`/`cleanup_interval_hours`（本ドキュメント「設定リファレンス」
  参照）による自動削除の対象、または手動削除も可能。
- コンテキスト圧縮（要約）が発生すると、要約LLMが書き出し済みのトピックを
  要約に残すとは限らないため、`src/context_compaction.py` の `maybe_compact`
  がトピック一覧（`list_thread_notes`相当）を要約結果とは無関係に機械的に
  再注入する。これにより圧縮後もモデルはthread noteの存在・トピック名を
  見失わない（承認済み実行計画の再注入と同じ仕組み）。

---

## ベクターDB（社内文書の意味検索）

規程・手順書・仕様書等の社内文書をインスタンス単位で取り込み、LLM が意味の近い箇所を検索して回答に使えるようにする機能
（RAG）。既定では無効（`[vector_db] enabled = false`）。ロジックは `src/vector_db/` に集約し、LLM 向けには
`src/tools/vector_db_tools.py` の3ツール（`list_document_collections`/`search_documents`/`search_document_paths`、読み取り専用）を公開する。
文書の取り込みは利用者ではなく管理者が設定ダッシュボードから行う。
メインエージェントのシステムプロンプト（`system_prompt.md` の「Knowledge」節）には、会話のグラフを組み立てるたび
（新しい会話・再開時。`app.py` の `_rebuild_graph`）にコレクションの名前・説明・文書数の一覧を `{{document_collections}}` へ差し込み、
事実・数値・文書の内容を探すときは作業フォルダやWeb検索より先にナレッジを検索するよう指示している
（無効・未取り込みのときは「(none: ...)」が入る）。既定の `[main_agent_tool_guard] allow_entries` では、検索結果で
メインエージェントの会話履歴が膨らまないよう `search_documents`/`search_document_paths` をメインエージェントに渡さず
（`list_document_collections` だけを渡す）、検索は `dispatch_agent(agent_type="explore")` に任せるよう指示している
（explore・worker・verifier は3ツールとも持つ）。モデルがそれでも渡していないツールを本文に `<tool_call>` の書式で書いた場合は、
`src/llm/undeclared_tool_call.py` が tool_call に組み直し、ガードの「メインエージェントは呼べない。dispatch_agent へ委譲」の
エラーを返して委譲へ切り替えさせる（推論サーバーは渡していないツールの呼び出しを tool_calls にせず本文のまま返すため、
そのままでは `<tool_call>…` が最終回答として画面に出てターンが終わる）。
ナレッジは必要なファイルを素早く見つけるための索引であって、出典ではない。検索結果（チャンクの抜粋）でファイル・ページを
見つけたら元のファイルを開いて値を確かめ、回答の出典には元のファイル（フルパス・ページ等）を書くよう指示している。
検索結果にはコレクション名を載せない（載せるとモデルが「出典: <コレクション名>」と書き写すため）。

- **コレクション**: 取り込み元フォルダ1つに対応する文書群。インスタンス内の全ユーザーが検索できる。名前は半角英数字・`_`・`-`
  （48文字以内）で、日本語の説明は「説明」欄に書く（`list_document_collections` で LLM にも見せる）。
  定義（自動取り込みの時刻・曜日を含む）と取り込み済みファイルの記録（サイズ・更新日時・SHA-256・チャンク数）は、保存先によらず
  `[vector_db] dir`（既定 `data/<インスタンス名>/vector_db/`）の `catalog.sqlite` に置く（`src/vector_db/catalog.py`）。
- **保存先**（`[vector_db] backend`）: `sqlite_vec`（既定。同じフォルダの `vectors.sqlite` に sqlite-vec の `vec0` 仮想テーブルで
  保存、サーバー不要、`src/vector_db/sqlite_vec_store.py`）か `qdrant`（Qdrant サーバー、`src/vector_db/qdrant_store.py`）。
  対応できるチャンク件数の目安（インスタンス内の合計、1024 次元の bge-m3 の場合）は、sqlite-vec が **〜10万件で快適・
  〜30万件が実用の上限**（索引を持たず検索のたびに全件と比べるため、30万件で検索1回が1秒前後）、Qdrant が
  `qdrant_preset = none` で **〜100万件**、`1m`〜`10m` で **100万〜1000万件**（「Qdrant を使う」参照）。チャンクは
  `chunk_size = 800` なら文字の詰まった A4 1ページあたり 1〜2件で、`image_input = true` のページ画像は1ページ1件が、ファイルパス（`search_document_paths` 用）は1ファイル1件が別に加わる。
  どちらもコサイン類似度で比べる。本文のチャンクは保存先によらず、同じフォルダの `keywords/<コレクション名>.sqlite`（SQLite FTS5。コレクションごとに1ファイルで、
  検索・書き込みがほかのコレクションの大きさに左右されない）にもキーワード検索用に入れる（`src/vector_db/keyword_index.py`）。
  0.4.8 以前の全コレクションで1ファイルの `keywords.sqlite` は読まないため、更新後は各コレクションを「再構築」する（再構築後は `keywords.sqlite` を手で消してよい）。
- **取り込み**（`src/vector_db/ingest.py`）: 対応形式は `.pdf`（ページ単位）・`.docx`・`.xlsx`/`.xlsm`（シート単位）・`.pptx`
  （スライド単位、ノート含む）・`.md`/`.txt`/`.csv`（`[vector_db] include_extensions` で絞れる。Office の一時ファイル `~$*` は除く）。
  アプリ専用の拡張子でも中身がテキストの形式（`.step`・`.igs`・AutoForm の材料ファイル `.mat` 等）は、`[vector_db] text_extensions` に書くと
  `.txt` と同じくテキストとして取り込める（`include_extensions` にも書く。中身がバイナリ（NUL を含む）ならファイルパスだけを登録する）。
  Office ファイルは本文の段落・セル・図形の文字に加えて、次のものも本文として取り出す（`src/vector_db/extract.py`）:
  - `.docx`: テキストボックス・ヘッダー/フッター・脚注/文末脚注・コメント（本文の後ろに `[Text box]`・`[Header]` 等の見出しを付けて並べる）。
    本文は段落と表を文書の順に読み、コンテンツコントロールの中の文字・変更履歴で挿入した文字も含める（削除した文字は含めない）。
  - `.xlsx`/`.xlsm`: 図形（テキストボックス・オートシェイプ・グループ内の図形）の文字・グラフのタイトル（セルを参照するタイトルはセルの値）・
    コメント（スレッド形式のコメントと、従来形式の「メモ」の両方）。
    図形はアンカー（左上が乗っているセル）の行、コメントは付いているセルの行の後ろに `[Shape C5] …`・`[Comment B3] …` の形で差し込み、
    近くのセルと同じチャンクに入るようにする（セルではなく位置で置いた図形・位置の読めない図形はシートの末尾）。グラフシートは
    グラフのタイトルを1シートの区切りとして取り込む。古い形式のフォームのボタン（VML）は読まない。
    表の中に縦に並ぶ数値のセル（同じ列の真上か真下も数値のもの。月別の数量・金額等）は、検索の手がかりにならずチャンクを増やすだけのため
    取り込まない。見出し・品名などの文字、見出しが No・ID・番号・品番・コード等の列の数値、1行だけの「項目名＋値」、日付・時刻は取り込む。
  - `.csv`: 行・列に分けて読み、`.xlsx` と同じく表の中に縦に並ぶ数値のセルを除いて、1行ずつセルをタブでつないだ本文にする
    （CSV として読めないファイルはテキストのまま取り込む）。
  - `.pptx`: グループ化した図形の中の文字（入れ子のグループも）・グラフのタイトル。
  本文を `chunk_size` 文字以内のチャンクに分け（段落・改行の切れ目を優先し、隣と `chunk_overlap` 文字重ねる）、
  チャンクごとの埋め込みを保存する。2回目以降は**差分だけ**を処理する（サイズ・更新日時が同じファイルは読み直さず、
  内容が同じなら埋め込み直さない。フォルダから消えたファイルのチャンクは削除する）。読めないファイル（壊れている・パスワード付き等）・
  `max_file_mb` を超えるファイルは失敗一覧に載せて続行する（`max_file_mb` は llama-server ではなく Locohane 側の上限で、超えたファイルがあるとナレッジ画面に「`max_file_mb` を大きくして取り込み直す」案内を出す。失敗したファイルは次の取り込みでやり直す）。
  中身を埋め込めないファイル（本文の無い画像だけの PDF 等と、`image_input = false` の画像ファイル `.png`・`.jpg`・`.heic` 等）は、
  失敗にせず**ファイルパスだけを登録**し（中身は読まない・埋め込まない）、`search_document_paths` でファイル名・フォルダ名から
  探せるようにする（ナレッジ画面の結果に件数を出す。チャンクは0件として記録し、`image_input` を切り替えると取り込み直す）。
  `[vector_db] image_input = true` なら PDF・Office ファイルは本文に加えてページ画像も埋め込み、画像ファイルも画像として埋め込む
  （「ページを画像として取り込む（image_input）」参照）。
  取り込み元のフォルダの中のシンボリックリンク・ジャンクションのフォルダもたどるが、取り込み元の中・取り込み元を含むフォルダ・
  既にたどったリンクと同じ実体を指すものはたどらない（ループ・同じファイルの重複を避けるため）。
- **埋め込みモデルの固定**: コレクションには作成時の埋め込みモデル名と次元を記録する。`embedding_url` のモデルを変えると
  ベクターを比べられなくなるため、そのコレクションは検索対象から外れ（理由を LLM に返す）、取り込みも「再構築」
  （全ベクターを消して取り込み直す）を求めるエラーになる。取り込みに使った `document_prefix` も同じく記録し、設定と違えば
  同じ扱いにする（記録の無い旧版のコレクションは確かめない）。
- **検索**（`search_documents`、`src/vector_db/search.py`）: `collection` 省略時は全コレクションを横断する。次の検索を
  コレクションごとに別々に行い、点数ではなく順位で統合して上位 `top_k` 件返す（Reciprocal Rank Fusion。各一覧での順位 r に
  `重み / (60 + r)` を足した合計の高い順。重みは `rrf_weight_text`・`rrf_weight_keyword`・`rrf_weight_image`、0 で無効）。
  - **本文の埋め込み**: 意味の近いチャンク（類似度 `min_score` 以上）。
  - **本文のキーワード**: 検索語の語句を含むチャンクを BM25 の高い順に。埋め込みが苦手な型番・品番・数値の完全一致を拾う。
    日本語は単語に分けずに、漢字・かな等の連続を2文字ずつ（バイグラム）に区切って索引にし（ひらがなだけの2文字は助詞・
    語尾として除く）、英数字は語ごとに小文字で、全角・半角は NFKC でそろえる。
  - **ページ画像**: `image_input = true` のとき（「ページを画像として取り込む（image_input）」参照）。

  各一覧は `top_k` の4倍の候補を取る。結果は**同じファイル・同じ位置（ページ・シート・スライド。`.md`/`.txt`/`.csv` は
  ファイル全体）ごとに1件にまとめる**。1つの一覧の中では同じ位置のチャンクは最上位の1つだけを数えるため、1ページに
  大量のチャンクがあっても点数はチャンク数で増えず、複数の検索（本文・キーワード・画像）で一致した位置が上に来る。
  埋め込み・索引はチャンク（`chunk_size`）単位のままで、抜粋には最も一致したチャンクを使い、同じ位置で他に一致した
  チャンクは数だけ添える。各結果にはどの検索で一致したか（`matched: text/keyword` 等）を載せ、出典のファイルパスをパスメモリー（`@N`）付きで添えるため、原文は
  `@N` を `Read`（メインエージェントは `explore` への委譲）に渡して確認できる。結果全体は `max_result_chars` 文字で切り詰める。
  結果が小さく抑えられるため、メインエージェント自身も直接呼べる（`[main_agent_tool_guard] allow_entries` に登録済み）。
  サブエージェントは `explore`・`worker`・`verifier` が持つ。
- **ファイル名・フォルダ名での検索**（`search_document_paths`、`src/vector_db/search.py` の `search_paths()`）: 写真・図面・
  スキャン画像のように中身から内容を読み取れないファイルでも、ファイル名やフォルダ名に内容が書かれていることが多いため、
  取り込みでは各ファイルの**取り込み元フォルダからの相対パス**（例: `製品A/外観写真/溶接部_NG例.jpg`。取り込み元より上は
  全ファイル共通で手がかりにならないため含めない）を1ファイル1件で埋め込み（`document_prefix` を付け、本文のチャンクと同じ
  リクエストの末尾で送る）、別のコレクション `<コレクション名>.path` に入れる。同じ相対パスはキーワード索引
  （`keywords/<コレクション名>.sqlite` の、本文とは別の表。本文のキーワード検索には混ざらない）にも入れる
  （拡張子は `pdf`・`jpg` 等が同じ形式の全ファイルに一致してしまうため索引の語に含めない。結果の相対パスは拡張子付き）。
  検索ではパスの埋め込み（類似度 `path_min_score` 以上）とパスのキーワード（BM25。型番・品番・日付等の完全一致を拾う。
  語の区切り方は本文のキーワード検索と同じ）を別々に探し、`search_documents` と同じ式で順位を統合して（重みは埋め込みが 1.0、
  キーワードが `rrf_weight_path_keyword`。0 でキーワードを使わない）上位 `top_k` 件を、どちらで一致したか
  （`matched: meaning/keyword` 等）・出典のパス（`@N`）・相対パスを添えて返す（`search_documents` の結果には混ぜない）。パスが変わる（ファイル・フォルダの名前の変更・移動）と別のファイルとして
  取り込み直されるため、パスのベクターも追従する。取り込みの対象は対応形式のファイルで、本文の無い PDF・`image_input = false` の
  画像ファイルもパスだけは入る（読めずに失敗一覧に載ったファイルは入らない）。本文のチャンクが無くてもファイルがあれば
  検索の対象にする（画像ファイルだけのコレクションでも探せる。`search_documents` はそのコレクションを「本文のある文書がありません」として外す）。この機能の導入前に取り込んだコレクションは、次の「取り込み（差分）」で内容を埋め込み直さずに
  パスだけを埋め込む（取り込み済みファイルの記録 `path_indexed` で判定。キーワード索引に無いパスも同じ取り込みで補う）。

### 埋め込みサーバーの準備と設定

埋め込みはチャット用とは別の埋め込みモデルを llama-server で起動し、OpenAI 互換の `/v1/embeddings` で計算する
（`src/vector_db/embeddings.py`）。日本語を含む文書には多言語モデル（例: `bge-m3`・`multilingual-e5-large`）を使う。

```cmd
llama-server.exe -m D:\models\bge-m3-Q8_0.gguf --alias bge-m3 --embedding --pooling cls --port 12440 -c 8192 -b 8192 -ub 8192
```

- `--pooling` はモデルに合わせる（`bge-m3` は `cls`、`multilingual-e5` 系は `mean`）。`none` だとトークンごとのベクターが返り、
  取り込み時に「--pooling を none 以外にして起動してください」というエラーになる。
- 埋め込みは1チャンクを1回で処理するため、`-ub`（物理バッチ）を1チャンクのトークン数より大きくする。

`config.ini`（またはダッシュボードの config.ini タブ。「ベクターDB」プリセットで関係キーだけを表示できる）で有効にする:

```ini
[vector_db]
enabled = true
embedding_url = [{"base_url": "http://localhost:12440/v1", "api_key": "dummy-not-used", "model": "bge-m3", "provider": "llama_cpp"},]
```

`embedding_url` は `[llm] main_url` と同じ形式で、接続先ごとにベクターが変わらないよう、すべて同じ `model` にする必要があり、
`start`/`end` は指定できない。複数書いたときの選び方は `embedding_routing_strategy`（`[llm] main_routing_strategy` と同じ
`round_robin`（既定）・`random`・`priority_failover`）で決める。`round_robin`・`priority_failover` では、送る前に接続先の空きを確かめ、
埋まっていれば次の接続先へ回す（`provider = "llama_cpp"` は GET /slots の空きスロット数（このプロセスから送信中の分を差し引く）、
`provider = "vllm"` は GET /metrics の `vllm:num_requests_waiting` が 0 かで判断し、その他の provider・確かめられない場合は空きとみなす）。
全接続先が埋まっていれば、取り込みは空きが出るまで待ち（「停止」で止められる。待ち始めた順に空きを使い、このプロセスの送信が返ったらすぐ、
それ以外は `[llm] round_robin_busy_poll_interval_seconds` 秒（埋め込みは1回が短いため最大0.5秒）ごとに確かめ直す）、
検索は待たずに並び順の先頭へ送る。確かめのタイムアウトは `[llm] round_robin_slots_probe_timeout_seconds`。どの戦略でも、接続できない・
HTTP 200 以外が返ったときは同じリクエストを次の接続先へ送り直し、`priority_failover` ではエラーが返った接続先を60秒間後回しにする。
すべての接続先でエラーになったら `embedding_retry_interval_seconds`（既定10）秒待って全接続先を試し直し、これを
取り込みでは `embedding_max_retries`（既定3）回まで繰り返す。`embedding_max_retries = 0` なら成功するまで無制限に繰り返す
（埋め込みサーバーの再起動を待つ用途。ナレッジ画面の「停止」で止められ、待っていたファイルは記録せずに次回の取り込みへ回す）。
検索（`search_documents` の検索語の埋め込み）は止める手段が無いため、別の `search_embedding_max_retries`（既定2）回までとし、
こちらの `0` は「試し直さない」（無制限は指定できない）。408・429 以外の 4xx（入力・設定の誤りで待っても直らない）しか返らなかったときは
試し直さずにエラーにする（`src/vector_db/embeddings.py` の `_embed_batch()`）。

一部の埋め込みモデルは、検索語と文書の先頭に決まった目印（接頭辞）を付けないと精度が落ちる。`query_prefix`・`document_prefix` が
既定の `auto` なら `model` の名前から自動で決める（`src/config.py` の `auto_embedding_prefixes()`）。実際に付ける値はナレッジ画面に出る。

| `model` の名前 | 検索語（`query_prefix`） | 文書（`document_prefix`） |
| --- | --- | --- |
| `multilingual-e5` 系（`e5` を含む） | `query: ` | `passage: ` |
| `multilingual-e5-*-instruct` | `Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: ` | なし |
| `ruri-v3` 系 | `検索クエリ: ` | `検索文書: ` |
| `ruri`（v1・v2）系 | `クエリ: ` | `文章: ` |
| `nomic-embed` 系 | `search_query: ` | `search_document: ` |
| Nemotron の埋め込み系（`nemotron` と `embed` を含む） | `query: ` | `passage: ` |
| それ以外（`bge-m3` 等） | なし | なし |

表に無いモデル、または `model` に別名を付けていて自動判定が当たらない場合は、値を直接書く（空欄なら付けない。
末尾の空白を残すにはダブルクォートで囲む。例: `query_prefix = "query: "`）。`document_prefix` が変わると、既存の
コレクションは検索対象から外れて「再構築」を求める（`query_prefix` だけなら再構築は要らない）。

ベクター同士の近さの測り方（距離関数）は `distance` で選ぶ（`Cosine` 既定 / `Euclid` / `Manhattan` / `Dot`）。
sqlite-vec では `Euclid` が vec0 の `l2`、`Manhattan` が `l1` に当たり、`Dot` は対応していない（`Qdrant` のみ）。
`Euclid`・`Manhattan` は値が小さいほど近いため、検索結果の類似度を `1 / (1 + 距離)` に換算してから
`min_score`・`image_min_score`・`path_min_score` と比べる（`Cosine` とは値の出方が違うので下限を調整する）。
**距離関数はコレクションを作るときに決まるため、変えた後は既存のコレクションをすべて「再構築」する**
（再構築前の差分取り込みは「距離関数が違います」のエラーになり、検索は作ったときの距離関数のまま行う）。

### ページを画像として取り込む（image_input）

`[vector_db] image_input = true` にすると、`[vector_db] image_input_extensions` の形式（既定 `.pdf`。
`.docx`・`.pptx`・`.xlsx`/`.xlsm` も選べる）は本文のチャンクに加えて1ページずつの画像も埋め込む（`src/vector_db/render.py`）。
本文は型番・数値等の細かい語句に、画像は図・表・写真・スキャンした PDF のように文字を取り出せない内容に強いため、
両方を持たせて補い合う。`image_input_extensions` に含めない Office ファイル（既定 `.docx`・`.pptx`・`.xlsx`/`.xlsm`）は、
本文のチャンクに加えてファイルの中の画像（写真・図）を1枚ずつ埋め込む（`embedded_images`。下の「Office ファイルの中の画像」）。
`.md`/`.txt`/`.csv` は従来どおり本文だけを埋め込む。

Office を既定でページ画像にしないのは、取り込みで一番時間がかかるため。Office で PDF に書き出すので1ファイル数秒かかり
（Excel は実測で1シート0.2〜1秒）、Office を並列に増やしても3つ程度で頭打ちになる（Office の PDF 書き出しが
並列に伸びないため。子プロセス（`ingest_processes`）に分けても変わらない）。Windows と Microsoft Office も要る。
Office ファイルは本文・図形の文字・グラフのタイトル・コメントを文字として取り出せ、文字で探せない写真・図は
中の画像として埋め込めるため、ページ画像が無くても探せる（大きい Excel のシートは1ページに縮めると文字が潰れ、
ページ画像はもともと役に立ちにくい）。
画像ファイル（`.png`・`.jpg`/`.jpeg`・`.gif`・`.bmp`・`.webp`・`.tif`/`.tiff`・`.heic`/`.heif`）も画像として埋め込む
（`image_input = false` のときは中身を読まず、ファイルパスだけを登録する）。

- **画像化の方法**: render 系スキル（`docx-render`・`pptx-render`・`excel-render`）と同じく、Office ファイルは OLE（COM）で
  Word / PowerPoint / Excel をヘッドレス起動して PDF に書き出し、PDF のページを pypdfium2 で `image_dpi`（既定100）の
  PNG にする。Excel は表示中のシートごとに横1×縦1ページのフィット印刷で書き出し（印刷設定の変更は保存しない。
  非表示シート・空のシートは除く）、PowerPoint の非表示スライドは含めない。Office ファイルの画像化には Windows と
  Microsoft Office が必要で、使えない環境ではそのファイルが失敗一覧に載る。
  取り込み元のファイルのマクロ（`.xlsm` の `Workbook_Open` 等）は実行しない（`AutomationSecurity` を
  `msoAutomationSecurityForceDisable` にし、Excel はイベントも止める）。パスワード付きのファイルはダミーのパスワードを渡して
  入力画面で止めずに失敗一覧へ載せる（PowerPoint はそれでも止まることがある）。見えない確認画面（パスワード入力・
  「前回開いた時に重大なエラーが発生しました」等）で止まったときに備え、PDF への書き出しが `office_timeout_seconds`
  （既定180秒）を過ぎたらその Office のプロセスを終了し、そのファイルを失敗一覧に載せる（PowerPoint は PC で1プロセスのため、
  同時に変換していた他のファイルは新しく起動し直して1回やり直す）。
- **画像ファイル**: Pillow（HEIC/HEIF は pillow-heif）で開き、EXIF の回転を反映して PNG にそろえる（透過部分は白で埋める）。
  長辺は A4 の文書を `image_dpi` で画像化したときの長辺（100 DPI なら 1169px）まで縮小し、それより小さい画像は拡大しない。
  1枚が1チャンクで出典の位置は `画像`。複数ページの TIFF はページごとに分けて `p.N`、アニメーション GIF は先頭のコマだけを使う。
- **Office ファイルの中の画像**（`embedded_images = true`、既定）: Office を起動せず、ファイル（ZIP）の中の画像のパーツ
  （`word/media/image1.png` 等）を直接読み、画像ファイルと同じく PNG にそろえて1枚を1チャンクで埋め込む（`src/vector_db/embedded_images.py`）。
  Word は本文（表・テキストボックスの中も）、PowerPoint はスライド（図形の塗りつぶし・背景の画像も）、Excel はシートに置いた
  画像だけを読み、スライドマスター・レイアウト・ヘッダーの画像（ロゴ等）は読まない。幅か高さが `embedded_image_min_px`
  （既定 128px）より小さい画像（アイコン・箇条書きの記号等）と、同じ中身の画像の2枚目以降は埋め込まない。外部のファイルへの
  リンクの画像と、Pillow で開けない形式（SVG 等。SVG には PNG の代替が入っているのでそちらを読む）は飛ばす。
  出典の位置は PowerPoint が `Slide N image (ppt/media/image2.png)`、Excel が `Sheet: 名前 C5 image (xl/media/image1.png)`
  （画像の左上が乗っているセル）、Word が `image (word/media/image1.png)`。結果の本文抜粋には、画像の代替テキスト（`[Image] …`）と
  周りの文字（PowerPoint はそのスライドの文字、Word は画像のある段落とその前後の文字のある段落）を入れる。中身を確かめるときは、
  Office ファイルが ZIP なのでそのパーツを取り出して `analyze_image` で見るか、render 系スキルでそのスライド・シートを画像にする。
- **画像の代替テキスト**: `image_input` に関係なく、Word は画像の位置に `[Image: 代替テキスト]`、PowerPoint は `[Image] 代替テキスト`、
  Excel は置かれたセルの行に `[Image C5] 代替テキスト` として本文に入れる（代替テキストの無い画像は何も入れない）。
- **チャンク**: 本文は `image_input = false` のときと同じチャンクにする。ページ画像は1ページ（Excel は1シート）が1チャンクで、
  出典の位置は PDF・Word が `p.N`、PowerPoint が `スライドN`、Excel が `シート: 名前`。ページ画像の結果の本文抜粋には、
  PDF のテキスト層から取ったそのページの文字（`chunk_size` 文字まで）を使う。
- **保存先**: 本文と画像は類似度の出方が違う（検索語はテキストのため、画像との類似度は低めに出る）ので、ページ画像の
  ベクターは別のコレクション `<コレクション名>.image`（Qdrant では接頭辞付き）に入れる。ページ画像の PNG は取り込みの間だけ
  一時フォルダ（`[vector_db] ingest_temp_dir`。既定は `%TEMP%`。その下の `locohane_vdb_pages_*`）に書き出し、埋め込むときに1枚ずつ読んで、ファイルを書き込んだら消す
  （400ページの PDF 等でも全ページの画像をメモリーに持たないため。停止・エラーで終わったときも取り込みの最後に消す）。
  出典は元のファイルとページ位置だけで、図表の中身を確かめるときは render 系スキル（`pdf-tools`・`docx-render` 等）で
  そのページを画像にしてから `analyze_image` で見る（画像ファイルは出典の `@N` をそのまま `analyze_image` に渡す）。
- **検索の統合**: ページ画像は `image_min_score` 以上のものを、本文の埋め込み・キーワードの結果と順位で統合する
  （「ベクターDB（社内文書の意味検索）」の「検索」参照）。同じページが本文と画像の両方で一致すれば1件にまとめて合算する。
- **埋め込みモデル**: 画像入力に対応したマルチモーダルの埋め込みモデル（例: Qwen3-VL-Embedding）を、llama-server に
  `--mmproj` を付けて起動する。画像は llama-server の `/v1/embeddings` の画像入力形式
  （`{"input": [{"content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}]}]}`）で、
  `embedding_batch_size` によらず1枚ずつ送る（`document_prefix` は付けない）。検索語はテキストのまま埋め込むため、
  同じモデルが文字と画像を同じベクター空間に埋め込める必要がある。テキスト専用のモデル（`bge-m3` 等）では使えない。

```cmd
llama-server.exe -m D:\models\Qwen3-VL-Embedding-2B-Q8_0.gguf --mmproj D:\models\mmproj-Qwen3-VL-Embedding-2B-f16.gguf --alias qwen3-vl-embedding --embedding --pooling last --port 12440 -c 8192 -b 8192 -ub 8192
```

- **切り替え**: 取り込み済みファイルの記録には埋め込み方（`text` / `text+image` / `text+embedded`）も残すため、`image_input`・`image_input_extensions`・`embedded_images` を切り替えて
  「取り込み（差分）」を押すと、内容が変わっていなくても該当する形式のファイルだけを新しい方式で取り込み直す
  （false に戻すとページ画像のベクターは消え、画像ファイルはファイルパスだけの登録になる）。
  埋め込みモデルも変えた場合は、これまでどおり「再構築」が必要。

### 文書の取り込み（設定ダッシュボードの「ナレッジ」）

インスタンスのカードの「ナレッジ」ボタン（またはインスタンス詳細の「ナレッジ」サブタブ）から操作する
（`admin/knowledge.py`・`admin/static/knowledge.js`）。

1. 「コレクションを追加」にコレクション名・説明・取り込み元フォルダ（管理ツールの PC から見える絶対パス。共有フォルダの
   UNC パスも可）を入れて追加する。取り込み元フォルダは1行に1つずつ書けば複数指定でき、すべてのフォルダの文書を
   1つのコレクションに取り込む（`catalog.sqlite` の `source_folder` 列に改行区切りで保存）。「サブフォルダも取り込む」ときは、
   別の取り込み元フォルダの中にあるフォルダは指定できない（同じファイルを2回取り込まないため）。取り込み元フォルダが
   1つでも見えない（共有フォルダに接続できない等）ときは、そのフォルダの文書を消さないよう取り込みを始めずにエラーにする。
   名前が `.` で始まる、または Windows の隠し属性が付いたファイル・フォルダ（`.git` 等）は
   取り込み対象外（取り込み元フォルダ自身は隠しでも可）。
   「取り込み除外キーワード」（1行に1つ。`catalog.sqlite` の `exclude_keywords` 列に改行区切りで保存）を書くと、
   取り込み元フォルダより下のパス（フォルダ名・ファイル名を含む）にそのキーワードを含むものを取り込まない
   （大文字小文字と、半角カナ・全角英数字・互換漢字の違いは区別しない（NFKC でそろえて比べる。ファイル名の全角「／」だけは
   区切りの `/` と区別する）。当てはまるフォルダの中はたどらない）。`/old/` のように `/` で囲むと名前がちょうど `old` の
   フォルダだけを除外できる（区切りは `\` でも可）。`*.xlsx` のように `*.` で始めると拡張子の指定になり、
   ファイル名がそれで終わるファイルだけを除外する（名前の途中に `.xlsx` を含むもの・フォルダには当てない）。取り込み元フォルダ自身のパスは比べない。キーワードを足すと、
   当てはまるようになった取り込み済みのファイルは次の取り込みで「削除」として消える。
   逆に「取り込み対象キーワード」（1行に1つ。`include_keywords` 列）を書くと、取り込み元フォルダより下のパスにどれかを含む
   ファイルだけを取り込む（空ならすべて）。書き方（大文字小文字・`/` で囲む・`*.pdf` の拡張子指定）は除外キーワードと同じ。
   ファイルにだけ当てる（フォルダ名に含めばその中のファイルが当てはまる。フォルダは絞らずにたどる）。除外キーワードにも
   当てはまるファイルは取り込まない（除外が優先）。対象から外れた取り込み済みのファイルも次の取り込みで消える。
2. 「取り込み（差分）」を押すと、管理ツールのプロセス内で取り込みが始まり、進捗（処理済みファイル数・処理中のファイル）・
   結果（追加・更新・変更なし・削除・失敗の件数と失敗理由）が表に出る。取り込み中は「停止」で中断できる
   （未処理のファイルは次回の取り込みで処理され、削除扱いにはならない）。取り込み中はインスタンス一覧のカードにも
   コレクション名・処理済みファイル数・進捗バーが出る（クリックでナレッジ画面を開く）。処理済みファイル数は
   ファイルの記録（埋め込みまで終わって保存した時点）ごとに進む（変更なし・失敗のファイルも数える）。
   取り込み中の画面は5秒ごとに、ジョブと取り込みの枠の状態だけを返す `GET /api/instances/{name}/knowledge/jobs` で更新する
   （大量の取り込みを同時に動かしても重くならないよう、設定・`catalog.sqlite` の件数の集計・ほかのインスタンスの参照は読まない）。
   「文書」の件数・最終取り込み等は、取り込みが始まった・終わったときに一覧を取り直して更新する。
   コレクションの一覧は、列の見出し（コレクション・取り込み元フォルダ・文書・埋め込みモデル・最終取り込み・自動取り込み・状態）
   を押すと並べ替えられる（同じ見出しを押すたびに昇順 → 降順 → 登録順。選んだ並びはブラウザに保存する）。
3. 文書を追加・更新したら、もう一度「取り込み（差分）」を押すか、下記の自動取り込みを設定しておく。埋め込みモデル・`document_prefix` を変えたときは「再構築」。

#### 時間指定の自動取り込み

コレクションの追加フォーム・「編集」ダイアログで「時間指定で自動取り込み（差分）を行う」にチェックを入れ、時刻（24時間表記、
`02:00` や `07:30, 19:00` のようにカンマ区切りで最大24個）と曜日（選ばなければ毎日）を指定すると、その時刻に
「取り込み（差分）」と同じ処理が自動で始まる。一覧の「自動取り込み」列に設定と次回の予定日時が出て、自動で始まった取り込みは
状態が「完了（自動）」のように表示される。

- 実行するのは管理ツール（`admin.bat`）のプロセス内のスケジューラー（`admin/knowledge.py` の `IngestScheduler`）で、
  管理ツールが起動している間だけ動く。管理ツールが止まっていた間の時刻の分は、起動後にさかのぼって実行しない
  （PC のスリープ等で確認が遅れたときは、10分以内の遅れなら実行する）。
- その時刻にそのコレクションが取り込み中（手動・自動とも）なら、その回は実行しない。
- `[vector_db] enabled = false` のインスタンスでは実行しない。再構築は自動では行わない。
- 設定は `catalog.sqlite` の `collections` 表（`schedule_enabled`・`schedule_times`・`schedule_weekdays`）に保存する
  （旧版の `catalog.sqlite` には列を自動で追加する）。自動取り込みの開始は変更履歴（監査ログ）に操作者 `scheduler` で残る。

取り込みは `src/vector_db/ingest.py` の `_Pipeline` で、走査 → 準備 → 埋め込み → 書き込みの流れで進める。

- **走査**: 差分取り込みでは毎回、取り込み元の全フォルダを読んで変わったファイルを探す。`[vector_db] scan_workers`（既定8）個の
  スレッドで複数のフォルダを同時に読む（ファイルサーバー上のフォルダは1フォルダごとの往復の待ち時間が大きいため）。
  見つかったファイルが `max_files_per_collection`（既定100万）を超えたら、上限までのファイルだけを取り込み、ナレッジ画面に
  警告（見つかった件数・取り込まなかった件数と、「管理者設定」の `max_files_per_collection` で上限を変えられること）を出す。
  `scan_workers`・`max_files_per_collection` は全インスタンス共通で「管理者設定」で変える（`max_file_mb`・`office_timeout_seconds`・`ingest_temp_dir` も同じ）。
  選ぶファイルは取り込み済みのものを優先し、残りの枠は新しいファイルのパス順の先頭から埋めるため、取り込みのたびに入れ替わらない。
  上限を超えて取り込まなかったファイルは、フォルダにある限り削除扱いにしない。取り込み中は対象のファイル・取り込み済みの
  記録をメモリーに持つため（100万件で数 GB 程度）、大きな文書群はコレクションを取り込み元のフォルダごとに分けるとよい。
- **準備**（ファイルの読み取り・本文の抽出・ページの画像化）: `[vector_db] ingest_workers`（既定1）個のスレッドで並列に行う。
  `ingest_processes`（既定0）を1以上にすると、その数の子プロセスで行う（抽出・画像化は CPU を使い、Python のスレッドでは
  同時に1つしか進まないため、PDF・Excel 等が多く CPU のコアに余裕があるときに速くなる。子プロセスの起動に数秒かかる）。
  子プロセスが異常終了した（壊れた PDF で PDFium が落ちた等）ときは、そのとき準備中だったファイルを新しい子プロセスで
  1つずつ準備し直し、1つだけでも落ちるファイルを失敗一覧に載せて続ける（スレッドで準備しているとき＝`ingest_processes = 0`
  では管理ツールごと落ちるため、出どころの分からないファイルが多い取り込み元では1以上にしておくと安全）。
  サイズ・更新日時が前回と同じファイルは読まずに済ませ、変わったファイルは中身を1回だけ読んで SHA-256 の計算と本文の抽出に使う。
  ファイルは並列数によらず小さいものから始める（多くのファイルを早く検索できるようにするため）。PDF の本文・ページの画像化は pypdfium2 で行い、
  同じプロセスの中では1つずつ行う（PDFium は複数スレッドから同時に呼べない）。`image_input = true` の Office ファイルは、
  スレッド（子プロセス）ごとに Word / PowerPoint / Excel を1つずつ起動して取り込みの間使い回し、終わったら終了する。
- **埋め込み**: 複数のファイルのテキスト（本文のチャンクとファイルパス）を `embedding_batch_size` 件ずつにまとめ、
  `ingest_embed_requests` 個（既定0＝`ingest_workers` と同じ）まで同時に送る（大きいファイルのチャンクも複数のバッチを
  同時に送る。画像は1枚ずつ。1ファイルは複数のリクエストに分かれるため、ファイル数より多くしてよい）。枠が空いていれば
  件数が足りなくても空いている枠へ均等に分けて送るため、埋め込みサーバーを遊ばせず、埋め込みが追いつかないときほど
  まとまったバッチになる。埋め込みサーバーへの接続は同時に送る数だけ閉じずに使い回す（閉じて作り直し続けると、Windows で
  ソケットのバッファーが尽きて WinError 10055 になるため）。同時に送って速くなるのは推論サーバーが同時に複数の
  リクエストを処理できる場合だけで、llama-server なら `--parallel`（`-np`）を `ingest_embed_requests` 以上にして起動する。
  埋め込みは `embedding_encoding_format = base64`（既定）で float32 のバイト列として受け取る（数値の JSON より小さく解析が速い。
  対応しない接続先は自動で数値の配列に切り替える）。
- **書き込み**: カタログ・ベクター（sqlite-vec / Qdrant）・キーワード索引への書き込みは書き込みの専用のスレッドで1ファイルずつ行い
  （メインスレッドは書き込み・Qdrant の応答を待たずに次の埋め込みを送る。書き込みが追いつかないときは256ファイル分までためて待つ）、
  約2秒（または500ファイル）ごとにまとめてコミットする（SQLite は WAL・`synchronous=NORMAL`。Qdrant は `points/batch` で
  削除と追加をまとめて送る）。ベクター → キーワード索引 → カタログの順にコミットするため、途中で止まってもカタログに
  記録されたファイルのベクターは必ず保存されている（記録されていないファイルは次回の取り込みで処理する）。
  SQLite（カタログ・キーワード索引・sqlite-vec）へはその場では書かず、コミットのときに DB ごとに1つの短いトランザクションで
  まとめて書く。書き込みのロックを持つのは書いている間だけで、Qdrant への送信・埋め込みを待つ間は持たない（同時に動く別の
  コレクションの取り込みや、設定ダッシュボードでのコレクションの編集が「database is locked」で失敗しないように）。
  同じインスタンスの DB への書き込みはプロセス内で順番に並べ（`src/vector_db/sqlite_batch.py` の `instance_write_lock`）、
  SQLite の待ち時間（busy timeout）は 120 秒。別のプロセスのロックで失敗したときは、間隔を空けて最大 10 分やり直す。
- **チャンクの多いファイル**: 本文のチャンクが256件を超えるファイル（業務データの数十万行の CSV・ログ等）は、全部の
  ベクターがそろうのを待たず、番号順にそろった分から保存先へ足していく（`src/vector_db/ingest.py` の `_Job.stream`）。
  ベクターをメモリーにためないため、20万チャンクの1ファイルでもメモリーはファイルの大きさにほぼよらない（ためるのは
  番号のそろわないベクター 1024件程度まで。前の番号のバッチが再試行等で返らない間は、そのファイルの先のバッチを送らない）。
  古いチャンクは書き始めに消すため、取り込みの間はそのファイルの書きかけのチャンクが検索に出る。256件以下のファイルは
  従来どおり全部そろってから1回で入れ替える。コピー（「インスタンス間のコピー」）も、ベクター・キーワード索引を数百件ずつ
  読んで書く。本文の抽出・チャンク分割はファイル全体で行うため、本文の文字列（20万チャンクで数百 MB 程度）はメモリーに持つ。
- **強制終了の後の整合性**: Qdrant はコミットを待たずに256件ごとに送り、チャンクの多いファイルは分けて書くため、
  プロセスの強制終了・PC の停止で、保存先には書きかけ（または書き換えた）チャンクがあるのにカタログは前の記録のまま
  （新しいファイルなら記録が無い）、ということが起こりうる。そこで保存先へ書く前にファイルを取り込みジャーナル
  （`[vector_db] dir` の `ingest_journal.sqlite`、`src/vector_db/journal.py`）へ即時に記録し、カタログへの記録までコミットしたら消す。
  次の取り込みの開始時に残っているファイルは、カタログに記録があれば必ず取り込み直し（内容を元に戻したファイルも
  「内容が同じ」として書きかけのまま残さない）、記録が無ければ保存先・キーワード索引から消す（フォルダから消えていても残らない）。
  再構築・コレクションの削除も、保存先を消す前に「消している途中」と記録し、カタログまで消し終えたら外す。途中で止まった
  コレクションは、次の「取り込み（差分）」が再構築として作り直す（削除はやり直せば消える）。
- **Qdrant への送り直し**: Qdrant へ接続できない・接続を切られた・タイムアウトした・一時的なエラー（5xx・408・429）が
  返ったときは、`[vector_db] qdrant_retry_interval_seconds`（既定10）秒待って同じリクエストを送り直す。回数は
  `qdrant_max_retries`（既定5。`-1` なら成功するまで無制限、`0` なら送り直さない。回数・間隔とも全インスタンス共通で「管理者設定」で変える）。
  送り直しを待つ間も「停止」で止められ、そのときはエラーにせずコミットしていない分を捨てて止まる（次回の取り込みで処理する）。
  送り直しても結果が変わらないよう、Qdrant への更新は冪等にしている（ポイントの ID はファイルのパスとチャンクの番号から決まり、
  削除はファイルのパスで絞り込む。コレクションの作成は、タイムアウトの後に送り直して「既にある」と返ったら作成済みを確かめる）。
  検索とコレクションの削除は止める手段が無く応答が返らなくなるため、この設定によらず最大1回（間隔は最大1秒）だけ送り直す。
  検索の1リクエストのタイムアウトは `qdrant_timeout_seconds` によらず最大30秒、接続のタイムアウトは最大10秒。
  コレクションの削除は始まると止められないため、`-1` のときは Qdrant が戻るまで待ち続ける（管理ツールを止めても上記のとおり整合は保たれる）。
- **停止**: 「停止」を押すと新しいファイル・バッチを始めず（全ファイルの準備を始めた後でも、残りの埋め込みを送らない）、
  送信中の埋め込みが返るのを待って、埋め込みまで終わったファイルだけを記録して止まる（ボタンは押した時点で「停止中…」になる）。
- **並列数の共有**: `ingest_workers`・`ingest_processes`・`ingest_embed_requests` は全インスタンス共通の設定で（インスタンス別の上書きはできず、
  設定ダッシュボードの「管理者設定」か `config.ini` で変える）、設定ダッシュボードの取り込みは全インスタンス・全コレクションの**合計で**この並列数までしか
  準備・埋め込みを動かさない（複数のコレクションを同時に取り込んでも、埋め込みサーバー・PC への負荷は1つのときと同じ）。
  超える分は空きを待ち、空いた枠はその種類（準備・埋め込み）の枠を使っている数の少ない取り込みから使う（取り込み中のコレクションに
  均等に分ける。空きを待つ取り込みが無ければ1つの取り込みが全部を使える。`src/vector_db/ingest.py` の `IngestSlots`）。
  並列数の変更は次に始める取り込みから反映する（管理ツールの再起動は不要）。
- **ワーカーの表示**: ナレッジ画面の上部に、全インスタンス共通の取り込みの状態を「準備」「埋め込み」ごとに出す。
  アイコンは1つが1ファイル（準備の空き＝灰の輪、準備中＝回る青の輪、空き待ち＝点滅する点線の輪。ほかのインスタンスの
  取り込みの分は薄く出す。アイコンにマウスを載せると処理中のファイルが出る）。埋め込み中のファイルは、埋め込み済みの割合
  だけ緑で塗った点で、リクエストを送信中なら脈打つ。送信中なのに60秒以上進まないファイルは黄色で点滅させ、
  ほかのファイルより優先してアイコンに出す（詰まっているファイルを見つけるため）。
  埋め込みのアイコンの数は取り込みの並列数（`ingest_workers`）で、空きは灰の輪。あわせて、送信中の埋め込みのリクエスト数（「リクエスト 7/32」）を出す。
  各コレクションの「状態」欄とインスタンス一覧のカードには、その取り込みの分だけを出す。
- **削除の順番待ち**: コレクションの削除は、同じインスタンスで取り込み・コピーが動いている間は「削除待ち」にして、
  それらが終わってから行う（キーワード索引・カタログ等は同じインスタンスのコレクションで共有するため、取り込み中に消すと
  「database is locked」になる）。削除待ちの間に始めた同じインスタンスの取り込みは、削除の後に始める（「待機中」）。
  削除待ちは「削除を取りやめ」で取りやめられる。

取り込みは対象インスタンスの設定（`config_overrides.json` を重ねたもの）で行う（並列数だけは全インスタンス共通の `config.ini` の値）。
本体インスタンスが起動中でも取り込みと検索は同時に行え、本体の再起動は不要。コレクションの作成・取り込み・削除は変更履歴（監査ログ）に残り、
取り込み・削除の経過（開始・終了・件数・失敗したファイル・エラーの詳細）は管理ツールのログ `instances/_admin_logs/admin_*.log` に出る。
同じ操作はコマンドラインからも行える（コレクションの作成はダッシュボードで行っておく。コマンドラインの取り込みは
管理ツールの取り込みとは枠を共有せず、それだけで config.ini の並列数まで動く）:

```cmd
python -m src.vector_db.ingest --instance default --collection manuals
python -m src.vector_db.ingest --instance default --collection manuals --rebuild
```

#### インスタンス間のコピーと参照

別のインスタンスでも同じ文書群を使いたいときは、取り込み直さずに次のどちらかを使える（スキル研究室のインスタンスは対象外）。

- **コピー**（コレクションの「コピー」ボタン）: コピー先のインスタンスとコレクション名（既定はコピー元と同じ名前。コピー先の
  コレクション・参照と同じ名前は不可）を選ぶと、ベクター（本文・ページ画像・ファイルパス）・キーワード索引・取り込み済みファイルの
  記録までを複製する（`src/vector_db/collection_copy.py`）。コピー先では埋め込み直さず、そのまま検索でき、以後の
  「取り込み（差分）」も変わったファイルだけを処理する。保存先（`sqlite_vec` / `qdrant`）はコピー元と違ってもよい。
  - ベクターを使えるのは、コピー先の埋め込みモデル（`embedding_url` の `model`）・`document_prefix`・`distance` がコピー元の
    コレクションと同じ場合だけ。違うときは定義（説明・取り込み元フォルダ・サブフォルダの有無）だけをコピーし、
    コピー先で「取り込み（差分）」を実行する（ダイアログに理由が出る）。
  - 自動取り込みの設定は既定では引き継がない（同じフォルダを2つのインスタンスで自動取り込みしないため。ダイアログで選べる）。
  - コピーはコピー先のコレクションのジョブとして管理ツールのプロセス内で動き、進捗はコピー先のナレッジ画面に出る。
    コピー中はコピー元のコレクションの取り込み（手動・自動とも）・削除はできない。「停止」で止めると、そこまでのファイルを
    残して終わる（残りはコピー先での「取り込み（差分）」で補われる）。変更履歴には `knowledge_collection_copy` で残る。
- **参照**（ナレッジ画面の下の「他のインスタンスのナレッジを参照」）: 別のインスタンスのコレクションを、複製せずに
  このインスタンスの検索対象に加える。参照名（このインスタンスでのコレクション名で、LLM にもこの名前で見せる）は
  このインスタンスのコレクション・他の参照と同じ名前にできない。参照の定義は自分の `catalog.sqlite` の `collection_refs` 表に置き、
  検索のたびに参照元のインスタンスの設定（config.ini＋`instances/<参照元>/config_overrides.json`）の保存先・キーワード索引を
  直接読む（`src/vector_db/references.py`・`src/vector_db/search.py`）。
  - 取り込み・更新は参照元のインスタンスで行い、参照する側は読むだけ。参照元の再取り込みは参照する側にもそのまま反映される。
  - 検索語はこのインスタンスの埋め込みモデルで埋め込むため、参照元のコレクションもこのインスタンスと同じ埋め込みモデル・
    `document_prefix` で取り込まれている必要がある。違う・参照元のインスタンスやコレクションが無い・参照元の
    `[vector_db] enabled = false` のときは、その参照を検索から外して理由を LLM に返す（ナレッジ画面の状態列にも出る）。
    閾値（`min_score` 等）・RRF の重み・`top_k` はこのインスタンスの設定を使う。
  - 参照元の Qdrant の API キーは、参照元の `instances/<参照元>/.env` に `QDRANT_API_KEY` があればそれを使う。
    それ以外の環境変数（`VECTOR_DB_DIR` 等）による上書きは参照する側のプロセスのものが効くため、参照元の保存先は
    環境変数ではなく config.ini・設定画面で指定しておく。
  - 参照されているコレクションを削除するときは、確認ダイアログに参照しているインスタンスが出る（削除はでき、
    参照している側では「参照元がありません」として検索から外れる）。参照の追加・削除は変更履歴に
    `knowledge_reference_add`・`knowledge_reference_delete` で残る。

### Qdrant を使う

`[vector_db] backend = qdrant`・`qdrant_url = http://localhost:6333` にすると、ベクターを Qdrant サーバーに保存する
（REST API を httpx で直接呼ぶため、追加のパッケージは不要）。API キーが必要なサーバーは、そのインスタンスの `.env`
（ダッシュボードの「環境変数」）に `QDRANT_API_KEY` を書く。

- Qdrant 上のコレクション名は `qdrant_collection_prefix`（既定 `locohane_${instance}_`）＋コレクション名になる。
  この接頭辞で始まらないコレクションには一切触れないため、他の用途と同じ Qdrant サーバーを共有できる。
  インスタンスごとに接頭辞が変わるため、インスタンス同士の文書も混ざらない。
- コレクションの定義と取り込み済みファイルの記録は Qdrant ではなく `[vector_db] dir` の `catalog.sqlite` に置く。
  `backend` を切り替えた場合は、各コレクションを「再構築」して新しい保存先へ取り込み直す。
- `[vector_db] distance = Dot`（内積）は Qdrant でだけ使える（sqlite-vec は未対応）。距離関数の選び方は
  距離関数の選び方は「埋め込みサーバーの準備と設定」を参照。
- `[vector_db] qdrant_preset` で、想定するチャンク件数（1コレクションの本文または画像）の規模に合わせたコレクションの設定を選べる
  （中身は `src/vector_db/qdrant_store.py` の `QDRANT_PRESETS`）。既定の `none` は何も指定しない（Qdrant の既定。ベクターを
  すべて RAM に置く）。`1m`〜`10m` はどれもベクターを int8 にスカラー量子化した複製を RAM に置いて探し、上位の候補を元の
  ベクターで再採点する（チャンク本文の payload はディスク）。RAM の目安は 1024 次元の場合で、次元に比例して増える。
  ディスクは 100万件あたり約 8GB（SSD 推奨）。

  | `qdrant_preset` | 想定件数 | HNSW `m` / `ef_construct` | 検索時 `hnsw_ef` | 元のベクター | セグメント数 | 再採点の oversampling | RAM の目安（サーバーの推奨） |
  | --- | --- | --- | --- | --- | --- | --- | --- |
  | `none` | 〜100万件 | Qdrant の既定（16 / 100） | Qdrant の既定 | RAM | Qdrant の既定 | なし（量子化しない） | 100万件あたり約 4GB＋索引 |
  | `1m` | 〜100万件 | 16 / 128 | 128 | RAM | 4 | 1.5 | 約 6GB（8GB 以上） |
  | `2m` | 〜200万件 | 16 / 200 | 160 | ディスク（mmap） | 6 | 2.0 | 約 3GB＋ディスクキャッシュ（8GB 以上） |
  | `3m` | 〜300万件 | 24 / 256 | 200 | ディスク（mmap） | 8 | 2.0 | 約 4GB＋ディスクキャッシュ（16GB 以上） |
  | `4m` | 〜400万件 | 32 / 256 | 256 | ディスク（mmap） | 8 | 2.5 | 約 5GB＋ディスクキャッシュ（16GB 以上） |
  | `5m` | 〜500万件 | 32 / 320 | 288 | ディスク（mmap） | 10 | 2.5 | 約 6GB＋ディスクキャッシュ（16GB 以上） |
  | `6m` | 〜600万件 | 40 / 320 | 320 | ディスク（mmap） | 10 | 2.5 | 約 8GB＋ディスクキャッシュ（32GB 以上） |
  | `7m` | 〜700万件 | 40 / 384 | 352 | ディスク（mmap） | 12 | 3.0 | 約 9GB＋ディスクキャッシュ（32GB 以上） |
  | `8m` | 〜800万件 | 48 / 384 | 384 | ディスク（mmap） | 12 | 3.0 | 約 11GB＋ディスクキャッシュ（32GB 以上） |
  | `9m` | 〜900万件 | 48 / 448 | 416 | ディスク（mmap） | 16 | 3.0 | 約 13GB＋ディスクキャッシュ（32GB 以上） |
  | `10m` | 〜1000万件 | 64 / 512 | 448 | ディスク（mmap） | 16 | 3.0 | 約 15GB＋ディスクキャッシュ（32GB 以上） |

  新しく作るコレクションには作るときに、既存のコレクションには次の「取り込み（差分）」の開始時に（変わったファイルが
  無くても）設定を更新する（`PATCH /collections/{name}`。埋め込み直しは要らず、Qdrant が裏で索引・量子化を作り直す間も
  検索できる）。`none` に戻しても既存のコレクションの設定は戻らない（戻すには再構築する）。1000万件を超える場合は
  Qdrant のクラスター（シャーディング）を検討する（Locohane 側の設定は無い）。
- インスタンスを削除してもデータフォルダの `catalog.sqlite` が消えるだけで、Qdrant 上のコレクションは残る。
  不要なら先にナレッジ画面でコレクションを削除するか、Qdrant 側で `locohane_<インスタンス名>_*` を削除する。

---

## プロンプト資産の自動チューニングループ（evals/）

`system_prompt.md`・`SKILL.md`・ツール docstring などの「LLM に渡すプロンプト資産」を、
実際のローカル LLM（llama.cpp server）を動かして自動評価し、失敗があれば修正して
再評価する仕組み。`.claude/skills/tune-prompt/SKILL.md` の手順書に沿って
Claude Code から `/tune-prompt system_prompt` のように実行する。

- 評価ケースは `evals/cases/<target>/*.yaml`（現状 `system_prompt` に1件、`system_prompt_scale` に1件）。
  ルールベース判定（`expect`: 特定ツールの呼び出し有無・応答文字列の含有等）と、
  自由記述の `judge`（transcript を読んで合否判断させる）を併用できる。
- `python evals/run_all.py system_prompt` で全ケースを直列実行し、結果は
  `evals/results/<target>/<timestamp>/`（`.gitignore` 対象、再生成可能なデータ）へ出力。
- `--instance <インスタンス名>` を付けると、設定ダッシュボードのインスタンス
  （`config_overrides.json` とインスタンス別 `.env`）の設定で評価する（省略時は `default`）。
- **スキル安定化トライアウト**: `--repeat N` を付けると各ケースを N 回ずつ直列に実行し、
  サマリ末尾と `tryout.json` にケースごとの合格回数と判定（全ケース N/N で合格。judge 付きのケースは
  全回の judge を読んで判断する）を出す。1回の合格が偶然でないかを確かめるためのもので、
  ドラフトスキルの昇格（promote-skill）では必須。ケースと `--skill-overlay` のスキルは開始時に一時フォルダへ
  写して固定し、そのハッシュ（`cases_sha256`・`skill_overlays[].sha256`）も `tryout.json` に残す
  （`tryout.json` は `--repeat 1` でも出力する）。
- `--cases-dir <フォルダ>` で `evals/cases/<target>/` 以外のケース（ドラフトの `evals/` 等）を、
  `--skill-overlay <スキルフォルダ>` で本番のスキル構成の上にスキルを最優先で重ねて、
  `--results-dir` で結果の出力先を変えて実行できる。
- `evals/run_case.py` は Chainlit の UI 呼び出しを `evals/headless_chainlit.py` で
  スタブに差し替え、`src/graph.py` のグラフを直接 `ainvoke` する（Chainlit サーバー起動不要）。
- チューニング時は編集前スナップショットを `evals/history/<target>/` に退避し、
  変更内容と理由を `evals/tuning_log.md` に追記する。git へのコミットは行わない。
- 同じ仕組みを流用し、`config.ini` の timeout系設定（`request_timeout_seconds`
  等）を実行環境のスペックに応じて実測チューニングする `config_timeouts`
  ターゲットもある（`evals/analyze_timing.py` で推奨値を算出し、適用は手動）。

詳細は [`evals/README.md`](evals/README.md) を参照。

---

## 設定リファレンス（config.ini）

| セクション | キー | 意味 | 対応する環境変数 |
|-----------|------|------|------------------|
| `[llm]` | `main_url` | メインエージェント用のLLM接続先リスト（`[{"base_url":...,"api_key":...,"model":...}]` のJSON/Python風リスト形式、複数指定可。各要素に任意で `start`/`end`（使用可能時間帯、単位は時間、分は小数、必ずセットで指定）、`provider`（`openai_compatible`既定/`llama_cpp`/`vllm`。推論サーバーの種類で、拡張パラメータ名とthinkingのフィールド名をサーバーに合わせる（`src/llm/dialect.py`）。`llama_cpp`は`round_robin`/`priority_failover`戦略が選ぶ前にGET /slotsで空き確認する対象にもなる）を追加でき、リスト全体で最低1件は`start`/`end`両方省略した常時使用可能な接続先が必要） | `LLM_MAIN_URL` |
| `[llm]` | `main_routing_strategy` | `main_url` が複数件のときの選び方（`round_robin`/`random`/`priority_failover`。`round_robin`/`priority_failover`はprovider="llama_cpp"の接続先を選ぶ前にGET /slotsで空きを確認し、無ければスキップして次点へ、全滅なら待機する。選択から最初の送信までは同時に選択中の他の呼び出しが同じ空きを使わないよう予約する） | `LLM_MAIN_ROUTING_STRATEGY` |
| `[llm]` | `sub_url` | サブエージェント（`dispatch_agent`）用のLLM接続先リスト。形式は `main_url` と同じ | `LLM_SUB_URL` |
| `[llm]` | `sub_routing_strategy` | `sub_url` が複数件のときの選び方。形式は `main_routing_strategy` と同じ。サブエージェントにモデル指定（`agents/*.md` の `model` / `dispatch_agent` の `model` 引数）がある場合は、そのモデルの接続先だけを候補にこの規則で選ぶ（一致する接続先が無ければ `sub_default_model`、それも無ければ指定は無視） | `LLM_SUB_ROUTING_STRATEGY` |
| `[llm]` | `sub_default_model` | サブエージェントにモデル指定が無い（または指定モデルの接続先が無い・全て時間帯外の）場合に使うモデル名（`sub_url` の `model` と大文字小文字を区別せず照合）。`sub_url` に軽量・高性能モデルを混在させる場合に、未指定の委譲が無作為に振られるのを防ぐ。空欄なら従来どおりモデル名を見ずに選ぶ（一致する接続先が無い・全て時間帯外なら無視。名前の誤りは起動時に WARNING を1回出す）。**`sub_url` 未指定（メインの接続先を引き継ぐ設定）でも有効で、メインが別モデルの接続先を使っていると引き継がず、`main_url` のうちこのモデルの接続先から選ぶ** | `LLM_SUB_DEFAULT_MODEL` |
| `[llm]` | `temperature` | 生成のばらつき | `LLM_TEMPERATURE` |
| `[llm]` | `top_p` | 累積確率上位のみサンプリング（空欄で未指定） | `LLM_TOP_P` |
| `[llm]` | `top_k` | 上位k候補のみサンプリング（llama.cpp/vLLM拡張、空欄で未指定） | `LLM_TOP_K` |
| `[llm]` | `repeat_penalty` | 直近トークンの再出現抑制（llama.cpp拡張、`provider=vllm`へは`repetition_penalty`として送信、空欄で未指定）。thinking内の同一文言ループ抑制に有効 | `LLM_REPEAT_PENALTY` |
| `[llm]` | `frequency_penalty` | 出現済みトークン全体への一律ペナルティ（空欄で未指定） | `LLM_FREQUENCY_PENALTY` |
| `[llm]` | `presence_penalty` | 一度でも出現したトークンへの一律ペナルティ（空欄で未指定） | `LLM_PRESENCE_PENALTY` |
| `[llm]` | `max_tokens` | 1リクエストあたりの最大生成トークン数（空欄で無制限） | `LLM_MAX_TOKENS` |
| `[llm]` | `dry_multiplier` | DRYサンプラーの強度（llama.cpp拡張、`dry_*`は`provider=vllm`へは送らない、空欄で無効）。フレーズ単位の反復に効く | `LLM_DRY_MULTIPLIER` |
| `[llm]` | `dry_base` | DRYサンプラーの反復長に対するペナルティ指数増加率（空欄で未指定） | `LLM_DRY_BASE` |
| `[llm]` | `dry_allowed_length` | DRYサンプラーがこの文字数以下の反復を許容する閾値（空欄で未指定） | `LLM_DRY_ALLOWED_LENGTH` |
| `[llm]` | `dry_penalty_last_n` | DRYサンプラーが反復検出に遡って見るトークン数（空欄で未指定） | `LLM_DRY_PENALTY_LAST_N` |
| `[llm]` | `dry_sequence_breakers` | DRYサンプラーの反復検出リセット区切り文字（リスト形式、空リスト`[]`で既定値） | `LLM_DRY_SEQUENCE_BREAKERS` |
| `[llm]` | `enable_thinking` | Qwen3系モデルのthinking（reasoning、`<think>`ブロック）モードのON/OFF（llama.cpp/vLLM共通、`chat_template_kwargs`で送信、空欄なら未指定でモデル・llama-server既定に委ねる） | `LLM_ENABLE_THINKING` |
| `[llm]` | `reasoning_format` | thinkingブロックの出力形式（`none`/`deepseek`/`deepseek-legacy`。llama.cpp拡張、`provider=vllm`へは送らない。空欄なら未指定でllama-server既定の`auto`に委ねる） | `LLM_REASONING_FORMAT` |
| `[llm]` | `reasoning_budget` | thinkingに使えるトークン数上限（`-1`=無制限・`N>0`=上限。llama-serverへは`reasoning_budget_tokens`、vLLMへは`thinking_token_budget`として送信。llama-serverはリクエストでの`0`を無視するため、thinkingを止めるなら`reasoning_effort=none`か`enable_thinking=false`を使う。空欄なら未指定でllama-server既定の`-1`に委ねる） | `LLM_REASONING_BUDGET` |
| `[llm]` | `reasoning_budget_message` | 上記`reasoning_budget`を使い切った際にthinking終了タグ直前へ挿入するメッセージ（`provider=vllm`へは送らない。空欄なら挿入しない） | `LLM_REASONING_BUDGET_MESSAGE` |
| `[llm]` | `reasoning_effort` | chat templateへ渡す推論努力レベル（`none`/`default`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`。llama.cpp拡張、llama.cpp/vLLM共通、`none`はthinking無効化（`enable_thinking=true`より優先して`enable_thinking=false`も送る）、`default`は送らないのと同じ。受け付ける値はモデルのテンプレート次第で対応外はリクエストエラー。空欄なら未指定でサーバー既定に委ねる） | `LLM_REASONING_EFFORT` |
| `[llm]` | `reasoning_preserve` | 過去のassistantメッセージのthinkingを履歴に残して送るか（`chat_template_kwargs`の`preserve_reasoning`/`preserve_thinking`で送信。`true`時は保持中の過去応答のthinkingもリクエストに含める（フィールド名は`llama_cpp`=`reasoning_content`、`vllm`=`reasoning`、`openai_compatible`=両方）。空欄なら未指定でllama-server既定に委ねる） | `LLM_REASONING_PRESERVE` |
| `[llm]` | `track_token_usage` | トークン使用量の取得を有効にする（Chainlit UI表示・eval結果に反映） | `LLM_TRACK_TOKEN_USAGE` |
| `[llm]` | `request_timeout_seconds` | LLMサーバーへの応答待ちタイムアウト秒数（read/write/pool） | `LLM_REQUEST_TIMEOUT_SECONDS` |
| `[llm]` | `stream_chunk_timeout_seconds` | ストリーミング中にチャンクが届かない場合のタイムアウト秒数 | `LLM_STREAM_CHUNK_TIMEOUT_SECONDS` |
| `[llm]` | `max_concurrent_requests` | 推論サーバーへの同時リクエスト数上限。1以上でSemaphore(N)ガード（既定1＝完全直列化）、0以下で無制限。全セッション合算で数え、生成1回の間だけ枠を占有する。`[graph]`/`[subagent]` の `max_parallel` がこの値より大きくても、超えた分はLLM呼び出しの直前で待つだけで、エラーにはならない（待ち時間はタイムアウトに数えない） | `LLM_MAX_CONCURRENT_REQUESTS` |
| `[llm]` | `round_robin_slots_probe_timeout_seconds` | `round_robin`/`priority_failover`戦略がprovider="llama_cpp"の接続先を選ぶ前に送るGET /slots問い合わせ自体のタイムアウト秒数（既定3、確認できなければ空きありとみなすfail-safe） | `LLM_ROUND_ROBIN_SLOTS_PROBE_TIMEOUT_SECONDS` |
| `[llm]` | `round_robin_busy_poll_interval_seconds` | `round_robin`/`priority_failover`戦略で候補の全接続先に空きスロットが無かった場合、再確認までに待機する秒数（既定2） | `LLM_ROUND_ROBIN_BUSY_POLL_INTERVAL_SECONDS` |
| `[paths]` | `common_data_dir` | 各種データ保存先パスの共通ベースディレクトリ（既定 `./data/${instance}`）。本セクションの`checkpoint_db`/`memory_dir`/`plans_dir`、および`[log]`/`[default_workdir]`/`[path_memory]`/`[chat_log]`の`dir`系キーの値に`${common_data_dir}`と書くとここで指定した値に置換される（configparser標準の補間ではなくconfig.py側の独自置換）。`common_data_dir`自身とこれらのキーでは`${instance}`がインスタンス名（環境変数`LOCOHANE_INSTANCE`、未設定なら`default`）に置換される。ローカルディスクのみ（UNC パス・ネットワークドライブは不可） | `COMMON_DATA_DIR` |
| `[paths]` | `skills_dir` | スキルフォルダ | `SKILLS_DIR` |
| `[paths]` | `agents_dir` | エージェント種別定義フォルダ（`dispatch_agent` の `agent_type`） | `AGENTS_DIR` |
| `[paths]` | `project_locohane_dir` | プロジェクト固有の拡張ディレクトリ（ClaudeCode の `.claude/` 相当）。配下の `skills/`（`skills_dir` にマージ走査、同名は優先）・`agents/`（`agents_dir` にマージ走査、同名は優先）・`LOCOHANE.md`（プロジェクト固有指示、存在しなくてもエラーにならない）を自動検知する。`nudge_messages` と同じリスト形式で複数ディレクトリ指定可。要素を `{"dir": ..., "python": ...}` の辞書で書くと、そのディレクトリの `skills/` 配下の `.py` スクリプトだけを専用のPython環境で起動する（`[scripts].python` より優先） | `PROJECT_LOCOHANE_DIR` |
| `[paths]` | `system_prompt_path` | メインエージェント用システムプロンプトのテンプレート | `SYSTEM_PROMPT_PATH` |
| `[paths]` | `bin_path` | 外部バイナリ実行ファイルの配置先ディレクトリ一覧（`project_locohane_dir`と同じリスト形式）。コマンド名を素の状態で叩くスキルがOS側PATH未登録でも呼び出せるようにする（`src/tools/_subprocess_env.py`参照） | `BIN_PATH` |
| `[paths]` | `checkpoint_db` | 会話状態 SQLite | `CHECKPOINT_DB` |
| `[paths]` | `memory_dir` | 永続メモリーの保存先ルート | `MEMORY_DIR` |
| `[paths]` | `plans_dir` | `create_plan` が `detail_markdown` を渡した際の詳細計画Markdownの保存先 | `PLANS_DIR` |
| `[paths]` | `help_path` | `help` ツールが返すヘルプ本文Markdownのパス | `HELP_PATH` |
| `[chainlit_files]` | `retention_days` | Chainlit自身のセッションファイルディレクトリ（`.files/<セッションID>/`）の保持日数（0以下で自動削除無効）。ディレクトリ単位で削除 | `CHAINLIT_FILES_RETENTION_DAYS` |
| `[chainlit_files]` | `cleanup_interval_hours` | 自動削除チェックの実行間隔（時間） | `CHAINLIT_FILES_CLEANUP_INTERVAL_HOURS` |
| `[elements]` | `dir` | 添付ファイル・回答本文への画像埋め込みの永続化先（スレッド再開・プロセス再起動後も表示するため） | `ELEMENTS_DIR` |
| `[elements]` | `retention_days` | 上記の保持日数（0以下で自動削除無効） | `ELEMENTS_RETENTION_DAYS` |
| `[elements]` | `cleanup_interval_hours` | 自動削除チェックの実行間隔（時間） | `ELEMENTS_CLEANUP_INTERVAL_HOURS` |
| `[images]` | `max_long_side_pixels` | LLMへ渡す前に画像を縮小する長辺ピクセル数の上限（`0`で縮小なし） | `IMAGE_MAX_LONG_SIDE_PIXELS` |
| `[images]` | `jpeg_quality` | 縮小後に再エンコードするJPEG品質（1-95） | `IMAGE_JPEG_QUALITY` |
| `[images]` | `inline_preview_max_long_side_pixels` | 回答本文（Markdownテーブルのセル等）へ直接埋め込む画像プレビューの長辺ピクセル数の上限。`analyze_image`の`show_in_chat=True`と同じセッションファイル配信経路で渡すためVision向け設定より小さくても差し支えなく、表示帯域・ディスク使用量を抑える目的で別に小さい値を使う | `IMAGE_INLINE_PREVIEW_MAX_LONG_SIDE_PIXELS` |
| `[images]` | `inline_preview_jpeg_quality` | 上記プレビューの再エンコード品質（1-95） | `IMAGE_INLINE_PREVIEW_JPEG_QUALITY` |
| `[images]` | `inline_preview_min_long_side_pixels` | 上記プレビューの長辺ピクセル数の下限。これより小さい画像はこの値まで拡大してから埋め込む（`0`で拡大なし） | `IMAGE_INLINE_PREVIEW_MIN_LONG_SIDE_PIXELS` |
| `[scripts]` | `timeout` | `run_script`/`execute_python_code` 共通のタイムアウト秒 | `SCRIPT_TIMEOUT` |
| `[scripts]` | `python` | `.py` 実行に使う Python。空欄なら本体を起動している Python（`python_env.bat` の `PYTHON_DIR`） | `SCRIPT_PYTHON` |
| `[scripts]` | `code_execution_enabled` | `execute_python_code` ツール自体の有効/無効 | `CODE_EXECUTION_ENABLED` |
| `[scripts]` | `background_max_runtime_seconds` | `run_script_background` のジョブを強制終了するまでの上限秒 | `SCRIPT_BACKGROUND_MAX_RUNTIME_SECONDS` |
| `[scripts]` | `background_job_retention_seconds` | `run_script_background` の完了済みジョブが `check_script_job` で未回収のまま残ってよい秒数 | `SCRIPT_BACKGROUND_JOB_RETENTION_SECONDS` |
| `[scripts]` | `background_min_poll_interval_seconds` | `check_script_job` を同一ジョブへ再度呼べるまでの最短間隔秒（0以下で無効化） | `SCRIPT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS` |
| `[scripts]` | `background_min_poll_message` | 上記間隔未満で呼ばれた際にLLMへ返すメッセージのテンプレート（`{wait_remaining}`/`{job_id}`/`{min_interval}` を埋め込み可）。空欄なら既定文言 | `SCRIPT_BACKGROUND_MIN_POLL_MESSAGE` |
| `[scripts]` | `background_inline_wait_max_seconds` | `run_script_background`/`execute_python_code_background` がジョブ完了をLLMを介さずコード側で待つ上限秒数。超過時のみ `job_id` を返してLLMへ制御を戻す | `SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS` |
| `[scripts]` | `background_progress_push_interval_seconds` | 待機中、人間向けに経過秒数・標準出力/標準エラー末尾をチャットへ直接送る間隔（秒） | `SCRIPT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS` |
| `[scripts]` | `background_job_output_tail_chars` | 進捗表示・`check_script_job`/`stop_script_job`/`read_thread_note`が末尾のみ表示する際の標準出力/標準エラー/進捗メモの最大文字数（`[subagent]`配下の同種表示も共有） | `SCRIPT_BACKGROUND_JOB_OUTPUT_TAIL_CHARS` |
| `[file_tools_limits]` | `search_time_limit_seconds` | Glob・Grep 1回あたりの処理時間の上限（秒）。超えたら走査を打ち切り、途中までの結果に「打ち切った」旨（`scan_stopped`）を添えて返す（既定120） | `FILE_TOOLS_SEARCH_TIME_LIMIT_SECONDS` |
| `[file_tools_limits]` | `grep_max_files` | Grep 1回あたりに中身を調べるファイル数の上限（既定20000） | `FILE_TOOLS_GREP_MAX_FILES` |
| `[file_tools_limits]` | `grep_max_file_mb` | Grep が中身を調べる1ファイルのサイズ上限（MB）。超えるファイルは飛ばし、件数と例を結果に載せる（既定10） | `FILE_TOOLS_GREP_MAX_FILE_MB` |
| `[file_tools_limits]` | `regex_timeout_seconds` | Grep の正規表現が1行の検索にかけてよい時間の上限（秒）。超えたらそのファイルを飛ばす（既定2） | `FILE_TOOLS_REGEX_TIMEOUT_SECONDS` |
| `[file_tools_limits]` | `full_read_max_mb` | Read が全体を読み込み、Glob が行数を数えるファイルのサイズ上限（MB）。超えると Read は必要な行まで逐次読み、Glob は行数を数えない（既定20） | `FILE_TOOLS_FULL_READ_MAX_MB` |
| `[file_tools_duplicate_guard]` | `enabled` | Read/Glob/Grep/json_query ツールの同一引数繰り返し呼び出しを防止するガードの有効/無効 | `FILE_TOOLS_DUPLICATE_GUARD_ENABLED` |
| `[file_tools_duplicate_guard]` | `max_calls` | 同一シグネチャの呼び出しを許可する回数（既定1回） | `FILE_TOOLS_DUPLICATE_GUARD_MAX_CALLS` |
| `[file_tools_duplicate_guard]` | `carry_over_to_main` | サブエージェント内の呼び出し履歴をメイン判定へ持ち越すかどうか | `FILE_TOOLS_DUPLICATE_GUARD_CARRY_OVER` |
| `[main_agent_tool_guard]` | `mode` | メインエージェント自身がビルトインツール・`run_script`配下のスキルスクリプトを直接呼び出せる回数を制限するガードのモード（`dispatch_agent`配下のサブエージェントは対象外）。`false`=無効、`tools_skills_only`=MCP動的ツール（`mcp__server__tool`形式）のみ常に許可しそれ以外は制限、`all`（既定）=MCP動的ツールも含め全て制限。トークン消費の大きい重量系ツールの連打によるトークン上限到達を防ぐ | `MAIN_AGENT_TOOL_GUARD_MODE` |
| `[main_agent_tool_guard]` | `visibility_mode` | `mode!=false`時の可視化モード。対象はスキル一覧とビルトインツールの呼べない名前案内（`mode=all`時はMCP動的ツール名も混ざりうる）。`strict`（既定）は呼び出せないツール・スキルを一覧から完全除外、`hint`は一覧に出しつつ直接実行不可のものへ`dispatch_agentへ委譲`という注記を付ける | `MAIN_AGENT_TOOL_GUARD_VISIBILITY_MODE` |
| `[main_agent_tool_guard]` | `allow_entries` | 許可リスト（ホワイトリスト）。`["ツール名", max_calls]`または`[["スキル名","スクリプトファイル名"], max_calls]`の要素からなるリスト形式で、未登録のツール・スキルスクリプトは`mode=all`（または`tools_skills_only`時のMCP以外）の間メインエージェントから一切呼び出せない。`max_calls`は`0`=登録のみで完全ブロック、`-1`=無制限、`1`以上=その回数まで許可（他のガードと0/-1の意味が逆なので注意） | `MAIN_AGENT_TOOL_GUARD_ALLOW_ENTRIES` |
| `[graph]` | `implementation` | ReAct ループの実装（`handwritten` または `prebuilt`） | `GRAPH_IMPL` |
| `[graph]` | `recursion_limit` | メインReActループ（agent→tools遷移）の最大反復回数。超過時は打ち切りメッセージを表示 | `GRAPH_RECURSION_LIMIT` |
| `[graph]` | `connection_error_max_retries` | LLMサーバーとの通信エラー（接続失敗・5xx等）検知時、直近接続先を一時クールダウンした上でグラフを再構築し同じ反復を自動リトライする回数（メインエージェント用。`0`でリトライせず通信エラーを通知して中断） | `GRAPH_CONNECTION_ERROR_MAX_RETRIES` |
| `[graph]` | `max_parallel` | メインエージェントのツール呼び出し（ImageAwareToolNode）の同時実行数上限。1以上でSemaphore(N)ガード、0以下でガード無効化。`dispatch_agent`/`dispatch_agent_batch` はこの枠を使わない（`[subagent].max_parallel` がガードし、待っている間も画面に「順番待ち」として表示される） | `GRAPH_TOOL_MAX_PARALLEL` |
| `[graph]` | `token_guard_enabled` | メインエージェントのトークン量ガード（直近1回の応答が閾値到達で引継ぎプロンプトへ差し替え）の有効/無効 | `GRAPH_TOKEN_GUARD_ENABLED` |
| `[graph]` | `token_guard_soft_threshold` | 上記ガードが発動するトークン数閾値。実測: この仕組みが無い状態では1リクエストあたり24,833→128,000まで単調増加しコンテキスト上限で停止した事例あり | `GRAPH_TOKEN_GUARD_SOFT_THRESHOLD` |
| `[graph]` | `handoff_prompt_path` | 上記ガード発動時に差し込む、新しいチャットへの引継ぎ手順を指示するMarkdownファイルのパス | `GRAPH_HANDOFF_PROMPT_PATH` |
| `[subagent]` | `max_iterations` | `dispatch_agent` の内部ReActループの最大反復回数 | `SUBAGENT_MAX_ITERATIONS` |
| `[subagent]` | `max_parallel` | `dispatch_agent` の実LLM呼び出しの同時実行数上限。1以上でSemaphore(N)ガード、0以下でガード無効化 | `SUBAGENT_MAX_PARALLEL` |
| `[subagent]` | `batch_max_groups` | `dispatch_agent_batch` が1回で起動してよいグループ（サブエージェント）数の上限。超える場合はジョブを起動せずエラー。同時実行数ではない。0以下で無制限 | `SUBAGENT_BATCH_MAX_GROUPS` |
| `[subagent]` | `batch_stop_after_unreachable_groups` | `dispatch_agent_batch` で、LLMのタイムアウト・通信エラーが再試行後も続いて打ち切られたグループがこの数に達したら、未起動のグループを起動せず「未実行」として返す。0以下で無効 | `SUBAGENT_BATCH_STOP_AFTER_UNREACHABLE_GROUPS` |
| `[subagent]` | `lookup_files_per_agent` | 情報探索（質問に答えるためにファイルを開いて確かめる調査）で、1つのサブエージェントが自分で開いてよい元ファイル数（既定2、1未満は1）。ナレッジ検索・Grep 等で開くべきファイルがこれを超えたら、サブエージェントは読まずにファイル一覧を返し、メインが `dispatch_agent_batch(files=...)` でこの数ずつ別の explore へ割り振る。プロンプトから `${subagent_lookup_files_per_agent}` で参照 | `SUBAGENT_LOOKUP_FILES_PER_AGENT` |
| `[subagent]` | `token_guard_enabled` | サブエージェントのトークン使用量ガードの有効/無効 | `SUBAGENT_TOKEN_GUARD_ENABLED` |
| `[subagent]` | `token_guard_soft_threshold` | ソフト警告（注意メッセージ注入）のトークン閾値 | `SUBAGENT_TOKEN_GUARD_SOFT_THRESHOLD` |
| `[subagent]` | `token_guard_hard_threshold` | ハード打ち切りのトークン閾値 | `SUBAGENT_TOKEN_GUARD_HARD_THRESHOLD` |
| `[subagent]` | `token_guard_soft_warning_text` | ソフト警告到達時に注入する注意メッセージの文言（`write_thread_note`への書き出しを促しつつ、ツール呼び出し自体は禁止しない） | `SUBAGENT_TOKEN_GUARD_SOFT_WARNING_TEXT` |
| `[subagent]` | `empty_response_max_retries` | 空応答の再試行回数 | `SUBAGENT_EMPTY_RESPONSE_MAX_RETRIES` |
| `[subagent]` | `background_job_retention_seconds` | `dispatch_agent` のジョブが終了後、`check_dispatch_agent_job` で一度も取得されないまま残ってよい秒数 | `SUBAGENT_BACKGROUND_JOB_RETENTION_SECONDS` |
| `[subagent]` | `background_min_poll_interval_seconds` | `check_dispatch_agent_job` を連続で呼べる最短間隔秒数。0以下で無効化 | `SUBAGENT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS` |
| `[subagent]` | `background_min_poll_message` | 上記間隔未満で呼ばれた際にLLMへ返すメッセージのテンプレート（`{wait_remaining}`/`{job_id}`/`{min_interval}` を埋め込み可）。空欄なら既定文言 | `SUBAGENT_BACKGROUND_MIN_POLL_MESSAGE` |
| `[subagent]` | `background_inline_wait_max_seconds` | `dispatch_agent` がジョブ完了をLLMを介さずコード側で待つ上限秒数。超過時のみ `job_id` を返してLLMへ制御を戻す | `SUBAGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS` |
| `[subagent]` | `background_progress_push_interval_seconds` | 待機中、人間向けに経過秒数・反復回数・進捗メモをチャットへ直接送る間隔（秒）。最初の表示以後は、今している動作（【思考中】等）が変わったら間隔を待たずに書き換える（最短1秒空ける） | `SUBAGENT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS` |
| `[subagent]` | `background_llm_timeout_max_retries` | 実行中のLLM呼び出しがタイムアウトした場合、モデルを再構築して同じ反復を再試行する最大回数 | `SUBAGENT_BACKGROUND_LLM_TIMEOUT_MAX_RETRIES` |
| `[subagent]` | `agent_type_run_script_allowlist` | `dispatch_agent`の`agent_type`ごとに`run_script`で呼べるスキル/スクリプトを絞り込むホワイトリスト（`[[agent_type, 対象], ...]`形式、対象は`"スキル名"`または`["スキル名","スクリプトファイル名"]`、空欄なら既定19件。登録がある`agent_type`は`{{skills}}`の一覧も登録スキルのみに絞られる（絞り込みたくない場合は`{{skills}}`の代わりに`{{all_skills}}`を使う）） | `SUBAGENT_AGENT_TYPE_RUN_SCRIPT_ALLOWLIST` |
| `[user_response_timeouts]` | `approval_seconds` | `approve_plan`／`run_script`・`execute_python_code`の個別実行確認でユーザー応答を待つ秒数。`0`で無期限待ち | `APPROVAL_TIMEOUT_SECONDS` |
| `[user_response_timeouts]` | `ask_user_question_seconds` | `AskUserQuestion`（自由記述質問。`labels`省略時は単一入力、指定時は複数項目フォーム）でユーザー応答を待つ秒数。`0`で無期限待ち | `ASK_USER_QUESTION_TIMEOUT_SECONDS` |
| `[user_response_timeouts]` | `ask_user_choice_seconds` | `ask_user_choice`（選択肢質問）でユーザー応答を待つ秒数。`0`で無期限待ち | `ASK_USER_CHOICE_TIMEOUT_SECONDS` |
| `[plan]` | `allow_badge_unlock` | Plan Mode バッジの双方向切り替えを許可するか | `PLAN_ALLOW_BADGE_UNLOCK` |
| `[plan]` | `reset_approval_on_recreate` | 承認済み（Edit Automatically）状態で`create_plan`を再度呼んだ際、`plan_approved`を無条件でリセットしてPlan Modeへ戻すか。`false`なら承認状態を維持したままstepsだけ差し替える | `PLAN_RESET_APPROVAL_ON_RECREATE` |
| `[plan]` | `reset_approval_on_new_message` | 新しいユーザーメッセージを受け取るたびに`plan_approved`を無条件でリセットしてPlan Modeへ戻すか。`false`なら承認状態をメッセージをまたいで維持する（`thinking_loop_guard`のリトライ上限到達後、ユーザーが続行メッセージを送っても再承認が不要になる） | `PLAN_RESET_APPROVAL_ON_NEW_MESSAGE` |
| `[plan]` | `auto_approve` | `approve_plan`呼び出し時にユーザーへの承認/却下確認を一切行わず自動承認するか（`false`が既定。書き込み系ツールが人の確認なしに実行されるため、無人自動化用途以外での使用は推奨しない） | `PLAN_AUTO_APPROVE` |
| `[plan]` | `plan_approval_exempt_scripts` | `run_script`/`run_script_background`の計画承認（Plan Mode）を免除する読み取り専用スクリプトのホワイトリスト（`[["スキル名","スクリプトファイル名"], ...]`形式、空欄なら既定7件）。免除は承認のみで、`[main_agent_tool_guard]`によるメインエージェントからの直接呼び出し制限は別枠 | `PLAN_APPROVAL_EXEMPT_SCRIPTS` |
| `[default_workdir]` | `dir` | エージェントの既定の作業ディレクトリのベース。実際の書き込み・`run_script`のcwdはこの配下の自セッション専用サブディレクトリ`_tmp_<name>`に限定される（`dir`直下への書き込みは許可されない） | `DEFAULT_WORKDIR` |
| `[default_workdir]` | `allow_sandbox_dir` | セッション分離の外側で常時書き込みを許可する追加ディレクトリのリスト（`[{"dir": "パス", "allow_entries": [["スキル名","スクリプトファイル名"], ...]}, ...]`形式）。各要素の`allow_entries`を空リストにすると、対象を問わずそのディレクトリへ無制限に書き込み可能になる。登録したディレクトリはスレッドを問わず常時書き込み可能になる点に注意（既定は空リストで無効） | `ALLOW_SANDBOX_DIR` |
| `[default_workdir]` | `retention_days` | 上記 `dir` 配下のファイル保持日数（0以下で自動削除無効） | `DEFAULT_WORKDIR_RETENTION_DAYS` |
| `[skill_creator]` | `draft_dir` | ユーザー別ドラフトスキルの置き場（既定 `${common_data_dir}/skill_drafts`）。スキル走査対象と重なると起動時エラー | `SKILL_DRAFT_DIR` |
| `[skill_creator]` | `tryout_repeats` | スキル安定化トライアウトの既定回数（昇格時に各ケースをこの回数だけ繰り返し、全回合格で合格。既定10） | `SKILL_TRYOUT_REPEATS` |
| `[skill_creator]` | `other_users_drafts` | 他ユーザーのドラフトの扱い。`hidden`（既定）／`listed`／`readable`／`full`（「ドラフトスキル（ユーザー別）と昇格」参照） | `SKILL_OTHER_USERS_DRAFTS` |
| `[skill_creator]` | `archive_dir` | 昇格したドラフトの退避先（既定 `${common_data_dir}/skill_drafts_archive`）。スキル走査対象・`draft_dir` と重なると起動時エラー | `SKILL_ARCHIVE_DIR` |
| `[skill_lab]` | `instance_locohane_dir` | インスタンス専用の拡張ディレクトリで、スキル研究室・promote-skill の昇格先（既定 `${instance_dir}` = `instances/<インスタンス名>`。`skills/`・`agents/`・回帰テスト用の `evals/<テーマID>/` を置く）。昇格先（対象）のインスタンスの値が使われる。本体も `project_locohane_dir` と同じく `skills/`・`agents/`・`LOCOHANE.md` を検知し、そのどれよりも後方（最優先）で常に走査する（`project_locohane_dir` を上書きしても外れない） | `INSTANCE_LOCOHANE_DIR` |
| `[skill_lab_worker]` | `max_iterations` | スキル調整ワーカーの スキル調整ループの最大反復回数（既定8）。このセクションはスキル調整ワーカーの設定画面にだけ出る（本体には出ない） | `SKILL_LAB_MAX_ITERATIONS` |
| `[skill_lab_worker]` | `max_hours` | 1テーマのループに使ってよい最大時間（既定6時間） | `SKILL_LAB_MAX_HOURS` |
| `[skill_lab_worker]` | `poll_interval_seconds` | ワーカーが処理待ちのテーマを探す間隔（既定10秒） | `SKILL_LAB_POLL_INTERVAL_SECONDS` |
| `[skill_lab_worker]` | `fixer_max_file_chars` | AI に渡す資産ファイル1つあたりの最大文字数（既定20000） | `SKILL_LAB_FIXER_MAX_FILE_CHARS` |
| `[skill_lab_worker]` | `transcript_excerpt_chars` | AI に渡す実行の記録の抜粋の最大文字数（1回分、既定6000） | `SKILL_LAB_TRANSCRIPT_EXCERPT_CHARS` |
| `[default_workdir]` | `cleanup_interval_hours` | default_workdir 自動削除チェック間隔（時間） | `DEFAULT_WORKDIR_CLEANUP_INTERVAL_HOURS` |
| `[log]` | `dir` | ログ出力先 | `LOG_DIR` |
| `[log]` | `level` | ログの詳細度。`info`（現行仕様、ツール呼び出しの概要のみ）／`debug`（ツール呼び出しの全引数・全結果・LLM応答本文・thinkingまで記録）／`none`（ログを一切生成しない） | `LOG_LEVEL` |
| `[log]` | `clear_on_startup` | 起動のたびに `app.log` を空にしてから書き始めるか（`false`＝従来通り追記） | `LOG_CLEAR_ON_STARTUP` |
| `[log]` | `max_lines` | app_*.log の最大行数（超過時にローテーション） | `LOG_MAX_LINES` |
| `[log]` | `retention_days` | ローテーション済み app_*.log の保持日数 | `LOG_RETENTION_DAYS` |
| `[log]` | `cleanup_interval_hours` | app_*.log 自動削除チェック間隔（時間） | `LOG_CLEANUP_INTERVAL_HOURS` |
| `[thinking_loop_guard]` | `enabled` | ループ検知＆ループガード＆リトライ機能の有効/無効 | `THINKING_LOOP_GUARD_ENABLED` |
| `[thinking_loop_guard]` | `target` | ループ検知の監視対象（`all`: 思考と本文の両方 / `content_only`: 本文だけを監視し、思考のループは`[llm].reasoning_budget`での打ち切りに任せる。`thinking_only`: 思考だけを監視し、本文は対象外。`content_only`で思考予算が無制限なら起動時に警告）。比較は同じチャネル（思考／本文）の中で行うため、思考の内容を本文へそのまま書き写してもループとは判定しない | `THINKING_LOOP_GUARD_TARGET` |
| `[thinking_loop_guard]` | `window_chars` | ループ判定対象とする直近テキストのウィンドウ文字数 | `THINKING_LOOP_GUARD_WINDOW_CHARS` |
| `[thinking_loop_guard]` | `check_interval_chars` | 何文字増えるごとに再チェックするか | `THINKING_LOOP_GUARD_CHECK_INTERVAL_CHARS` |
| `[thinking_loop_guard]` | `confirm_count` | ループ確定と判定するまでの連続成立回数（誤検知防止） | `THINKING_LOOP_GUARD_CONFIRM_COUNT` |
| `[thinking_loop_guard]` | `max_history_chars` | 反復検知で比較対象とする過去履歴の上限文字数 | `THINKING_LOOP_GUARD_MAX_HISTORY_CHARS` |
| `[thinking_loop_guard]` | `match_ratio_threshold` | 直近ウィンドウとの最長一致率がこの値を超えたらループ候補とみなす閾値 | `THINKING_LOOP_GUARD_MATCH_RATIO_THRESHOLD` |
| `[thinking_loop_guard]` | `max_retries` | ループ検知後、注意メッセージを注入して再試行する最大回数 | `THINKING_LOOP_GUARD_MAX_RETRIES` |
| `[thinking_loop_guard]` | `empty_response_max_retries` | メインエージェントの空応答（tool_callsもcontentも無い応答）を検知した際の再試行最大回数 | `THINKING_LOOP_GUARD_EMPTY_RESPONSE_MAX_RETRIES` |
| `[thinking_loop_guard]` | `nudge_messages` | ループ検知後に注入する注意メッセージ（複数指定可） | `THINKING_LOOP_GUARD_NUDGE_MESSAGES` |
| `[tool_loop_guard]` | `enabled` | 全く同じ応答（本文とツール呼び出しの名前・引数）を連続で繰り返すループを検知する機能の有効/無効（`src/tool_loop_guard.py`）。ツールの結果やエラーかどうかは比べない。検知したら`[thinking_loop_guard]`と同じ仕組みで、繰り返した応答とその結果を会話履歴から取り除いたうえで（残すとモデルが同じ呼び出しを書き写し続けるため）、注意メッセージに「何を繰り返したか」を添えて再試行し、`max_retries`（思考のループと合算）で停止してユーザーに通知する。サブエージェントは上限でそこまでの経過を返して打ち切る。モデルにツール呼び出しを強制する`tool_choice`はQwen3.6では効かないため使わない | `TOOL_LOOP_GUARD_ENABLED` |
| `[tool_loop_guard]` | `max_repeats` | 全く同じ応答が何回連続したらループとみなすか（1以下で無効） | `TOOL_LOOP_GUARD_MAX_REPEATS` |
| `[tool_loop_guard]` | `nudge_messages` | ループ検知後に注入する注意メッセージ（複数指定可。思考のループとは別に数えた回数の順に使い、末尾に何を繰り返したかが自動で添えられる） | `TOOL_LOOP_GUARD_NUDGE_MESSAGES` |
| `[tool_loop_guard]` | `exclude_tools` | このツールだけを呼ぶ応答は数えない（状態確認のポーリング等。リスト形式、既定`["check_script_job", "check_dispatch_agent_job"]`） | `TOOL_LOOP_GUARD_EXCLUDE_TOOLS` |
| `[thinking_control]` | `enabled` | ReActループの各ステップで、直前の履歴の形から思考レベル（`off`/`low`/`medium`/`high`/`xhigh`）をルールで選び、リクエストごとに切り替える機能全体の有効/無効（`src/llm/thinking_control.py`）。`off`は`enable_thinking=false`、それ以外は思考予算の大小で表すため、プロンプトの先頭部分は変わらずKVキャッシュは外れない（`reasoning_effort`はテンプレート次第でシステムプロンプトが変わるため切り替えに使わない）。`false`なら従来と全く同じリクエスト。`[llm]`側で思考が無効なら働かない。判定はアプリログの`thinking_control:`行（レベル・判定理由・直前ステップの思考文字数・予算での打ち切り・本文への漏れ）で確認できる | `THINKING_CONTROL_ENABLED` |
| `[thinking_control]` | `apply_to_main` / `apply_to_sub` | メインエージェント / サブエージェントのループに適用するか（要約・圧縮のLLM呼び出しには常に適用しない） | `THINKING_CONTROL_APPLY_TO_MAIN` / `THINKING_CONTROL_APPLY_TO_SUB` |
| `[thinking_control]` | `rule_user_turn` / `rule_tool_error` / `rule_consecutive_cap` / `rule_after_tools` | 判定ルール1〜4それぞれに当てはまったときのレベル（`off`〜`xhigh`、`false`でそのルールを無効化。1: 末尾がユーザー発言・ナッジ（画像のフォローアップは除く）、2: 直前のツールがエラー、3: 直前`max_consecutive_reduced`回が全てこのレベル未満、4: 直前に呼んだツールが全て`after_tools`にある）。ルール1→4の順で最初に当てはまったものを使い、どれにも当たらなければ`default_level`。既定は`high`/`high`/`high`/`low` | `THINKING_CONTROL_RULE_USER_TURN` 等 |
| `[thinking_control]` | `default_level` | どのルールにも当たらないときのレベル | `THINKING_CONTROL_DEFAULT_LEVEL` |
| `[thinking_control]` | `budget_low` / `budget_medium` / `budget_high` / `budget_xhigh` | 各レベルの思考予算（-1=無制限、空欄なら`[llm].reasoning_budget`）。小さくするほど予算での打ち切りが増え、思考が本文へ漏れやすくなる | `THINKING_CONTROL_BUDGET_LOW` 等 |
| `[thinking_control]` | `after_tools` | ルール4の対象ツール一覧（リスト形式`["Read", "Grep"]`）。起動時に存在しないツール名を警告する | `THINKING_CONTROL_AFTER_TOOLS` |
| `[thinking_control]` | `error_prefixes` | ルール2でエラーとみなすツール結果の先頭文字列（リスト形式。`ToolMessage.status="error"`も対象） | `THINKING_CONTROL_ERROR_PREFIXES` |
| `[thinking_control]` | `max_consecutive_reduced` | ルール3が働くまでの連続回数（0以下で無効） | `THINKING_CONTROL_MAX_CONSECUTIVE_REDUCED` |
| `[context_trim]` | `enabled` | 古い `ToolMessage` を切り詰めてプリフィル遅延を抑える機能の有効/無効 | `CONTEXT_TRIM_ENABLED` |
| `[context_trim]` | `trigger_total_tokens` | トリムを発動させる閾値（Claude APIのcontext editing、`clear_tool_uses_20250919`のtrigger.value相当）。LLM呼び出しのtotal_tokensがこの値未満のうちは発動しない。一度でも達したら、以後その会話ではトリムを継続する（解除・再トリムの繰り返しでプレフィックスキャッシュが毎回外れるのを防ぐため）。コンテキスト圧縮が走ると解除される。0以下なら常に発動 | `CONTEXT_TRIM_TRIGGER_TOTAL_TOKENS` |
| `[context_trim]` | `keep_recent_tool_iterations` | 全文保持する直近の反復数（1反復＝ReActループ1周＝LLM呼び出し1回。`AIMessage`の件数で数える。ユーザーの発話回数では数えない — 画像フォローアップや各種nudgeが`HumanMessage`として積まれるためずれる。並列tool_callsに対応する`ToolMessage`群は同じ反復内で分断されない） | `CONTEXT_TRIM_KEEP_RECENT_TOOL_ITERATIONS` |
| `[context_trim]` | `trim_ai_messages` | `ToolMessage`だけでなく`AIMessage`（モデル自身の思考本文・tool_calls引数）も切り詰め対象にするか。`execute_python_code`のcode引数へファイル本文を書き写す使い方をすると`ToolMessage`側だけの切り詰めでは1リクエスト入力が膨らみ続けるため既定で有効 | `CONTEXT_TRIM_AI_MESSAGES` |
| `[context_trim]` | `keep_recent_ai_iterations` | 全文保持する直近の反復数（`keep_recent_tool_iterations`と同じ判定方式・独立した値。`trim_ai_messages=true`の場合のみ意味を持つ） | `CONTEXT_TRIM_KEEP_RECENT_AI_ITERATIONS` |
| `[context_trim]` | `truncated_max_chars` | 切り詰め対象 `ToolMessage` の残す最大文字数 | `CONTEXT_TRIM_TRUNCATED_MAX_CHARS` |
| `[context_trim]` | `duplicate_guard_tool_max_chars` | Read/Glob/Grep/json_query/analyze_image（`[file_tools_duplicate_guard]`の対象ツール）の `ToolMessage` にだけ適用する切り詰め文字数（`truncated_max_chars`の代わりに使う） | `CONTEXT_TRIM_DUPLICATE_GUARD_TOOL_MAX_CHARS` |
| `[context_compaction]` | `enabled` | 会話履歴の自動要約・圧縮機能（ClaudeCodeのcompact相当）の有効/無効 | `CONTEXT_COMPACTION_ENABLED` |
| `[context_compaction]` | `token_threshold` | メインエージェントの累積トークン数（圧縮発火のたびに0へリセット）がこの値を超えたら圧縮する条件（1リクエストあたりの上限ではない） | `CONTEXT_COMPACTION_TOKEN_THRESHOLD` |
| `[context_compaction]` | `single_request_token_threshold` | 直近1回のLLM呼び出しのtotal_tokensがこの値を超えたら圧縮する条件（累積条件とのOR判定） | `CONTEXT_COMPACTION_SINGLE_REQUEST_TOKEN_THRESHOLD` |
| `[context_compaction]` | `keep_recent_iterations` | 圧縮時に丸ごと保持する直近の反復数（`[context_trim]`の`keep_recent_tool_iterations`と同じ単位）。これとは別に「末尾の`HumanMessage`以降は必ず原文のまま残す」上限も内部で被せる（適用すると要約対象が空になる場合は圧縮の機会自体が失われるため適用しない） | `CONTEXT_COMPACTION_KEEP_RECENT_ITERATIONS` |
| `[context_compaction]` | `min_messages_to_compact` | 会話全体のメッセージ数がこの件数未満なら圧縮しない安全弁 | `CONTEXT_COMPACTION_MIN_MESSAGES_TO_COMPACT` |
| `[context_compaction]` | `compaction_prompt_path` | 要約を指示するプロンプト本文（Markdown）のパス | `CONTEXT_COMPACTION_PROMPT_PATH` |
| `[context_compaction]` | `summary_source_max_chars` | 要約対象の古い`ToolMessage`を要約LLMへ渡す前に切り詰める文字数（`[context_trim]`とは別枠） | `CONTEXT_COMPACTION_SUMMARY_SOURCE_MAX_CHARS` |
| `[context_compaction]` | `skill_reattach_max_chars_per_skill` | 要約対象に入った`read_skill`結果（スキル本文）を要約の後ろへ再添付する際の1スキルあたりの最大文字数（サブエージェントも共有） | `CONTEXT_COMPACTION_SKILL_REATTACH_MAX_CHARS_PER_SKILL` |
| `[context_compaction]` | `skill_reattach_total_max_chars` | 上記の再添付全体の最大文字数。収まらない古いスキルは名前だけを列挙する | `CONTEXT_COMPACTION_SKILL_REATTACH_TOTAL_MAX_CHARS` |
| `[context_compaction]` | `pre_note_threshold` | 圧縮発火前に`write_thread_note`への書き出しを促す注意メッセージを注入する、直近1回のLLM呼び出しのtotal_tokens閾値（0以下で無効化） | `CONTEXT_COMPACTION_PRE_NOTE_THRESHOLD` |
| `[context_compaction]` | `pre_note_warning_text` | 上記閾値到達時に注入する注意メッセージの文言 | `CONTEXT_COMPACTION_PRE_NOTE_WARNING_TEXT` |
| `[token_ratio]` | `enabled` | 設定ダッシュボード専用（アプリ本体は読まない）。基準値`[graph] token_guard_soft_threshold`や比率を変更したときに、各閾値へ「基準値×比率」を自動入力するか | - |
| `[token_ratio]` | `<セクション>.<キー>`（例: `subagent.token_guard_soft_threshold`） | 自動入力先の閾値の、基準値に対する比率（空欄なら連動させない） | - |
| `[memory]` | `user_visibility` / `feedback_visibility` / `project_visibility` / `reference_visibility` | 種類ごとの永続メモリーの公開範囲。`private` の種類は常に個人用、`shared` の種類は LLM が振り分け基準で選ぶ（既定は `user` だけ `private`。「永続メモリー」参照） | `MEMORY_<TYPE>_VISIBILITY` |
| `[memory]` | `shared_criteria` / `private_criteria` | LLM に渡す振り分け基準（言語は問わず、改行可。空欄なら既定の文言。設定ダッシュボードの「AIで清書」で表示言語の箇条書きに整えられる） | `MEMORY_SHARED_CRITERIA` / `MEMORY_PRIVATE_CRITERIA` |
| `[memory]` | `user_ranks` / `default_rank` | メモリーの優先順位に使うユーザーのランク（`{"ユーザー名": ランク}`。0以上の整数で大きいほど上位。食い違うメモリーはランクの高い方に従う。設定ダッシュボードの「ユーザー」画面でも設定できる）と、未設定のユーザー・共通のメモリーのランク（既定1）。「永続メモリー」参照 | `MEMORY_USER_RANKS` / `MEMORY_DEFAULT_RANK` |
| `[memory]` | `force_private_checks` | 指定に関係なく `private` にする目印のリスト（`email`／`phone`／`card_id`／`credential`／`confidential_label`／`personal_path`。既定 `["card_id", "credential"]`） | `MEMORY_FORCE_PRIVATE_CHECKS` |
| `[path_memory]` | `dir` | パスメモリー機能のレジストリファイル（`<thread_id>.json`）保存先 | `PATH_MEMORY_DIR` |
| `[path_memory]` | `retention_days` | パスメモリーのレジストリファイル保持日数 | `PATH_MEMORY_RETENTION_DAYS` |
| `[path_memory]` | `cleanup_interval_hours` | パスメモリーの自動削除チェック間隔（時間） | `PATH_MEMORY_CLEANUP_INTERVAL_HOURS` |
| `[path_memory]` | `max_entries` | 1会話あたりのパスメモリー登録上限件数 | `PATH_MEMORY_MAX_ENTRIES` |
| `[path_memory]` | `search_min_score` | パスメモリー類似検索（`search_path_memory`・「見つかりません」エラー時の候補提示）で結果に出す類似度（0.0〜1.0）の下限（既定0.3） | `PATH_MEMORY_SEARCH_MIN_SCORE` |
| `[vector_db]` | `enabled` | ベクターDB（社内文書の意味検索）の有効/無効（既定 false）。false の間は `search_documents`/`list_document_collections` が「無効です」を返し、ダッシュボードからの取り込みもできない（「ベクターDB（社内文書の意味検索）」参照） | `VECTOR_DB_ENABLED` |
| `[vector_db]` | `backend` | ベクターの保存先（`sqlite_vec` 既定 / `qdrant`） | `VECTOR_DB_BACKEND` |
| `[vector_db]` | `distance` | コレクションの距離関数（`Cosine` 既定 / `Euclid` / `Manhattan` / `Dot`。`Dot` は `backend = qdrant` のみ）。**変えた後は既存のコレクションの再構築が必要**。`Euclid`・`Manhattan` の類似度は `1 / (1 + 距離)` に換算 | `VECTOR_DB_DISTANCE` |
| `[vector_db]` | `dir` | `catalog.sqlite`（コレクション定義・取り込み済みファイルの記録）と、`sqlite_vec` 時の `vectors.sqlite`、キーワード索引 `keywords/`（コレクションごとの `.sqlite`）の保存先（既定 `${common_data_dir}/vector_db`） | `VECTOR_DB_DIR` |
| `[vector_db]` | `qdrant_url` | `backend = qdrant` 時の Qdrant サーバー URL（例 `http://localhost:6333`）。API キーは `.env` の `QDRANT_API_KEY` | `VECTOR_DB_QDRANT_URL` |
| `[vector_db]` | `qdrant_collection_prefix` | Qdrant 上のコレクション名の接頭辞（既定 `locohane_${instance}_`）。この接頭辞以外のコレクションには触れない | `VECTOR_DB_QDRANT_COLLECTION_PREFIX` |
| `[vector_db]` | `qdrant_timeout_seconds` | Qdrant への1リクエストのタイムアウト秒数（既定120。検索は最大30秒、接続は最大10秒） | `VECTOR_DB_QDRANT_TIMEOUT_SECONDS` |
| `[vector_db]` | `qdrant_max_retries` | Qdrant へ接続できない・接続を切られた・タイムアウトした・5xx・408・429 が返ったときに送り直す回数（既定5。`-1` は無制限、`0` は送り直さない。取り込み・コピーは送り直しの待ち中も「停止」で止められる。検索はこの値によらず最大1回）。**全インスタンス共通**（インスタンス別に上書きできない。設定ダッシュボードの「管理者設定」で変えられる） | `VECTOR_DB_QDRANT_MAX_RETRIES` |
| `[vector_db]` | `qdrant_retry_interval_seconds` | 上記の送り直しの間隔（秒。既定10。検索は最大1秒）。**全インスタンス共通**（インスタンス別に上書きできない。設定ダッシュボードの「管理者設定」で変えられる） | `VECTOR_DB_QDRANT_RETRY_INTERVAL_SECONDS` |
| `[vector_db]` | `qdrant_preset` | Qdrant のコレクションのチャンク件数の規模別の設定（`none` 既定＝Qdrant の既定 / `1m` / `2m` / … / `10m`＝〜100万〜1000万件向け。HNSW・int8 量子化・ベクターのディスク配置・セグメント数・検索時の `hnsw_ef`）。既存のコレクションには次の取り込み時に反映（再構築不要）。「Qdrant を使う」参照 | `VECTOR_DB_QDRANT_PRESET` |
| `[vector_db]` | `embedding_url` | 埋め込みを計算する接続先（`/v1/embeddings`）。`[llm] main_url` と同じ形式で、全要素同じ `model`・`start`/`end` 指定不可。`enabled = true` のとき必須（`[]` は未設定） | `VECTOR_DB_EMBEDDING_URL` |
| `[vector_db]` | `embedding_routing_strategy` | `embedding_url` が複数件のときの選び方（`round_robin` 既定 / `random` / `priority_failover`。`round_robin`・`priority_failover` は llama_cpp なら GET /slots、vllm なら GET /metrics で空きを確かめ、埋まっている接続先を飛ばす）。どの戦略でもエラーが返れば次の接続先へ送り直す（「埋め込みサーバーの準備と設定」参照） | `VECTOR_DB_EMBEDDING_ROUTING_STRATEGY` |
| `[vector_db]` | `embedding_max_retries` | 取り込みで全接続先がエラーになったとき試し直す回数（既定3）。`0` なら成功するまで無制限。408・429 以外の 4xx だけなら試し直さない | `VECTOR_DB_EMBEDDING_MAX_RETRIES` |
| `[vector_db]` | `search_embedding_max_retries` | 検索（`search_documents`）で同じく試し直す回数（既定2）。`0` は試し直さない（無制限は指定できない） | `VECTOR_DB_SEARCH_EMBEDDING_MAX_RETRIES` |
| `[vector_db]` | `embedding_retry_interval_seconds` | 上記の試し直しの間隔（秒、既定10。取り込み・検索で共通） | `VECTOR_DB_EMBEDDING_RETRY_INTERVAL_SECONDS` |
| `[vector_db]` | `embedding_batch_size` | 埋め込み1リクエストあたりのチャンク数（既定16。取り込みでは複数のファイルのチャンクをまとめる） | `VECTOR_DB_EMBEDDING_BATCH_SIZE` |
| `[vector_db]` | `embedding_encoding_format` | 埋め込みの受け取り方（`base64` 既定・`float`）。`base64` は応答が小さく解析が速い。対応しない接続先（400・422 を返す）は自動で `float` に切り替える | `VECTOR_DB_EMBEDDING_ENCODING_FORMAT` |
| `[vector_db]` | `embedding_timeout_seconds` | 埋め込み1リクエストのタイムアウト秒数（既定120） | `VECTOR_DB_EMBEDDING_TIMEOUT_SECONDS` |
| `[vector_db]` | `query_prefix` / `document_prefix` | 検索語・文書の先頭に付ける文字列（既定 `auto` で `model` の名前から決める。空欄で付けない、それ以外はその値。末尾の空白を残すならダブルクォートで囲む。`document_prefix` を変えたら再構築が必要。「埋め込みサーバーの準備と設定」参照） | `VECTOR_DB_QUERY_PREFIX` / `VECTOR_DB_DOCUMENT_PREFIX` |
| `[vector_db]` | `chunk_size` / `chunk_overlap` | チャンク1つの最大文字数（既定800）と、隣り合うチャンクで重ねる文字数（既定100、`chunk_size` 未満） | `VECTOR_DB_CHUNK_SIZE` / `VECTOR_DB_CHUNK_OVERLAP` |
| `[vector_db]` | `include_extensions` | 取り込む拡張子（カンマ区切り。対応形式は `.pdf .docx .xlsx .xlsm .pptx .md .txt .csv` と画像ファイル `.png .jpg .jpeg .gif .bmp .webp .tif .tiff .heic .heif`。画像ファイルは `image_input = false` ならファイルパスだけを登録する） | `VECTOR_DB_INCLUDE_EXTENSIONS` |
| `[vector_db]` | `text_extensions` | 対応形式のほかに、`.txt` と同じくテキストとして読む拡張子（カンマ区切り。既定は空。例: `.step, .igs, .mat`）。取り込むには `include_extensions` にも書く。中身がバイナリ（NUL を含む）のファイルはファイルパスだけを登録する。対応形式・画像ファイルの拡張子はその形式の読み方が優先 | `VECTOR_DB_TEXT_EXTENSIONS` |
| `[vector_db]` | `max_file_mb` | 取り込む1ファイルの上限サイズ（MB、既定50。超えると失敗一覧に載る）。**全インスタンス共通**（インスタンス別に上書きできない。設定ダッシュボードの「管理者設定」で変えられる） | `VECTOR_DB_MAX_FILE_MB` |
| `[vector_db]` | `image_input` | 埋め込みに画像を入力するか（既定 false）。true なら `image_input_extensions` の形式（既定 `.pdf`）は本文に加えてページごとの画像も埋め込み、それ以外の Office ファイルは中の画像を埋め込み（`embedded_images`）、画像ファイル（`.png .jpg .heic` 等）も画像として埋め込む。画像入力に対応した埋め込みモデルと、Office ファイルには Windows・Microsoft Office が必要（「ページを画像として取り込む（image_input）」参照） | `VECTOR_DB_IMAGE_INPUT` |
| `[vector_db]` | `image_input_extensions` | `image_input = true` のときにページ画像も埋め込む文書の形式（既定 `.pdf`。`.pdf .docx .pptx .xlsx .xlsm` から選ぶ。空ならどれも画像にしない）。含めない Office ファイルは本文と中の画像（`embedded_images`）で埋め込む。画像ファイルはこの設定に関係なく画像で埋め込む | `VECTOR_DB_IMAGE_INPUT_EXTENSIONS` |
| `[vector_db]` | `embedded_images` | `image_input = true` で、ページ画像にしない Office ファイル（`.docx .pptx .xlsx .xlsm`）の中の画像（写真・図）を取り出して1枚ずつ画像で埋め込むか（既定 true。Office は起動しない。「ページを画像として取り込む（image_input）」参照） | `VECTOR_DB_EMBEDDED_IMAGES` |
| `[vector_db]` | `embedded_image_min_px` | `embedded_images` で取り出す画像の幅・高さの下限（px、既定 128）。どちらかが下回る画像（アイコン等）は埋め込まない。0 なら大きさで除かない | `VECTOR_DB_EMBEDDED_IMAGE_MIN_PX` |
| `[vector_db]` | `image_dpi` | `image_input = true` のときにページを画像にする解像度（DPI、既定100） | `VECTOR_DB_IMAGE_DPI` |
| `[vector_db]` | `office_timeout_seconds` | `image_input = true` で Office ファイルを PDF に書き出す1回の上限秒数（既定180）。超えたら（見えない確認画面で止まった等）その Office のプロセスを終了し、そのファイルを失敗一覧に載せる。**全インスタンス共通**（インスタンス別に上書きできない。設定ダッシュボードの「管理者設定」で変えられる） | `VECTOR_DB_OFFICE_TIMEOUT_SECONDS` |
| `[vector_db]` | `ingest_temp_dir` | 取り込みの間だけ使う一時ファイル（ページ画像の PNG・ファイルの中から取り出した画像・Office から書き出した PDF）を置くフォルダ。空（既定）ならシステムの一時フォルダ（`%TEMP%`）。相対パスはプロジェクトルート基準（`${common_data_dir}`・`${instance}` は使えない）。無ければ作り、その下に取り込み1回ごとのフォルダ（`locohane_vdb_pages_*`）を作って終わったら消す。システムドライブの空きが少ないときに別のドライブを指定する。**全インスタンス共通**（インスタンス別に上書きできない。設定ダッシュボードの「管理者設定」で変えられる） | `VECTOR_DB_INGEST_TEMP_DIR` |
| `[vector_db]` | `ingest_workers` | 取り込みの並列数（既定1）。並列に準備するファイル数（`ingest_processes` が1以上ならそちらの値）。同時に送る埋め込みのリクエスト数は `ingest_embed_requests`（「文書の取り込み」参照）。**全インスタンス共通**（インスタンス別に上書きできない。設定ダッシュボードの「管理者設定」で変えられる。設定ダッシュボードの取り込みは全インスタンス・全コレクションの合計でこの並列数まで） | `VECTOR_DB_INGEST_WORKERS` |
| `[vector_db]` | `scan_workers` | 取り込み元フォルダの走査で同時に読むフォルダの数（スレッド数、既定8）。ファイルサーバー上のフォルダで効く。**全インスタンス共通**（インスタンス別に上書きできない。設定ダッシュボードの「管理者設定」で変えられる） | `VECTOR_DB_SCAN_WORKERS` |
| `[vector_db]` | `max_files_per_collection` | 1コレクションで取り込む最大ファイル数（既定1000000）。超えた分は取り込まず、ナレッジ画面に警告を出す（取り込み済みのファイルを優先し、残りはパス順の先頭から選ぶ。「文書の取り込み」参照）。**全インスタンス共通**（インスタンス別に上書きできない。設定ダッシュボードの「管理者設定」で変えられる） | `VECTOR_DB_MAX_FILES_PER_COLLECTION` |
| `[vector_db]` | `ingest_processes` | 取り込みで本文の抽出・ページの画像化を行う子プロセスの数（既定0＝子プロセスを使わず `ingest_workers` のスレッドで行う）。CPU のコアに余裕があり PDF・Excel 等が多いときに 2 以上にする。**全インスタンス共通**（インスタンス別に上書きできない） | `VECTOR_DB_INGEST_PROCESSES` |
| `[vector_db]` | `ingest_embed_requests` | 取り込みで同時に送る埋め込みのリクエスト数（既定0＝`ingest_workers` と同じ）。1ファイルは複数のリクエストに分けて送るため、ファイル数より多くしてよい。推論サーバー側も同時処理（llama-server の `--parallel`）が必要。**全インスタンス共通**（インスタンス別に上書きできない。設定ダッシュボードの「管理者設定」で変えられる） | `VECTOR_DB_INGEST_EMBED_REQUESTS` |
| `[vector_db]` | `top_k` | `search_documents`・`search_document_paths` が既定で返す件数（既定5） | `VECTOR_DB_TOP_K` |
| `[vector_db]` | `max_result_chars` | `search_documents`・`search_document_paths` の結果全体の最大文字数（既定6000） | `VECTOR_DB_MAX_RESULT_CHARS` |
| `[vector_db]` | `min_score` | 結果に出す本文のチャンクの類似度（0.0〜1.0、コサイン類似度）の下限（既定0.3） | `VECTOR_DB_MIN_SCORE` |
| `[vector_db]` | `image_min_score` | `image_input` で埋め込んだページ画像の類似度の下限（既定0.1）。検索語と画像の類似度は本文より低めに出るため `min_score` と別に持つ | `VECTOR_DB_IMAGE_MIN_SCORE` |
| `[vector_db]` | `path_min_score` | `search_document_paths` の結果に出すファイルパスの類似度の下限（既定0.3）。パスは短い文字列のため、本文と値の出方が違えば `min_score` と別に調整する | `VECTOR_DB_PATH_MIN_SCORE` |
| `[vector_db]` | `rrf_weight_text` | 検索結果を順位で統合するときの、本文の埋め込みの結果の重み（既定1.0、0 で使わない） | `VECTOR_DB_RRF_WEIGHT_TEXT` |
| `[vector_db]` | `rrf_weight_keyword` | 同じく本文のキーワード検索（BM25）の結果の重み（既定1.0、0 で使わない） | `VECTOR_DB_RRF_WEIGHT_KEYWORD` |
| `[vector_db]` | `rrf_weight_path_keyword` | `search_document_paths` で、ファイルパスのキーワード検索（BM25）の結果を、ファイルパスの埋め込みの結果（重み 1.0）と順位で統合するときの重み（既定1.0、0 で使わない） | `VECTOR_DB_RRF_WEIGHT_PATH_KEYWORD` |
| `[vector_db]` | `rrf_weight_image` | 同じくページ画像（`image_input`）の結果の重み（既定1.0、0 で使わない） | `VECTOR_DB_RRF_WEIGHT_IMAGE` |
| `[path_memory]` | `search_filename_weight` | 検索語がフルパスの場合に、類似度計算でファイル名部分を重視する割合（既定0.7、残りはフォルダ部分同士の類似度） | `PATH_MEMORY_SEARCH_FILENAME_WEIGHT` |
| `[auth]` | `enabled` | ログイン認証機能のON/OFF（`false`＝現状通りログイン不要） | `AUTH_ENABLED` |
| `[auth]` | `require_password` | 認証ON時、パスワード一致を必須にするか（`false`＝ユーザー名のみで通す） | `AUTH_REQUIRE_PASSWORD` |
| `[chat_log]` | `enabled` | 会話ログ（ユーザー発言・AI最終応答）のテキストファイル記録の有効/無効 | `CHAT_LOG_ENABLED` |
| `[chat_log]` | `dir` | 会話ログの保存先ルートディレクトリ | `CHAT_LOG_DIR` |
| `[thread_store]` | `enabled` | 会話スレッド一覧（画面左サイドバー）・再開機能の有効/無効 | `THREAD_STORE_ENABLED` |
| `[thread_store]` | `db` | スレッド一覧・Step履歴の保存先SQLiteファイル（`data/checkpoints.sqlite`とは別ファイル） | `THREAD_STORE_DB` |
| `[thread_store]` | `retention_days` | スレッド一覧の保持日数（0以下で無期限保持、既定）。削除してもLangGraph側の会話状態自体は消えない | `THREAD_STORE_RETENTION_DAYS` |
| `[thread_store]` | `cleanup_interval_hours` | 上記の自動削除チェックの実行間隔（時間） | `THREAD_STORE_CLEANUP_INTERVAL_HOURS` |
| `[chat_starters]` | `prompts` | チャット開始時に表示する定型文ボタン（クリックでそのまま送信、複数指定可） | `CHAT_STARTER_PROMPTS` |
| `[chat_starters]` | `prompts_en` | 英語表示のセッションで使う定型文ボタン（書式は `prompts` と同じ。空なら `prompts` を使う） | `CHAT_STARTER_PROMPTS_EN` |
| `[checkpointer]` | `op_timeout_seconds` | LangGraphの会話状態SQLite（`[paths].checkpoint_db`）に対する1回あたりの操作（aget_tuple/aput_writes等）タイムアウト秒数。超過時はcheckpointer再構築へフォールバック | `CHECKPOINTER_OP_TIMEOUT_SECONDS` |
| `[checkpointer]` | `close_timeout_seconds` | checkpointer再構築時に旧DB接続をクローズする際のタイムアウト秒数 | `CHECKPOINTER_CLOSE_TIMEOUT_SECONDS` |
| `[checkpointer]` | `shutdown_drain_timeout_seconds` | アプリシャットダウン時、保留中の非同期タスクの完了を待つ最大秒数 | `CHECKPOINTER_SHUTDOWN_DRAIN_TIMEOUT_SECONDS` |
| `[mcp]` | `enabled` | MCPサーバー自動接続機能の有効/無効（既定値。`.locohane/settings.json` の `"mcp"."enabled"` があればそちらが優先） | `MCP_ENABLED` |
| `[mcp]` | `settings_path` | `.locohane/settings.json` のパス | `MCP_SETTINGS_PATH` |
| `[mcp]` | `connect_timeout_seconds` | 1サーバーあたりの起動（プロセス起動+initialize+tools/list）のタイムアウト秒数 | `MCP_CONNECT_TIMEOUT_SECONDS` |
| `[mcp]` | `call_timeout_seconds` | MCPツール（tools/call）1回あたりのタイムアウト秒数 | `MCP_CALL_TIMEOUT_SECONDS` |
| `[admin]` | `host` | 設定ダッシュボード（管理ツール）自身の待受ホスト。`instances/<name>/config_overrides.json` の上書き対象外 | `ADMIN_HOST` |
| `[admin]` | `port` | 管理ツール自身の待受ポート | `ADMIN_PORT` |
| `[admin]` | `ssl_certfile` / `ssl_keyfile` | HTTPS 用のサーバー証明書・秘密鍵（PEM）のパス。両方指定すると管理ツールと本体インスタンスが HTTPS で待ち受ける。両方空なら HTTP（既定）。「5. HTTPS で待ち受ける（任意）」参照 | `SSL_CERTFILE` / `SSL_KEYFILE` |
| `[admin]` | `session_timeout_minutes` | 管理ツールへのログインセッションの有効時間（分） | `ADMIN_SESSION_TIMEOUT_MINUTES` |
| `[admin]` | `backup_keep` | インスタンスごとの設定バックアップの保持世代数 | `ADMIN_BACKUP_KEEP` |
| `[admin]` | `stop_apps_on_exit` | 管理ツール終了時に、子プロセスとして起動した本体インスタンスも道連れで停止するか | `ADMIN_STOP_APPS_ON_EXIT` |
| `[admin]` | `assistant_enabled` | ダッシュボード右上の「AIに相談」（管理アシスタント）を有効にするか。false でボタンを出さず API も 404 | `ADMIN_ASSISTANT_ENABLED` |
| `[admin]` | `assistant_llm_instance` | 管理アシスタントの回答に使う LLM 接続先を取るインスタンス名。空欄なら相談対象インスタンス自身の `[llm]`。設定ダッシュボードの「管理者設定」で変えられる（インスタンス別には変更できない） | `ADMIN_ASSISTANT_LLM_INSTANCE` |
| `[admin]` | `assistant_max_keys` | 相談内容に近い config.ini のキーを AI に渡す最大件数 | `ADMIN_ASSISTANT_MAX_KEYS` |
| `[admin]` | `assistant_max_docs` | 相談内容に近い README_DETAIL.md・API リファレンスの段落を AI に渡す最大件数（0 で渡さない） | `ADMIN_ASSISTANT_MAX_DOCS` |
| `[admin]` | `assistant_use_embedding` | 相談内容に近いキー・段落を選ぶのに埋め込み（`[vector_db] embedding_url`）を使うか。false なら文字2-gram だけ | `ADMIN_ASSISTANT_USE_EMBEDDING` |
| `[admin]` | `assistant_embedding_instance` | 埋め込みの接続先を取るインスタンス名。空欄なら自動で探す | `ADMIN_ASSISTANT_EMBEDDING_INSTANCE` |
| `[admin]` | `assistant_max_history_messages` | AI に渡す直近の会話の最大メッセージ数 | `ADMIN_ASSISTANT_MAX_HISTORY_MESSAGES` |
| `[admin]` | `instances_dir` | インスタンス別ディレクトリのルート（既定 `./instances`）。app.bat直接起動時のdefaultインスタンスの`.env`・`config_overrides.json`の読み込み先にも使われる。変更時は既存の中身を手動で移動する | `INSTANCES_DIR` |
| `[data_backup]` | `enabled`・`dest_dir`・`times`・`weekdays`・`keep`・`exclude_workdir`・`exclude_logs`・`exclude` | データのバックアップ（「データバックアップ」節）の既定値。全インスタンス共通（インスタンス別に上書きできない）。設定ダッシュボードで保存した値（`<instances_dir>/data_backup.json`）が優先する | - |
| `[ui]` | `default_language` | 表示言語の既定値（`auto`＝ブラウザの言語 / `ja` / `en`）。利用者が画面で言語を選ぶとそちらが優先される。AI も選んだ言語で回答する（「言語切り替え（日本語 / 英語）」節） | `UI_DEFAULT_LANGUAGE` |
| `[ui]` | `max_display_messages` | チャット画面に描画するメッセージの最大件数（表示専用の間引き、`0`で無制限） | `UI_MAX_DISPLAY_MESSAGES` |
| `[ui]` | `max_display_side_steps` | サイドパネルに描画するツール呼び出し等のStepの最大件数（表示専用の間引き、`0`で無制限） | `UI_MAX_DISPLAY_SIDE_STEPS` |
| `[ui]` | `paste_as_attachment_threshold_chars` | 入力欄へこの文字数以上のテキストを貼り付けると、入力欄へ展開せず「貼り付けテキスト」カード（`pasted-text-*.txt` 添付）にする。送信時はパスではなく本文としてLLMへ渡す（`0`以下で無効） | `UI_PASTE_AS_ATTACHMENT_THRESHOLD_CHARS` |
| `[ui]` | `max_input_chars` | 1回の送信の最大文字数（入力欄＋貼り付けテキストの合計）。超過中は送信不可、バックエンドも拒否する（`0`以下で無制限） | `UI_MAX_INPUT_CHARS` |
| `[ui]` | `token_usage_warn_threshold` | トークン使用量カードの「リクエスト1回あたり」行（メインと実行中のサブエージェント/一括委譲のグループごとに1行。コンテキスト圧縮の要約呼び出しは「圧縮処理（直近1回）」行として出し、会話累計にも加算する。行ごとに判定）の合計トークン数がこの値以上でオレンジ太字表示（`0`以下で無効） | `UI_TOKEN_USAGE_WARN_THRESHOLD` |
| `[ui]` | `token_usage_alert_threshold` | 同上、この値以上で赤太字表示（`token_usage_warn_threshold`より優先、`0`以下で無効） | `UI_TOKEN_USAGE_ALERT_THRESHOLD` |
| `[websocket]` | `ping_interval_seconds` | ブラウザ⇔サーバー間WebSocket（Socket.IO）の生存確認ping送信間隔秒数。`[llm].stream_chunk_timeout_seconds`（LLMサーバーとの通信）とは別レイヤー | `WEBSOCKET_PING_INTERVAL_SECONDS` |
| `[websocket]` | `ping_timeout_seconds` | 直近pingへの応答をこの秒数待っても受信できない場合に切断とみなす。LLM応答待ちでイベントループがブロッキング気味の時間帯（`dispatch_agent`の長時間実行中等）に短すぎると誤切断しやすい | `WEBSOCKET_PING_TIMEOUT_SECONDS` |

環境変数が設定されていれば `config.ini` の値より優先される（詳細は `src/config.py` を参照）。

`provider` ごとの reasoning 系パラメータの対応表は `src/llm/dialect.py` の `build_extra_body()` を参照。対応表は `tools/probe_reasoning_dialect.py` で実測済み（llama-server b10437 / Qwen3.8、vLLM 0.30.0 / Qwen3.5-9B-AWQ）。サーバーのバージョンやモデルを変えたときは、`python tools/probe_reasoning_dialect.py --base-url <URL>/v1 --model <モデル名> --provider <llama_cpp|vllm>` で確認できる。

## ログイン認証の設定

`[auth] enabled = true` にするとログイン画面が表示され、ユーザー名/パスワードでの
ログインが必須になる。ユーザー名・パスワードの実体、および JWT 署名鍵は
**機密情報のため config.ini には置かず、`.env`（環境変数）で管理する**。

1. `chainlit create-secret` を実行し、出力された値を `.env` の
   `CHAINLIT_AUTH_SECRET` に設定する（`[auth] enabled = true` の場合は必須。
   未設定だと起動時にエラーで止まる）。
2. `.env` の `AUTH_USERS` にログイン可能なユーザー名/パスワードの組を
   `[["ユーザー名","パスワード"], ...]` の Python リスト形式で登録する
   （書式・記入例は `.env.example` を参照）。複数行に分けて見やすく書きたい
   場合は、値全体をシングルクォート(`'`)で囲むこと（python-dotenv の仕様上、
   クォート無しの値は複数行にできない）。例:
   ```
   AUTH_USERS='[
       ["alice", "password1"],
       ["bob", "password2"]
   ]'
   ```
3. `config.ini` の `[auth]` で `enabled` / `require_password` を設定する。
   `require_password = false` の場合、`AUTH_USERS` に登録済みのユーザー名で
   あればパスワードの内容は問われない。
4. 独自フロントエンド（`frontend/`）にログインフォームを実装しているため、
   フロントエンドを変更した場合は `cd frontend && npm run build` で
   再ビルドし `public/build` へ反映する。

**`.env` は絶対にコミットしないこと**（`.gitignore` 済みだが、値の取り扱いに注意）。

## 会話ログの記録

`[chat_log] enabled = true` にすると、ユーザー発言とAIの最終応答（ツール実行の
詳細・thinkingは含まない）を、データベースを使わずテキストファイルへ記録する。

- 保存先: `[chat_log].dir`（既定 `./data/logs_chat`）配下に
  `<ユーザー名>/<日時>_<thread_id>.log`（日時は秒単位まで含む）の構成で書き出す。
- ユーザー名はログイン中のユーザー識別子（`[auth]` 参照）。`[auth] enabled = false`
  で未ログインの場合は `anonymous` にまとめる。
- 1つの会話（thread_id）につき1ファイルへ追記し続ける（日をまたいでも
  ファイルは変わらない）。左サイドバーからスレッドを再開した場合も、同じ
  thread_id の既存ファイルへ追記を継続する（新規ファイルには分割しない）。
  再開したタイミングは `----- resumed YYYY-MM-DD HH:MM:SS -----` という区切り行で
  ファイル内に記録される。

## 会話スレッド一覧・再開（左サイドバー）

`[thread_store] enabled = true`（既定）にすると、画面左に過去の会話一覧が
サイドバー表示され、選ぶとLangGraphの会話状態ごとその続きを再開できる
（ChatGPT/Claude 同様）。実装は `src/thread_store.py`（Chainlit の
`BaseDataLayer` を実装した軽量な独自ストア、`data/chat_threads.sqlite`）と
`frontend/src/components/Sidebar.tsx`。

- **所有者の分け方**: `[auth] enabled = false`（既定）では全会話を
  `anonymous` という1つのバケットにまとめ、単一の一覧として表示する
  （`[chat_log]` と同じ挙動）。`[auth] enabled = true` ではログインユーザー
  ごとに自分のスレッドのみが見える。
- **一覧・リネーム・削除**: 独自の `GET/PUT/DELETE /locohane/threads*`
  エンドポイント（`app.py`）を使う。Chainlit公式の `/project/threads*` は
  匿名モードで常に401になり使えないため採用していない。
- **再開の仕組み**: Chainlit自身のスレッドID（`cl.context.session.thread_id`）
  をそのままLangGraphのチェックポイント分離キーとして使う（`on_chat_start`）。
  再開時は同じIDが渡されるため、LangGraph側の会話状態が自動的に引き継がれる。
  `@cl.on_chat_resume` は `on_chat_start` の軽量版で、ウェルカムメッセージ等は
  再送しない（過去のメッセージは resume 時に自動再生されるため）。
- **匿名モードでの再開有効化**: Chainlit本体の `resume_thread()`
  （`chainlit/socket.py`）は `session.user` が `None` だと無条件で
  早期returnする設計のため、匿名モードのままではスレッド再開が発火しない。
  `app.py` の `_patch_chainlit_anonymous_resume()` が、匿名モード時のみ
  `chainlit.socket.resume_thread` をモジュール関数単位で差し替え、再開時に
  一時的な `anonymous` ユーザーを補ってから元の実装を呼ぶ。
- **添付ファイル・埋め込み画像の永続化**: `provide_download`/`analyze_image`の
  `show_in_chat=True` 等の添付ファイル、および回答本文へ直接埋め込まれた画像は、
  `data/elements/`（`[elements]`参照）へ実体をコピー保存し、再開したスレッドや
  プロセス再起動後も表示できる（`src/thread_store.py` の `create_element`/
  `get_element`/`delete_element`、`app.py` の
  `_embed_local_images_as_session_urls`）。ただし `analyze_image` を
  `show_in_chat=False`（既定）で呼んだ場合はチャットUIへ表示しないため、
  この永続化とは無関係。

---

## 作業ディレクトリの切り替え（ツールバー）

`run_script`/`execute_python_code` が読み書きする作業ディレクトリは、既定では
サンドボックス化された `[default_workdir]` 配下の `_tmp_<name>`
（前述「データの保存場所」参照）だが、チャット入力欄ツールバーの
フォルダアイコン（`frontend/src/components/WorkDirButton.tsx`）から、
会話（スレッド）単位でユーザー自身の実プロジェクトフォルダ等へ絶対パスで
切り替えられる。

- **入力方式**: OSネイティブのフォルダ選択ダイアログではなく、絶対パス文字列を
  直接入力するポップオーバー方式（サーバーとブラウザが別マシンの構成だと
  ネイティブダイアログがサーバー側にしか表示されずクライアントが固まって
  見える問題があったため）。「既定値に戻す」で `config.ini` の
  `[default_workdir].dir` ベースの設定へ戻せる。
- **変更可能なタイミング**: 新規チャットで一度もメッセージを送信していない間、
  または未送信のまま再開したスレッドに限る。送信済みのチャットではアイコンが
  無効化される（送信後に作業フォルダを変えると挙動が分かりにくくなるため）。
- **検証とフォールバック**: 設定時にサーバー側で実際にディレクトリ一覧
  取得・一時ファイルの作成/書き込み/削除を試みて読み書き可否を判定する
  （`src/tools/check_work_dir_status.py` の `probe_workdir_access`）。存在しない・
  読み取り不可・（書き込みが必要な場面では）書き込み不可と判定された場合は、LLMが確認を
  怠っても安全側に倒れるよう自動的に `default_workdir` へフォールバックする。
  判定結果は `check_work_dir_status` ツールでも確認できる。
- **スコープ**: `cl.user_session`（会話単位）に保存されるだけで、
  `config.ini` 自体は変更されない。他の会話・他ユーザーには影響しない。

---

## 推論の深さの切り替え（ツールバー）

チャット入力欄ツールバーのメーターアイコン（`frontend/src/components/ReasoningEffortButton.tsx`）から、
`[llm].reasoning_effort` を利用者ごとに上書きできる。

- **選択肢**: 「config.ini の設定」（既定。`[llm].reasoning_effort` をそのまま使う）・`none`・`minimal`・`low`・
  `medium`・`high`・`xhigh`・`max`。受け付ける値はモデルのテンプレート次第で、対応外の値はリクエストがエラーになる
  （「設定リファレンス」の `reasoning_effort` 参照）。
- **反映のしかた**: 選んだ値はブラウザの localStorage（`la-reasoning-effort`）に保存し、送信のたびにメッセージの
  metadata（`reasoning_effort`）で送る。`app.py` の `on_message` が値の変化を検知してグラフを組み直す
  （`src/config.py` の `with_reasoning_effort()`）。メインエージェントと `dispatch_agent` のサブエージェントに効き、
  コンテキスト圧縮の要約には効かない。
- **変更可能なタイミング**: 生成中（`askUser` の応答待ちを含む）はアイコンが無効化される。送信時点の値でそのターンが
  確定するため、ターンの途中で切り替わることはない。
- **スコープ**: ブラウザ単位で保存され、`config.ini` 自体は変更されない。

---

## 言語切り替え（日本語 / 英語）

チャット画面・設定ダッシュボード・AI の応答を、利用者ごとに日本語と英語で切り替えられる。

- **選び方**: チャット画面は右上の表示設定（パレットアイコン）→「言語 / Language」、またはログイン画面の下部。
  設定ダッシュボードは右上（ログイン画面は下部）の選択欄（切り替えると再読み込みする）。選んだ言語はブラウザの
  localStorage（`la-language`）に保存される。チャット画面と設定ダッシュボードはポートが違う（別オリジン）ため、
  それぞれで選ぶ。
- **既定値**: まだ選んでいない利用者は `[ui] default_language`（`auto` ならブラウザの言語。英語なら英語、それ以外は日本語）。
  設定ダッシュボードの config.ini 画面では選択式で変更できる。
- **LLM 向けの文言は英語ベース**: `system_prompt/` 配下のプロンプト・`agents/*.md`・ツールの docstring と戻り値
  （エラーの接頭辞は `Error:`、スクリプトの出力は `[exit code]`・`[stdout]`・`[stderr]`）・同梱スキル（SKILL.md・
  references・スクリプトのメッセージ）・管理アシスタントとスキル研究室のプロンプトは英語で書く。
- **AI の応答言語**: セッションの表示言語に合わせ、`system_prompt.md` の末尾に応答言語の指示（`src/i18n.py` の
  `response_language_directive()`。日本語・英語とも付ける）を足してグラフを組み直す。会話の途中で切り替えた場合は、
  次の送信から切り替わる（生成中の応答はそのまま）。管理アシスタントも同様に、ダッシュボードの表示言語で回答する
  （`admin/i18n.py` の `response_language_instruction()`）。evals はセッション外で動くため、既定の言語（日本語）の指示が付く。
- **ログは英語**: アプリログ（`app_*.log`）・管理ツールのログは英語で出す。モニターはローテーション前の日本語のログ形式も読める。
- **config.ini の説明**: 正本は config.ini の日本語コメント。設定ダッシュボードを英語で表示すると、英訳カタログ
  （`admin/ini_catalog_en.py`）の説明・グループ見出しを返す。config.ini のキーを追加・説明を変更したら、カタログも
  合わせて更新する（`tests/test_admin_ini_catalog_en.py` が全キーの英訳の有無を確かめる）。
- **翻訳されないもの**: 送信済みのメッセージ（送った時点の言語のまま残る）、開発者向けのコードコメントと Python の
  docstring、`skills/SKILLS_README.md` などの開発者向けドキュメント。
- **ウェルカムメッセージとヘルプ**: `public/settings/welcome.md`・`system_prompt/help.md` は言語別のファイルを持たず、
  英語の1本だけ（help は LLM が利用者の言語で伝える）。日本語にしたい場合は設定ダッシュボードの「表示設定」で
  welcome.md を書き換える。英語の定型文ボタンは `[chat_starters] prompts_en`。

### 文言の置き場所と翻訳の追加方法

| 対象 | 置き場所 | 書き方 |
|------|----------|--------|
| チャット画面（React） | `frontend/src/i18n/messages.ts` | キー（`'composer.placeholder'` 等）ごとに `ja`・`en` を書き、コンポーネントで `useI18n().t('キー')`。変更後は `npm run build` |
| チャットのシステム通知・ボタン・Step 名（Python） | `src/i18n.py` の `MESSAGES` | キーごとに `ja`・`en` を書き、`i18n.t("キー")`。セッションの言語は `cl.user_session` の `lang` |
| 設定ダッシュボード（画面） | `admin/static/i18n_en.js` | 日本語の原文をそのままキーにする。JS では `_t("原文")`（可変部分は `_t("{0} を停止します", name)`）、`index.html` のテンプレートは `clone()` が自動で訳す |
| 設定ダッシュボード（サーバーのエラー） | `admin/i18n.py` の `EN` | 日本語の原文をキーにし、`_t("インスタンス {0!r} が見つかりません。", name)`。言語は `X-Locohane-Lang` ヘッダー（`admin.js` の `api()` が付ける） |

訳を書き忘れた文言は日本語のまま表示される（落ちはしない）。`tests/test_i18n.py`・`tests/test_admin_i18n.py`・`tests/test_admin_ini_catalog_en.py` が、
キーの英訳の有無とプレースホルダー（`{0}` 等）の一致を確かめる。

`🔢 トークン使用量` などのメッセージの接頭辞（`app.py` の `*_PREFIX` と `frontend/src/utils/messageTree.ts`）や
`SUBAGENT_MESSAGE_AUTHOR` は、メッセージの種類を見分ける目印なので翻訳しない（表示はフロントエンドで訳す）。

---

## 設定ダッシュボード（管理ツール）

`config.ini`（`[admin]` を除く全セクション）・`public/settings/`（ヘッダー・
タブタイトル・ウェルカムメッセージ・アイコン）・ログインユーザーを、
テキストエディタを使わずブラウザのUIから変更できる独立した管理ツール
（`admin/` パッケージ、既定で `http://127.0.0.1:8001`）。Locohane本体
（`app.py`、既定で `http://127.0.0.1:8000`）とは別プロセス・別ポートで動く。

さらに、Locohane本体を**複数のインスタンスとして同時に起動・停止・
再起動**できる（例: 別のモデル・別の接続先で動く2つ目のインスタンスを
追加する等）。各インスタンスは会話履歴・永続メモリー・アップロード等の
データ、設定の上書き差分、ログインユーザーが完全に分離される。

![Dashboad](admin/Dashboad_01.png)

`config.ini` タブの左サイドバーには、よく変更するキーだけを絞り込んで
表示するプリセットボタンがある（もう一度押すと解除）。対象キーは
`admin/static/admin.js` の `CONFIG_PRESETS` で定義している。

- **並列数**: `[llm] max_concurrent_requests`・`[graph] max_parallel`・`[subagent] max_parallel`
- **トークン制御**: `[graph] token_guard_soft_threshold`・`[subagent] token_guard_soft_threshold`・`token_guard_hard_threshold`・`[context_trim] enabled`・`trigger_total_tokens`・`[context_compaction] enabled`・`token_threshold`・`single_request_token_threshold`・`pre_note_threshold`・`[token_ratio]` の各キー
- **スキル関係**: `[paths] project_locohane_dir`・`[subagent] agent_type_run_script_allowlist`・`[main_agent_tool_guard] allow_entries`・`[plan] plan_approval_exempt_scripts`・`[skill_creator]` の各キー・`[skill_lab] instance_locohane_dir`
- **ベクターDB**: `[vector_db]` の `enabled`・`backend`・`distance`・`qdrant_url`・`qdrant_preset`・`embedding_url`・`embedding_routing_strategy`・`embedding_max_retries`・`search_embedding_max_retries`・`embedding_retry_interval_seconds`・`embedding_encoding_format`・`query_prefix`・`document_prefix`・`image_input`・`image_input_extensions`・`embedded_images`・`embedded_image_min_px`・`image_dpi`・`office_timeout_seconds`・`ingest_temp_dir`・`ingest_workers`・`ingest_processes`・`ingest_embed_requests`・`scan_workers`・`max_files_per_collection`・`chunk_size`・`chunk_overlap`・`max_file_mb`・`top_k`・`max_result_chars`・`min_score`・`image_min_score`・`path_min_score`・`rrf_weight_text`・`rrf_weight_keyword`・`rrf_weight_image`・`rrf_weight_path_keyword`

その種類のインスタンスに出していないキーだけのプリセットは出さない（スキル調整ワーカーは出すキーが少ないため、プリセットは出ない）。

トークン制御系の閾値は、`[graph] token_guard_soft_threshold` を基準値とした相対比で連動させられる。
比率は `config.ini` の `[token_ratio]`（キー名＝自動入力先の `セクション.キー`、値＝比率。空欄なら連動させない）に
置き、他の設定と同じくインスタンスごとに変更できる。`[token_ratio] enabled = true` の間は、ダッシュボードで
基準値（または比率）を変更すると「基準値×比率」（整数に四捨五入）が各閾値へ自動入力され、通常の変更と同じく
「変更を確認」→「保存」で保存される。「トークン制御」プリセットの先頭にある「比率で再計算」ボタンは、
`enabled = false` でも今の基準値と比率で一括入力する。`[token_ratio]` はダッシュボード専用で、アプリ本体は
読まない（起動時に使われるのは各閾値に保存された値そのもの）。

### 起動方法

```cmd
admin.bat
```

起動すると `instances/default/` が無ければ自動的に作られ（`app.bat` と
同じ `127.0.0.1:8000`）、`autostart=true` のインスタンス（既定では
`default` のみ）を子プロセスとして起動する。管理ツールへは
`http://127.0.0.1:8001` からログインする（下記「ログイン」参照）。

管理ツール自身の待受ホスト・ポートは `admin.bat` 内の `ADMIN_HOST`/
`ADMIN_PORT` で指定する（`config.ini` の `[admin]` セクションより優先。
変更したい場合は `admin.bat` をテキストエディタで開いてこの2行を書き換える）。

従来通り `app.bat` で `default` インスタンスを単体起動することもできる
（`instances/default/instance.json` のホスト/ポートに追従する）。ただし
その場合、管理ツールからは「外部で起動中」と表示され、そこからの
再起動・停止はできない（多重起動によるデータ破損を防ぐ排他ロック
`src/instance_lock.py` の制約による）。

### ログイン

管理ツールへのログインは、Locohane本体のログインユーザー（後述）とは
別の名前空間で、**常にパスワード必須**（本体の `[auth].require_password`
の設定には左右されない）。プロジェクト直下の `.env` に

```
ADMIN_USERS=[["admin", "変更してください"]]
```

の形式（`AUTH_USERS` と同じ `[["ユーザー名","パスワード"], ...]` 形式）で
設定する（`.env.example` 参照）。未設定のままだと管理ツールは起動を拒否
する。同一IPからのログイン失敗が既定5回に達すると一時的にロックアウトする。

### 設定値の優先度と保存先

**config.ini は既定値として扱い、管理ツールは config.ini 自体を一切
書き換えない。** UIで変更した値は、インスタンスごとの
`instances/<インスタンス名>/config_overrides.json`
（`{"<セクション>": {"<キー>": "<config.iniに書くのと同じ形式の文字列>"}}`
のJSON。管理ツールで一度も保存していない間は存在しない）に記録する。

優先度（高い順）: **環境変数 > `config_overrides.json` > config.ini（既定値）**

（`.locohane/settings.json` の `"mcp"` ブロックは、これとは別に
`[mcp]` セクションの実効値をさらに上書きする、従来からの最優先設定）

`src/config.py` の `load_config()` が起動時にこの優先度で解決する
（`CONFIG_OVERRIDES_PATH` 環境変数で `config_overrides.json` のパスを
明示的に指定することもできる。省略時は `instances/default/
config_overrides.json`）。`[admin]` セクション（管理ツール自身の設定）・
`[data_backup]` セクション（「データのバックアップ」節）・「管理者設定」節の
全インスタンス共通のキーは上書き対象外で、config.ini（と管理者設定・環境変数）の
値だけを使う。これらはインスタンスの設定画面（config.ini タブ）にも表示しない
（`admin/instances.py` の `config_key_applies()`、`src/config.py` の
`is_instance_overridable()`）。

スキルの登録系のリスト（`[plan] plan_approval_exempt_scripts`・`[subagent]
agent_type_run_script_allowlist`・`[main_agent_tool_guard] allow_entries`）は、
`config_overrides.json` の値で config.ini を置き換えず、既定値に無い項目だけを
足す（`src/config.py` の `ADDITIVE_OVERRIDE_KEYS`）。アップデートで既定値に足された
新しいスキルを、保存済みの値でつぶさないため。既定値の項目は消せず、`allow_entries`
の上限回数も既定値のままになる（環境変数は従来どおり値全体を置き換える）。
設定画面では既定値と足した項目を合わせたリストを表示・編集し、保存時に既定値に無い
項目だけを `config_overrides.json` へ記録する（既定値の項目を消した・変えた場合は
保存時にエラー。`admin/overrides.py`）。

「管理者設定」へ移した全インスタンス共通のキー（`ADMIN_SETTING_KEYS`）に、以前
インスタンス別に保存した値が残っている場合は、設定画面に「効かない値」として表示し、
「既定値に戻す」で消せる。

保存前には `load_config()` 本体の型変換・バリデーションで実際に検証し、
失敗する値は保存されない。保存のたびに変更前の内容を
`instances/<name>/backups/` へバックアップし（`[admin].backup_keep`
世代まで保持）、UIから復元できる。

### インスタンス

`instances/<インスタンス名>/` 配下に、インスタンスごとの以下を持つ
（`.gitignore` 済み）。

```
instances/
├── default/                     # 既定インスタンス（削除不可）
│   ├── instance.json            # 表示名・ホスト・ポート・自動起動の有無
│   ├── config_overrides.json    # config.iniとの差分（初回保存時に生成）
│   ├── .env                     # インスタンス専用のログインユーザー等（任意）
│   ├── settings/                # インスタンス専用の表示設定（任意。下記参照）
│   ├── backups/                 # 保存・復元前のスナップショット
│   └── app_stdout.log           # 本体プロセスの標準出力
├── <他のインスタンス名>/
│   └── ...（同上）
├── admin_settings.json          # 設定ダッシュボードの「管理者設定」で保存した全インスタンス共通の設定（「管理者設定」節）
├── data_backup.json             # 設定ダッシュボードで保存したデータのバックアップの設定（「データのバックアップ」節）
├── _admin_logs/                 # 管理ツールの動作ログ（admin_*.log。ナレッジの取り込み・削除の経過等。[log] max_lines 行で切り替え）
└── admin_changes.log            # 全インスタンス共通の変更履歴（JSON Lines）
```

データ本体（会話履歴 `checkpoints.sqlite`・スレッド一覧・永続メモリー・
アップロード・ログ等。「データの保存場所と手動削除の手順」参照）は、
config.ini 既定の `[paths] common_data_dir = ./data/${instance}` により
`data/<インスタンス名>/` へ自動的に分かれる。管理ツールは子プロセス起動時に
環境変数 `LOCOHANE_INSTANCE=<インスタンス名>` を渡し、`app.bat` で直接
起動した場合は `default` になる。永続メモリーだけ全インスタンスで共有したい
場合は、`memory_dir` を `${instance}` を含まない固定パスに上書きすればよい。
新しく作るインスタンスの名前（表示名ではない方）は半角英数字・`_`・`-` の
**6文字以内**に制限している。名前がそのまま `data/<インスタンス名>/`・
`instances/<インスタンス名>/` 等のパスに入るため、長いと配下のスキル生成物等で
Windows のパス長上限（260文字）を超えやすいため（制限導入前に作った
`default` 等の既存インスタンスはそのまま使える）。作成時に既存インスタンスの設定を複製することもできる（設定の上書き・
`.env`・表示設定 `instances/<name>/settings/` を写す。`common_data_dir` の上書きは
`D:/data/${instance}` のように `${instance}` を含む場合だけ引き継ぎ、固定パスなら
複製元と同じ場所になるため引き継がない。引き継がなかった設定は、作成後にその名前と理由を表示する）。
作成済みのインスタンスへも、設定画面（config.ini タブ）の「他のインスタンスからコピー」で別のインスタンスの設定を写せる
（`admin/overrides.py` の `copy_from_instance()`）。コピー元と値が違う設定を一覧にし、選んだものだけを写す（コピー元で
既定値のままの設定は既定値に戻る）。`[paths] common_data_dir`（変えると会話履歴等の保存先が変わるため）と、コピー先の
種類で効かない設定はコピー先の値のまま。`.env`・表示設定は写さない。保存と同じ検証をし、前の内容は `backups/` へ退避する
（変更履歴には `config_copy` で残る）。ポート・
データ保存先が他インスタンスと重複する場合は、作成・起動時と、設定画面での
保存・バックアップ復元時にエラーとして拒否される。

インスタンスの削除では `instances/<name>/` を削除し、あわせて削除する
永続データを削除ダイアログで選択できる。候補はそのインスタンスの実効設定
（config.ini＋上書き）で解決した `common_data_dir`（データディレクトリ全体）・
`checkpoint_db`・`[thread_store] db`・`memory_dir`・`plans_dir`・`[log] dir`・
`[chat_log] dir`・`[elements] dir`・`[path_memory] dir`・
`[default_workdir] dir` で、パスごとにチェックボックスで選ぶ（SQLite は
`-wal`/`-shm` も一緒に消す）。`data/<そのインスタンス名>/` 配下は初期状態で
選択済み、プロジェクト外のパスは未選択で表示される。次のパスは誤削除防止の
ため選択できない: 存在しないパス、ドライブのルート・ホーム・プロジェクト・
`instances/` やその上位、プロジェクト内で `data/<そのインスタンス名>/` 配下
ではないパス、他インスタンスのデータパスや自他の `skills_dir`・`agents_dir`・
`project_locohane_dir`・`bin_path`・`allow_sandbox_dir` と同一・包含関係に
あるパス（他インスタンスの設定が読めない場合も確認できないため不可）。
データの削除に失敗した場合はインスタンス自体を残す。稼働中の
インスタンスと `default` は削除できない。設定（`config_overrides.json`）が
壊れていて状態を解決できないインスタンスは「設定エラー」と表示される。

表示名・ホスト・ポート・自動起動の有無・下記の `headless`/`watch` は、
作成後もインスタンスカードの「編集」から変更できる（ホスト・ポートの
変更は、稼働中の場合は再起動後に反映される）。

- **ヘッドレス**（`chainlit run` の `-h`/`--headless`。既定で有効）:
  管理ツール経由でインスタンスを起動するたびにブラウザタブが自動で
  開かないようにする。無効にすると起動のたびにブラウザが開く。
- **自動リロード**（`-w`/`--watch`。既定で無効）: ファイル変更を検知して
  自動リロードする開発用オプション。本番運用では無効のままにすること。

### ユーザー管理（.env）

| ファイル | 用途 | 管理ツールでの編集 |
|---|---|---|
| プロジェクト直下 `.env` | 管理ツール自身のログイン情報（`ADMIN_USERS`）と、全インスタンス共通の環境変数 | 不可（手動編集） |
| `instances/<name>/.env` | そのインスタンスのログインユーザー（`AUTH_USERS`）・JWT署名鍵（`CHAINLIT_AUTH_SECRET`）・任意の環境変数 | 可（「ユーザー」「環境変数」タブ） |

優先度は **`instances/<name>/.env` > OS環境変数 > プロジェクト直下 `.env`**。
`app.py` の先頭で、環境変数 `LOCOHANE_INSTANCE_ENV`（管理ツールが子プロセス
起動時に設定。未設定時は `instances/default/.env`）が指す `.env` を
`override=True` で読み込んでから、Chainlit自身がプロジェクト直下 `.env` を
`override=False` で読む（後から読む方が「未設定のキーだけ補う」動作になる
`python-dotenv` の挙動を利用している）。そのため `app.bat` で直接起動して
も `default` インスタンスの `.env` が使われ、管理ツール経由の起動と同じ
挙動になる。

「環境変数」タブでは、`LOCOHANE_INSTANCE`・`LOCOHANE_INSTANCE_ENV`・
`CONFIG_OVERRIDES_PATH`・`COMMON_DATA_DIR`・`CHECKPOINT_DB` は設定できない
（管理ツールは重複チェックや稼働判定の際にインスタンスの `.env` を読まない
ため、これらで保存先やインスタンス名を変えると判定と食い違う。データ保存先は
設定画面の `[paths]` から変更する）。値は常に引用符付きで書き込むため、
空白や `#` を含むパスワード・値もそのまま保存できる。

インスタンス専用のユーザーを1人も設定していない間は、プロジェクト直下
`.env` の `AUTH_USERS` を継承しているものとしてUIに表示される。ユーザーを
追加すると、そのインスタンス専用の設定に切り替わる（以後は継承しない）。
継承中のユーザーは管理者設定扱いのため、「プロジェクト直下 .env（管理者設定）」
バッジ付きでグレーアウト表示され、パスワード変更・削除はできない（API も 403 で
拒否する）。変更する場合はプロジェクト直下の `.env` を直接編集する。

### 表示設定（共通 + インスタンス別）

header.md・tab_title.md・welcome.md・icon・favicon は、トップの「表示設定」
タブで **共通**（`public/settings/`）を、インスタンス詳細画面の「表示設定」
サブタブで **そのインスタンス専用**（`instances/<name>/settings/`）を
編集する。各インスタンスは、自分の `settings/` にあるファイルを優先し、
無い項目は共通設定を使う（icon・favicon は拡張子違いを1組として扱い、
インスタンス側に1つでもあれば共通側は使わない）。インスタンス専用の設定は
「共通に戻す」で削除できる。

`app.py` が `/public/settings/{ファイル名}` を Chainlit 標準の静的配信より
手前で処理して、この優先順位で返す。変更は **再起動不要** で、ブラウザの
再読み込み（welcome.md は次のチャット開始）で反映される。

### モニター（稼働状況・会話閲覧・トークン推移・ログ）

インスタンスカードに接続中のセッション数・ユーザー名・生成中スレッド数を
表示し、生成中のスレッドがあれば今回の生成開始以降の LLM リクエストごとの
入力トークン（コンテキスト長。メインとサブエージェントのうち最大の値）を
スレッドごとに色分けした折れ線グラフで表示する。「モニター」ボタン（またはインスタンス詳細の「モニター」「会話」
「ログ」サブタブ）から次を閲覧できる。すべて読み取り専用で、本体の動作には
影響しない。

- **モニター**: 接続中のユーザー・セッション（ブラウザタブ）、生成中の
  スレッド（経過時間・ユーザーの応答待ちか）、直近24時間の WARNING/ERROR
  件数（5秒ごとに自動更新）。LLM接続先（`[llm] main_url`/`sub_url`）の
  到達確認と、`provider="llama_cpp"` ならスロット使用状況。
- **会話**: ユーザー別のスレッド数・トークン累計・最終利用日時、スレッド一覧
  （ユーザー・名前で絞り込み）。スレッドを選ぶと会話内容と、その会話の
  累積トークン使用量の推移グラフ（全体＝サブエージェント・圧縮処理を含む／
  メインエージェントのみ）・LLMリクエストごとの入力トークン（コンテキスト長）
  のグラフを表示する。コンテキスト圧縮の実行位置は破線で示す。累計はログの
  各呼び出しを管理ツール側で足し上げた値で、本体のセッション内カウンタ
  （生成中に切断したスレッドを再開すると0から数え直し、圧縮のたびに
  メイン分を0に戻す）とは独立している。会話内容は既定で
  ユーザー発言とAIの応答のみで、ツール実行・思考も表示に切り替えられる。
  **会話内容を開くたびに、閲覧した事実（閲覧者・対象スレッド・所有者）を
  変更履歴へ `conversation_view` として記録する。**
- **ログ**: `[log] dir` の `app_*.log` をレベル・文字列・thread_id で絞り込んで
  新しい順に表示する。

データの取得元:

| 表示 | 取得元 |
|---|---|
| 接続中セッション・生成中スレッド | `<common_data_dir>/runtime_status.json`（本体が3秒ごとに変化があれば書き出し、終了時に削除。`src/runtime_status.py`） |
| スレッド一覧・会話内容 | `[thread_store] db`（`[thread_store] enabled=false` だと閲覧できない） |
| トークン推移 | `app_*.log` の `token usage thread_id=...` 行（会話本体の呼び出し）、`compaction token usage` 行（要約・write_thread_note 強制実行。`src/context_compaction.py`）、`context compacted` 行（破線の位置）。ローテーション前の日本語の行（`トークン使用量`・`圧縮処理トークン使用量`・`コンテキスト圧縮を実行しました`）も読む。`[llm] track_token_usage=true` の場合のみ出力され、ログがローテーション・削除された分は表示されない |

本体と HTTP で通信せずファイルを読む方式にしているのは、`app.bat` で直接
起動した（管理ツールが起動に関与しない）インスタンスでも同じように見える
ようにするためと、管理ツールと本体の間に認証用の秘密を共有する仕組みが
無いため（本体は `0.0.0.0` で公開されうる）。

### 管理者設定

上部の「管理者設定」タブで、インスタンス別には変えられない全インスタンス共通の設定を変える（`admin/admin_settings.py`）。
保存した値は `<instances_dir>/admin_settings.json` に持ち、`config.ini` より優先する（`load_config()` が config.ini の後・
インスタンス別の `config_overrides.json` の前に重ねる。各項目に「この画面で設定」「config.ini の値」「環境変数で上書き中」の
どれかを出し、環境変数で上書きされている項目は編集できない）。「config.ini の値に戻す」で画面の値を消す。
管理ツールを再起動しなくても、次の取り込み・次の相談から効く。変更は変更履歴（`admin_settings_save`）に残る。

| 項目 | キー | 内容 |
|---|---|---|
| 取り込みの並列数 | `[vector_db] ingest_workers` | 全インスタンス・全コレクションの取り込みの合計で、同時に準備するファイル数（「文書の取り込み」参照） |
| 本文の抽出・画像化の子プロセス数 | `[vector_db] ingest_processes` | 0 ならスレッドで行う。1 以上なら同時に準備するファイル数もこの値。画面にこの PC のコア数を出す |
| 埋め込みのリクエストの並列数 | `[vector_db] ingest_embed_requests` | 全インスタンス・全コレクションの取り込みの合計で、同時に送る埋め込みのリクエスト数。0 なら取り込みの並列数と同じ |
| 取り込み元フォルダの走査の並列数 | `[vector_db] scan_workers` | 取り込み元フォルダを走査するときに同時に読むフォルダの数（ファイルサーバー上のフォルダで効く） |
| 1コレクションの最大ファイル数 | `[vector_db] max_files_per_collection` | 超えた分は取り込まず、ナレッジ画面に警告を出す（管理ツールのメモリーを使いすぎないための上限） |
| 1ファイルの上限サイズ（MB） | `[vector_db] max_file_mb` | 超えるファイルは取り込まず、失敗一覧に理由を載せる |
| Office の PDF 書き出しの上限秒数 | `[vector_db] office_timeout_seconds` | `image_input = true` で Office ファイルを PDF に書き出す1回の上限秒数。超えたら Office のプロセスを終了し、そのファイルを失敗一覧に載せる |
| 取り込みの一時ファイルの置き場所 | `[vector_db] ingest_temp_dir` | 取り込みの間だけ使う一時ファイル（ページ画像・取り出した画像・Office から書き出した PDF）を置くフォルダ。空ならシステムの一時フォルダ（`%TEMP%`） |
| Qdrant への送り直しの回数 | `[vector_db] qdrant_max_retries` | `backend = qdrant` で接続できない・接続を切られた・タイムアウトした・一時的なエラーが返ったときに送り直す回数。`-1` なら無制限（取り込み・コピーは「停止」で止められる）、`0` なら送り直さない。検索は最大1回 |
| Qdrant への送り直しの間隔（秒） | `[vector_db] qdrant_retry_interval_seconds` | 上記の送り直しで次に送るまで待つ秒数（0 より大きい数）。検索は最大1秒 |
| 回答に使う LLM | `[admin] assistant_llm_instance` | 「AIに相談」の回答を作る LLM を、どのインスタンスの `[llm] main_url` から取るか。空なら相談対象のインスタンス自身。インスタンスを選んで「接続を確認」を押すと、その `main_url` の各接続先へ `GET /models` を送ってつながるかを確かめる（モデルは動かさない） |
| 関係する設定を探す埋め込み | `[admin] assistant_embedding_instance` | 相談内容に近い設定・説明を選ぶ埋め込みサーバーを、どのインスタンスの `[vector_db] embedding_url` から取るか。空なら自動 |

「AIに相談」で「LLM から応答を得られませんでした」となるとき（最初のセットアップで、相談対象のインスタンスの LLM が
止まっている・未設定のとき等）は、ここで「回答に使う LLM」に動いている LLM を持つインスタンスを選ぶ。

### データのバックアップ

上部の「データバックアップ」タブで、`[admin] instances_dir` と全インスタンスの `[paths] common_data_dir` を、日時つきの zip
（`locohane_backup_YYYYMMDD_HHMMSS.zip`）にまとめて保存先へ退避する（`admin/data_backup.py`）。データ本体（SQLite）は
ローカルディスクに置く必要があるため（下記）、ファイル共有へはこのバックアップで退避する。

- **設定**: `config.ini` の `[data_backup]`（保存先 `dest_dir`・自動バックアップ `enabled`・時刻 `times`・曜日 `weekdays`・残す世代数 `keep`・
  除外 `exclude_workdir`／`exclude_logs`／`exclude`）が既定値で、画面で保存した値は `<instances_dir>/data_backup.json` に持ち、そちらを
  優先する（各項目にどちらの値かを表示する。「config.ini の既定に戻す」で画面の値を消す）。全インスタンス共通の設定で、インスタンス別の
  `config_overrides.json` では上書きできない。保存先は絶対パス（UNC パス可）で、バックアップ元の中は指定できない。
- **中身**: zip の中は `instances/…`・`data/<インスタンス名>/…` と `manifest.json`（版・元の場所・件数・バックアップできなかったファイル）。
  `common_data_dir` の外に置いた `checkpoint_db`・`[thread_store] db`・`[vector_db] dir` は `data/<インスタンス名>/_external/<キー名>/` に入る。
  既定では作業フォルダ（`[default_workdir] dir`）とログ（`[log] dir`・`[chat_log] dir`・`instances/_admin_logs/`・`app_stdout.log`）を除く。
  SQLite（会話履歴・スレッド一覧・ナレッジ等）は使用中でも壊れないよう SQLite のバックアップ機能で写し、`-wal`・`-shm`・`-journal` は入れない。
  Qdrant のデータ（Qdrant サーバー側）は含まない（Qdrant のスナップショット機能で別に退避する）。
- **実行**: 自動バックアップは管理ツールが起動している間だけ、指定の時刻・曜日に動く（止まっていた間の分は後から実行しない）。
  「今すぐバックアップ」で手動でも実行でき、実行中は進捗と「停止」が出る。作成中は `.partial` に書き、完了したら改名する
  （途中で止めた・失敗した zip は残らない）。`keep` 世代を超えた古い zip は消す。経過は管理ツールのログ（`instances/_admin_logs/admin_*.log`）と
  変更履歴（`data_backup_start`・`data_backup_end`）に残る。
- **復元**: 管理ツールとインスタンスを止めてから zip を展開し、`instances/` を `[admin] instances_dir` へ、`data/<インスタンス名>/` を
  各インスタンスの `common_data_dir` へ戻す（元の場所は `manifest.json` にある）。

### 管理アシスタント（AIに相談）

ダッシュボード右上の「AIに相談」から、管理者の困りごと（「2人同時に使うと待たされる」「チャット画面にログインできない」等）を
相談すると、**どの画面のどこを開き、どの設定キーをどう変えればよいか**を AI が助言する（`admin/assistant.py`・`admin/static/assistant.js`）。

- **AI は設定を読むだけ**で、保存・起動停止・ユーザー操作などは一切行わない（そのための API を AI に渡していない）。操作は管理者が画面で行う。
- 回答に出てきた設定キーには「`[section] key` を開く」ボタンが付き、押すとそのインスタンスの config.ini 画面を、検索欄にキーを入れた状態で開く。
- パネル上部の「対象」で相談対象のインスタンスを選ぶ（開いた時点でインスタンス詳細を見ていればそのインスタンス、無ければ前回の選択か `default`）。
- 会話履歴はブラウザのタブ（sessionStorage）にだけ残り、サーバーには保存しない。変更履歴（監査ログ）にも記録しない。
- AI へのプロンプトと画面ガイドは英語で書き、末尾の応答言語の指示（`admin/i18n.py` の `response_language_instruction()`）でダッシュボードの表示言語に合わせて回答させる。

AI に渡すもの（回答のたびに組み立て直す）:

| 内容 | 出どころ |
|---|---|
| 画面ガイド（タブ・サブタブの役割、よくある困りごとと見る場所） | `admin/ASSISTANT_GUIDE.md`（毎回読み直すため、編集すれば再起動不要で反映） |
| 相談内容に近い config.ini のキー（説明・既定値・現在値・選べる値・環境変数で上書き中か） | 相談対象インスタンスの実効値。`[admin] assistant_max_keys` 件まで（下の検索で選ぶ。会話中で `[section] key` と名指しされたキーは必ず含める） |
| 相談内容に近いドキュメントの段落 | README_DETAIL.md（ライセンス・ファイル構成など管理の相談に関係しない節は除く）と API リファレンスを見出しごと（最大1200文字程度）に分けたもの。`[admin] assistant_max_docs` 件まで。回答の下の「参考にした説明」に一覧が出る |
| 管理者が今開いている画面、インスタンス一覧（種類・稼働状態） | 画面の状態 |

config.ini の説明文やドキュメントは全部で数十万字あり、ローカルの小さめのモデルにそのまま渡すと入力が長すぎるため、
相談内容で検索して絞って渡す（簡易 RAG、`admin/assistant_index.py`）。索引は config.ini の全キー（キーの説明に、
セクション冒頭の説明と「設定リファレンス」表の説明も付ける）と上の段落で、元のファイルが変われば次の相談で作り直す。
ダッシュボードを英語で表示しているときは、英語の相談と文字・意味が重なるよう、`README_DETAIL.en.md`・`admin/API_REFERENCE.en.md`・
キーの説明の英訳（`admin/ini_catalog_en.py`）で作った別の索引を使う（埋め込みのキャッシュも言語ごとに別ファイル）。

- **埋め込み**（既定）: ナレッジと同じ埋め込みサーバー（`[vector_db] embedding_url`）で相談内容と各キー・段落の意味の近さを測り、
  文字2-gram の類似度（下記）を 0.2 倍して足す。「ナレッジのファイル容量」と `[vector_db] max_file_mb`（1ファイルあたりの最大サイズ）の
  ように、言い回しが違って文字が重ならない相談にも当たる。接続先は `[admin] assistant_embedding_instance` のインスタンスの
  `embedding_url`（空欄なら、回答に使う LLM のインスタンス → 相談対象 → それ以外の名前順で、`embedding_url` が設定されている最初のもの。
  `[vector_db] enabled` が false でも `embedding_url` があれば使う）。文書側の埋め込みは `instances/_assistant_index/` に保存し、
  変わった段落だけを埋め込み直す（初回だけ全段落の埋め込みに数十秒かかる）。
- **文字2-gram**（埋め込みサーバーが無い・接続できない・`[admin] assistant_use_embedding = false` のとき）: パスメモリーの類似度検索
  （`src/path_memory.py`）と同じく、文字を2文字ずつに分けた出現頻度のコサイン類似度で選ぶ（多くの段落に出る2文字ほど軽くする TF-IDF の重み付き）。
  言い回しが違う相談は漏れやすい。

困りごと→正解のキーの評価（開発用33問・作成時に見ていない20問）では、上位12件に正解が入った割合が、
文字2-gram だけで 27/33・16/20、埋め込みで 31/33・20/20 だった。

AI には「キーの一覧は検索で選んだ一部なので、一覧に無いことを理由に『その設定は無い』と言い切らない」
「相談内容とキーの意味（例: ファイルの容量と件数）が合っているかを説明で確かめる」よう指示している。
`[llm] main_url`/`sub_url` や `api_key`・`password`・`secret`・`token` を含むキーの値は、変更履歴と同じ規則でマスクしてから渡す。

回答に使う LLM は、既定では相談対象インスタンスの `[llm]` 接続先（メイン）。`[admin] assistant_llm_instance` にインスタンス名を
指定すると、常にそのインスタンスの接続先を使う（相談対象の LLM が止まっていても相談できるようにしたい場合や、本番の利用者を
待たせたくない場合に、スキル調整ワーカー等を指定する）。思考（thinking）を有効にしたモデルでは、最初の文字が出るまで数分かかることがある
（待っている間は経過秒数を表示し、「停止」で打ち切れる）。思考部分（`<think>…</think>`）は表示しない。

使わない場合は `[admin] assistant_enabled = false` にすると、ボタンを出さず API も 404 を返す（`[admin]` は config.ini か環境変数で変え、管理ツールを再起動する）。

### スキル研究室

利用者のドラフトや、開発者が新しく作るスキル・**サブエージェント**を、AI に不具合を自己修正させて本番運用に耐えるものに
仕上げ、承認したものをインスタンスへ昇格させる画面（トップの「スキル研究室」タブ。`admin/skill_lab/`・`admin/static/skill_lab.js`）。

画面上の入口と役割は次のとおり（「研究室」と付くのは作業画面、実際に AI を動かすのは スキル調整ワーカー）。

| 場所 | 役割 |
|---|---|
| 上部タブ「スキル研究室」 | 作業画面。左で対象インスタンスを選び、そのインスタンスの研究テーマを作る・ケースを作る・結果を見る・昇格する |
| インスタンスカードの「研究テーマ」ボタン・詳細の「スキル研究室」サブタブ | 上と同じ作業画面を、そのインスタンスを選んだ状態で開く近道 |
| インスタンス一覧の スキル調整ワーカー（種類 `skill_lab`） | 作業画面で頼んだ評価・AI 修正・判定・下書きを裏で処理する実行役。研究テーマは持たない。カードには処理中のテーマ・待ち件数・LLM 接続先が出る |

**スキル調整ワーカー**: 「＋ インスタンス追加」で種類「スキル調整ワーカー」を選んで作る（`instance.json` の `kind: skill_lab`）。
チャット画面・ポートを持たず、「起動」でワーカーのプロセス（`python -m admin.skill_lab.worker --lab <名前>`）が動く。ログは詳細画面の「ログ」タブで見られる。
評価・AI 修正・judge・下書きは、研究テーマの対象インスタンスの設定（スキル・ガード・タイムアウト・`[llm]` の推論パラメータ等）を
そのまま引き継ぐ。スキル調整ワーカーが独自に持つ設定は次の3つだけで、ワーカーの config.ini 画面にもこれだけが出る
（ほかのキーは API からも保存できない。逆に `[skill_lab_worker]` は本体インスタンスの画面には出ない。判定は `admin/instances.py` の `config_key_applies()`）。

- LLM の接続先: `[llm] main_url`・`sub_url`・`main_routing_strategy`・`sub_routing_strategy`（モデルは本番と同じものを想定。
  本番と別の接続先にすれば、研究室の処理が本番の利用者を待たせない）
- ループの上限等: `[skill_lab_worker]`（反復回数・時間の上限・待ち間隔・AI に渡す文字数）
- ワーカー自身のログ: `[log]`

本体インスタンスを複製してワーカーを作った場合も、複製元の設定のうち上の3つ以外は持ち込まない。ワーカーが複数あるときは、テーマごとに担当を選ぶ。

**研究テーマはインスタンスごと**: 組織別・役割別のインスタンスごとに、画面左の一覧でインスタンスを選ぶと、そのインスタンスの
ドラフト・テーマだけが出る（インスタンス詳細の「スキル研究室」サブタブ、カードの「研究テーマ」ボタンからも開ける）。
テーマは対象インスタンスのデータフォルダ（`data/<インスタンス名>/skill_lab/themes/<テーマID>/`）に置かれ、API も
`/api/instances/{name}/skill-lab/...` の下にあるため、他のインスタンスのテーマ・ドラフトには触れない。
1つのテーマは、複数のスキル・サブエージェントと、それらにまたがるケースのまとまり。

| 画面（テーマの詳細） | できること |
|---|---|
| 研究内容 | 上から順に埋めれば開始できる1ページ（【必須】は ①②③、足りない間は開始ボタンを押せず、足りないものを見出しの下に表示）。**① 研究対象**（スキルかサブエージェントを1つ以上）：A. 利用者のドラフトの取り込み／B. 正式スキル・正式サブエージェントの複製（改善）／C. 新規作成（注文文から AI が作る／白紙のひな形）、ファイルの編集・追加・削除。サブエージェントは frontmatter（説明・tools の選択・model）と本文、run_script で呼んでよいスキル（`[subagent] agent_type_run_script_allowlist` へ足す分）をフォームで編集できる。**② やってほしいこと**（`spec.md`、必須は「目的」）：目的・使う場面・期待する出力・やってはいけないことを欄ごとに書く採点の基準。AI judge の判定と AI 修正の基準で、AI は変えない。③ の試験問題は例にすぎないため、② が無いと AI が試験にだけ通る直し方をしても見分けられない。spec.md の見出しは英語圏でも打ちやすい半角の `## Purpose`（目的）・`## When to use`（使う場面）・`## Expected output`（期待する出力）・`## Must not do`（やってはいけないこと）で、AI が資料から読み取れなかった点は `TODO:` で始まる行になる（日本語の旧見出し・「要確認:」で書かれた spec.md も読め、画面で保存すると英語の見出しに揃う）。見出しだけ・`TODO:` だけの Purpose は未記入とみなす。決まった見出し以外を含む spec.md はそのまま編集する。AI による下書き（① の一覧・入手元・注文と中身を読んで書き、人が書いた所は残して空いている所を埋める）。**③ 試験問題**（ケース、1問以上）：② を実際に試す問題。必須は利用者の指示と合格の決め方（回答に入るべき・入ってはいけない言葉、AI が判断する点のどれか）で、問題の名前は空欄なら `case_001` 等を自動で付け、ほかは「細かい設定」に折りたたむ。YAML の直接編集、AI による下書き（① の中身と ② をもとに、期待する出力を合格の条件、やってはいけないことを不合格の条件にする）、試験で使うファイルのアップロード（`cases/fixtures/<フォルダ>/`。ケースの `work_dir: fixtures/<フォルダ>`）、1回試行。**設定パッチ**（`config_patch.json`、折りたたみ）の編集、**AI への依頼**の状況 |
| 実行の記録 | 反復ごとのケース別合格数、AI の分析と直したファイル、各回の最終回答・実行の記録・AI judge の理由 |
| レビュー・昇格 | トライアウトの結果、取り込み・複製した時点からの差分（スクリプトの変更・AI によるケースの変更は強調）、昇格前の確認（配置先・登録する設定・同梱スキルの置き換え）、承認して昇格、差し戻し・不採用 |
| 履歴 | 人・AI の操作の記録 |

**スキル調整ループ**（「スキル調整ループを開始」）: スキル調整ワーカーが、対象インスタンスの構成（会社専用のスキル・ガード設定を含む）に
テーマの全資産（`--skill-overlay`・`--agent-overlay`）と設定パッチ（`--config-patch`）を重ね、LLM の接続先だけを研究室のものにして
（`--llm-from-instance`）全ケースを評価する。judge 付きのケースは AI judge が仕様と judge の観点で判定する。

1. 失敗があれば、AI が失敗の記録（満たさなかった判定・ツール呼び出しと結果・最終回答）と仕様を読んで原因を分析し、
   資産（SKILL.md・scripts/・references/・サブエージェント定義）・ケース・設定パッチを直して、もう一度評価する。
   直せる場所はテーマ内に限られ、frontmatter・tools 名・ケースの形式・Python の構文に1つでも違反があれば修正は全部取り消し、
   その理由を次の反復に渡す。ケースも直せるが、仕様と矛盾している場合だけで、変更はレビュー画面で強調される。
   試験に通るだけの直し方を防ぐため、AI はケースの削除・追加、入力ファイル（`cases/fixtures/`）の変更、研究対象そのもの
   （SKILL.md・エージェント定義）の削除、研究対象に無いスキル・サブエージェントの新規作成はできない。
   評価で使う LLM に接続できない（`llm_unreachable`）ときは、AI 修正に回さず「エラー」で止める。
2. 全ケースに合格したら、対象インスタンスの `tryout_repeats` 回ずつのトライアウトを行い、全回合格で「レビュー待ち」にする。
3. `[skill_lab_worker] max_iterations`・`max_hours` に達したら「上限到達」で止まる（手で直して再開できる）。研究室を停止した場合は、
   次に起動したときに続きから再開する（異常終了で「実行中」のまま残った AI への依頼も、起動時に待ちへ戻す）。
   テーマの「停止」は、担当のワーカーが動いていなければその場で停止になり、ワーカーが止まっている間に押した停止が
   再起動時に打ち消されることはない。

開始すると担当のスキル調整ワーカーが決まる（ワーカーが1つで「自動」の場合もそのワーカーに決める）。担当はテーマ詳細の
「担当・名前を変更」で変えられる（実行中・開始待ちの間は停止してから）。AI への下書きの依頼が残っている間は開始できない
（ループの途中で仕様・ケースが書き換わらないように）。合格の決め方（expect の条件か judge）が無いケース・`work_dir` の
入力フォルダが無いケースがあると、開始の条件（③）を満たさない。1つのドラフトを取り込めるのは、終わっていないテーマ1つだけ。
テーマはテーマ一覧・テーマ詳細の「削除」でフォルダごと消せる（元に戻せない。`admin/skill_lab/api.py` の `delete_theme`）。
ループ・トライアウトの実行中・開始待ちと、AI への依頼（下書き・1回試行）の処理中は消せない（先に停止する）。終わっていない
テーマを消すと、取り込み元のドラフトは利用者のもとに残り、研究室の印だけが外れる（また別のテーマに取り込める）。昇格済みの
テーマを消しても、昇格したスキル・サブエージェントと `instances/<名前>/evals/<テーマID>/` のケースは残る。theme.json が
壊れて開けないテーマも消せる。変更履歴には `skill_lab_theme_delete` で残る。
研究対象そのもの（スキルのフォルダ・SKILL.md・エージェント定義）はファイルの削除では消せず、「外す」で除く（取り込んだドラフトの印も外れる）。

研究対象・ケース・設定パッチ・入力ファイルを1か所でも変えると、それまでのトライアウトは無効になる（0回から）。

**昇格**（「承認して昇格する」）: 事前確認（トライアウトが今の内容を規定回数で評価して全回合格したものか、複製元の正式資産が
変わっていないか、取り込み元のドラフトが作成者によって変わっていないか、配置先の同名資産、設定の登録が環境変数に邪魔されないか）を
通ったテーマ全体を、対象インスタンス専用の置き場（`instances/<名前>/skills/`・`agents/`）へ置く。

- スキルのスクリプトの免除設定と設定パッチを、そのインスタンスの `config_overrides.json` へ登録する（評価で足したのと同じ値）。
- ケース一式（仕様・設定パッチ・入力ファイルを含む）は `instances/<名前>/evals/<テーマID>/` に残る
  （回帰テストは `python evals/run_all.py --cases-dir <そこ> --instance <名前>`）。
- 取り込み元のドラフトはアーカイブへ移り、作成者の一覧から消える。
- 置き換えたものは `evals/history/promote/` に退避し、記録は `evals/promotion_log.md` と変更履歴に残る。
- 任意で「他のインスタンスにも配布する」を選ぶと、選んだインスタンスごとに、それぞれの専用置き場へ複製して設定も登録する。
- 設定の保存に失敗した場合は、置いたファイルを元に戻す。昇格したものが使えるのは、配置先のインスタンスの再起動後。

インスタンスを削除すると `instances/<名前>/` 配下の `skills/`・`agents/` も消えるため、削除ダイアログに専用のスキル・サブエージェントの一覧を出して警告する。
インスタンスを複製して作るときは、専用の置き場も複製するかを選べる。

### 変更履歴（監査ログ）

`instances/admin_changes.log`（JSON Lines）に、設定変更・インスタンスの
作成/削除/起動/停止/再起動・ユーザー操作等を1操作1行で記録する。日時・
実行者・接続元IP・操作種別・（設定変更なら）変更前後の値を記録するが、
キー名に `api_key`/`password`/`secret`/`token` を含む値、および
`[llm].main_url`/`sub_url`（`api_key` をネストしたJSONとして含みうる）は
マスクして記録する。管理ツールの「変更履歴」タブから閲覧できる。

### APIリファレンス（curl での直接操作）

画面の操作はすべて HTTP API（`/api/...`）でも行える。curl での呼び出し方は
[admin/API_REFERENCE.md](admin/API_REFERENCE.md) にまとめてあり、同じ内容を
管理ツールの「APIリファレンス」タブでも表示する（表示のたびに md を読み直すため、
md を編集すれば管理ツールを再起動せずに反映される）。表示言語が英語のときは英訳の
[admin/API_REFERENCE.en.md](admin/API_REFERENCE.en.md) を表示する。API を追加・変更した場合は
この md（日本語版が正本）と英訳の両方を更新すること。

### セキュリティ上の注意

既定では管理ツールはHTTPで待ち受ける。`[admin].host` を `0.0.0.0` にしてLAN上に
公開する場合は、`[admin].ssl_certfile`/`ssl_keyfile` を設定してHTTPS化すること
（「5. HTTPS で待ち受ける（任意）」参照）。HTTPのままだと、ログインパスワードや
設定値が平文でネットワークを流れる（社内LAN等、経路上の盗聴リスクを許容できる
場合を除きHTTPのまま公開しないこと）。
また `[scripts].python` や `[llm]` の接続先を変更できることは、実質的に
任意のコード実行・任意のLLMサーバーへの接続を許可することと同義であり、
管理ツールへログインできる人（`ADMIN_USERS`）の範囲には注意すること。

---

## バージョン管理とリリース手順

バージョン番号は [セマンティックバージョニング](https://semver.org/lang/ja/)（`MAJOR.MINOR.PATCH`）で付け、
正本はプロジェクト直下の `VERSION` ファイル（1 行）の 1 か所だけに置く。
`src/version.py` の `__version__` がこれを読み、次の場所に表示される（手で書き換える箇所は他に無い）。

| 表示先 | 内容 |
| --- | --- |
| チャット画面 | ヘッダーのタイトルにマウスを乗せたときのツールチップ（`GET /locohane/version`） |
| 設定ダッシュボード | 上部バーのタイトル横（`GET /api/me`・`POST /api/login` の `version`） |
| MCP サーバー | `initialize` 応答の `serverInfo.version` |
| ログ | 本体起動時の `app_*.log` に `Locohane vX.Y.Z started.` |

### 番号の上げ方

- **1.0.0 になるまで（0.x）**: `config.ini` のキーの移動・改名、データ配置の変更、ツール・スキル・
  サブエージェントの廃止など、既存の設定やデータのままでは動かなくなる変更を含む場合は **MINOR** を上げる。
  機能追加も MINOR。不具合修正と互換性を保った小さな改善だけなら **PATCH** を上げる。
- **1.0.0 以降**: 互換性のない変更で MAJOR、互換性のある機能追加で MINOR、不具合修正で PATCH を上げる。
- `system_prompt/`・`agents/`・`skills/` のプロンプトのチューニングだけの変更は PATCH とする。
- **プレリリース**（正式版の前に試してもらう版）は、次の番号に `-rc.N` を付ける（例: `0.5.1-rc.1`）。`VERSION` とタグ
  （`v0.5.1-rc.1`）にこの番号を使い、GitHub Release は Pre-release として作る。本文は `CHANGELOG.md` の
  `## [Unreleased]` の内容を使い、`CHANGELOG.md` は正式版を出すまで `## [Unreleased]` のままにする。

### 変更履歴（CHANGELOG.md）

日々のコミットでは、利用者・運用者に関係する変更を `CHANGELOG.md` の `## [Unreleased]` に
「追加」「変更」「廃止」「修正」「移行時の注意」の見出しで追記しておく。
設定キーの移動・改名やデータ配置の変更は、旧キー → 新キーが分かるように必ず「移行時の注意」に書く。

### リリース手順

`main` ブランチで、作業ツリーがクリーンかつテストが通る状態から行う。

1. `CHANGELOG.md` の `## [Unreleased]` の内容を `## [X.Y.Z] - YYYY-MM-DD` へ移し、空の `## [Unreleased]` を残す。
   末尾の比較リンク（`[Unreleased]`・`[X.Y.Z]`）も追加・更新する。
2. `VERSION` を `X.Y.Z` に書き換える。
3. フロントエンドを変更していれば `frontend/` で `npm run build` し、`public/build/` を更新する。
4. `pytest` を実行して全件成功を確認する。
5. 「Release vX.Y.Z」の 1 コミットにまとめ、注釈付きタグを付けて push する。

   ```bash
   git commit -am "Release vX.Y.Z"
   git tag -a vX.Y.Z -m "Locohane vX.Y.Z"
   git push origin main vX.Y.Z
   ```

6. GitHub Release を作成する（本文は `CHANGELOG.md` の該当版の節）。

   ```bash
   gh release create vX.Y.Z --title "Locohane vX.Y.Z" --notes-file <該当版の節を書き出したファイル>
   ```

Claude Code からは `/release` スキル（`.claude/skills/release/`）でこの手順を対話的に実行できる。

---

## ライセンスと商用利用

### 本プロジェクトのライセンス

本ソフトウェアは **MIT License** です。詳細は [LICENSE](LICENSE) を参照。

### 依存パッケージ（商用利用可）

`requirements.txt`（pip）の直接依存から到達する実行時の推移的依存、全 162 パッケージの
ライセンス内訳:

| ライセンス種別 | 数 | 商用利用 |
|---|---:|---|
| Apache 2.0 | 74 | ✅ |
| MIT | 57 | ✅ |
| BSD | 26 | ✅ |
| PSF | 3 | ✅ |
| MPL 2.0 | 2 | ✅（改変せず利用する限り） |

**GPL / AGPL / LGPL は含まれません。** すべて寛容ライセンス、またはファイル単位の
弱いコピーレフト（MPL 2.0）であり、改変せず依存として利用する限り本ソフトウェアへの
組み込み・商用配布が可能です。全パッケージの完全な一覧と帰属表示は
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) に記載しています（依存追加時は
`tools/gen_licenses.py` で再生成）。

上記は pip 依存のみが対象です。`frontend/` の npm 依存（react、recoil、
`@chainlit/react-client`、vite 等、いずれも MIT / Apache 2.0）は別途手動確認済みで
GPL / AGPL / LGPL 系は含まれませんが、`tools/gen_licenses.py` の自動集計・
THIRD_PARTY_LICENSES.md には含まれていません。npm 依存を追加・更新した場合は
`frontend/package-lock.json` のライセンスも別途確認してください。

なお `frontend/public/favicon.svg`（`public/build/favicon.svg` に配布）は
[Chainlit](https://github.com/Chainlit/chainlit)（Apache 2.0）が同梱するデフォルトの
favicon をそのまま使用しています。

### 外部通信について（完全オフライン保証）

本スタックの外部送信はすべて **opt-in**（API キー設定時のみ）で、既定では発生しません。
`app.py` 冒頭で以下を明示的に無効化しています:

- Chainlit の Literal AI データ層（`LITERAL_API_KEY` 未設定なら送信なし）
- LangChain の LangSmith トレーシング（`LANGCHAIN_TRACING_V2` / `LANGSMITH_TRACING` を false 化）

唯一の外部通信は `config.ini` の `base_url`（既定 `http://localhost:8080` = ローカルの
llama-server。vLLMを使う場合も同様にローカルのエンドポイントを指す）への LLM 呼び出しのみです。

MCPサーバー機能（`.locohane/settings.json`、上記「MCPサーバー接続」参照）を有効化した
場合、そこに定義したコマンド（例: `npx` 経由のサードパーティ製MCPサーバー）が
ローカルサブプロセスとして起動されます。既定状態（`.locohane/settings.json` が
空の `mcpServers` のみ、または `[mcp] enabled = false`）では何も起動されず、
オフライン保証は変わりません。ただし、ユーザー自身が追加したMCPサーバーが内部で
ネットワーク通信を行うかどうかは各MCPサーバーの実装次第であり、本プロジェクトの
管理範囲外です。

ベクターDB（`[vector_db]`、既定は無効）を有効にした場合、埋め込みは `embedding_url` の推論サーバー、
`backend = qdrant` 時のベクターは `qdrant_url` の Qdrant サーバーへ送る（取り込んだ文書のチャンク本文を含む）。
どちらもローカルまたは LAN 内のサーバーを指定する前提で、既定の `backend = sqlite_vec` ではローカルファイルにだけ保存する。

管理ツールの「AIに相談」（管理アシスタント）は、インスタンスの `[llm]` 接続先（上と同じ LLM サーバー）へ、相談内容と
関係する設定キーの説明・値（機密値はマスク済み）・ドキュメントの段落を送り、検索のために相談内容とドキュメントの段落を
`[vector_db] embedding_url` の埋め込みサーバーへ送るだけで、それ以外の外部通信は行わない。

`web-search` スキル（Tavily APIによるWeb検索）は、スキル専用の
`skills/web-search/scripts/.env` に `TAVILY_API_KEY` を設定した場合のみ、
ユーザーがこのスキルを実行した時に限り `https://api.tavily.com` へ通信します。
未設定（`.env` 自体が無い、または空）の場合は一切通信せず、設定手順を示す
エラーメッセージを返すだけです。

### サードパーティ製Agent Skillsのライセンス

`skills/`・`.locohane/skills/` 配下にGitHub等で公開されているサードパーティ製の
Agent Skills（SKILL.md一式）を追加導入した場合、そのスキル自体のライセンス・
著作権表示は本プロジェクトのMIT Licenseとは別に、そのスキルの配布元が定める
条件に従います。ユーザーが任意に追加する外部コンテンツであり、本プロジェクトの
同梱物ではないため、`THIRD_PARTY_LICENSES.md`の自動集計・本プロジェクトの
ライセンス保証（上記「依存パッケージ」節）の対象外です。導入前に配布元の
LICENSEファイル・SKILL.mdのfrontmatter（`license`キー）を確認し、商用利用の
可否・帰属表示義務の有無を個別にチェックしてください。スキルが追加で外部
パッケージに依存する場合、そのパッケージのライセンスも別途確認が必要です
（`requirements.txt`への追加は本リポジトリ直下 `CLAUDE.md`「修正・実装時
ルール」に従うこと）。

### 商用利用時のチェックリスト

- [ ] 使用するモデル（GGUF/vLLM対応形式）の**ライセンス**を確認する（→ 上記「推論サーバー（llama.cpp / vLLM）の起動例」の注記）
- [ ] 依存を追加・更新したら `tools/gen_licenses.py` で告知ファイルを再生成する
- [ ] サードパーティ製Agent Skillsを追加した場合、そのスキル自体と追加依存
      パッケージのライセンスを個別に確認する（上記「サードパーティ製Agent
      Skillsのライセンス」参照）

---

## 免責事項

本ソフトウェアは「現状のまま」（as-is）で提供され、明示的または黙示的な保証はありません。
著作者または著作権者は、本ソフトウェアの使用またはそれ以外の行為について、本ソフトウェアに
起因するかぎり一切の責任を負いません。いかなる状況下においても、本ソフトウェアの使用により
直接・間接的に生じたいかなる損害（データ消失、システム障害、ビジネス損失、機会損失を含む）
について、著作者または著作権者は責任を負いません。

上記は本ソフトウェアに適用される [LICENSE](LICENSE)（MIT License）の無保証・免責条項の
要約です。詳細な法的文言は LICENSE 本文が優先します。

## README_DETAIL.md更新時の注意 

見出し（`##`/`###`）を追加・変更・削除した場合は、`CLAUDE.md`・`QWEN.md` の「ClaudeCode実行時ルール」に記載している README_DETAIL.md 見出し一覧も必ず合わせて更新すること。
利用者の操作手順や画面の見え方が変わる変更は、利用者向けの [README.md](README.md) にも反映すること（README.md には設定キーやディレクトリ構造の詳細は書かず、本書へのリンクで済ませる）。
本書を更新したら、英語版の [README_DETAIL.en.md](README_DETAIL.en.md) も合わせて更新すること（日本語版が正本）。README.md を更新したら [README.en.md](README.en.md) も同様に更新する。

