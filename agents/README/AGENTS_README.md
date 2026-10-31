# サブエージェント実装仕様（ClaudeCode の `.claude/agents/*.md` 相当）

このディレクトリ（`agents/`）配下に置く各サブエージェント種別の実装仕様をまとめる。
`skills/SKILLS_README.md`（Agent Skills 仕様準拠）の姉妹文書。
準拠元: ClaudeCode の `.claude/agents/*.md`（Anthropic公式のサブエージェント仕様）。

実装の中核は3か所（`tools.py` という単一ファイルは廃止され、`src/tools/` パッケージへツール1つ原則1ファイルで分割済み）:
- `src/agent_types.py` … `agents/` 配下の走査・frontmatter検証・`AgentType` 定義
- `src/tools/` パッケージ … ツール名の解決（`_resolve_agent_types()`、`src/tools/_state.py`）・委譲ツール本体（`dispatch_agent`、`src/tools/dispatch_agent.py`）・計画承認ガード（`src/tools/create_plan.py`/`src/tools/approve_plan.py`）
- `src/subagent.py` … 委譲されたサブエージェント自身の独立した ReAct ループ

## 1. ディレクトリ構成

```
agents/
  <agent-name>.md   # 1ファイル = 1エージェント種別。frontmatter + システムプロンプト本文
```

`skills/<skill-name>/` のようなフォルダ単位ではなく、**ファイル単位**（`scripts/`や`references/`のようなサブフォルダは存在しない）。

- `<agent-name>` はファイル名（拡張子除く stem）。frontmatterの `name` と**完全一致必須**（不一致は起動時スキャンでスキップされ、警告ログのみで起動は継続する）。
- `.locohane/agents/` にも同名の仕組みでファイルを置けて、`agents_dir`（`config.py` 879行）とマージされる。同名の `name` があれば `.locohane` 側が後勝ちで上書きする（`agent_types.py` 184-193行）。

## 2. \*.md の形式

```markdown
---
name: worker              # 必須。1〜64文字、小文字英数字・ハイフン・アンダースコアのみ、
                            # 先頭末尾は区切り文字不可、区切り文字の連続不可、ファイル名(stem)と一致
description: ...            # 必須。1〜1024文字。「何をする専用エージェントか」を書く
tools: read_skill, Read, Glob, run_script   # 任意。カンマ区切り文字列 or YAMLリストのどちらも可
model: QWEN3.6_35B-A3B      # 任意。[llm].sub_url の接続先の model 名（後述）
---

# 本文（システムプロンプトとして丸ごとサブエージェントに渡る Markdown 自由記述）
```

- `name` / `description` の検証ルールは `src/agent_types.py` の `_validate()`（100-126行）が唯一の正。`_NAME_RE`（25行）は `skills.py` の `Skill.name` と同じ正規表現を踏襲。
- 検証に落ちたファイルは黙ってスキップされる（例外で全体を落とさない設計）。ログ（`app.log`）で `仕様違反のためスキップ` を確認できる。成功時は `エージェント種別発見: <name>`（155行）。
- `description` は **メインエージェントが `dispatch_agent` 呼び出し時に `agent_type` を選ぶ唯一の手がかり**（利用可能なエージェント種別一覧としてシステムプロンプトに列挙される）。「何を委譲できる専用エージェントか」を具体的に書くこと（既存4種別を参照）。
- `tools` は省略可能。**省略した場合は `_SUBAGENT_TOOLS`（後述）を丸ごと継承する**（`_resolve_agent_types()`、`src/tools/_state.py` 623-624行。Anthropic仕様の「tools省略時は全ツール継承」を踏襲したと同関数の同ファイル608-609行のdocstringに明記）。書式はカンマ区切り文字列（Anthropic公式仕様の主形式、`_parse_tools_field()` 81行コメント）・YAMLリストのどちらでも受け付ける。
- `model` は省略可能（Anthropic公式仕様の `model` 相当）。`config.ini` の `[llm].sub_url` に並べた接続先の `model` 名を書くと、その種別のサブエージェントは**その `model` の接続先だけを候補に `sub_routing_strategy` で接続先を選ぶ**（比較は前後空白・大文字小文字を無視）。
  - 一致する接続先が無い、または一致する接続先が全て `start`/`end` の時間帯外の場合は、`[llm].sub_default_model`（下記）、それも使えなければ指定を無視して通常のルーティングに従う（起動は止めず、`app.log` に WARNING を出す）。
  - `sub_url` 未指定（メインの接続先を継承する設定）の場合も、継承先が指定モデルでなければ `main_url` の中の指定モデルの接続先から選ぶ。
  - 省略・`model: inherit`（ClaudeCode の定義ファイル由来）・空文字は指定なしとして扱い、`config.ini` の `[llm].sub_default_model` があればそのモデルを同じ扱いで使う（空欄なら従来どおりモデル名を見ずに選ぶ）。`sub_url` 未指定（メインの接続先を引き継ぐ設定）でも `sub_default_model` は有効で、メインが別モデルの接続先を使っていると引き継がず、`main_url` のうち `sub_default_model` のモデルの接続先から選ぶ（メインと同じ接続先を使わせたいなら空欄にする）。`sonnet`/`opus` 等のエイリアスは解釈しない（一致する接続先が無いものとして無視される）。
  - 実装: `_parse_model_field()`（`src/agent_types.py`）→ `ResolvedAgentType.model`（`src/tools/_state.py`）→ `_run_dispatch_agent_job` が `set_preferred_sub_model()` でジョブのタスク内だけに設定（`src/tools/_dispatch_agent_job.py`）→ `build_model(role="sub")` が `_select_endpoint(preferred_model=...)` へ渡す（`src/llm/routing.py`）。contextvar なのでジョブ内の再構築・圧縮用の `build_model()` にも同じ指定が効き、メインエージェントや他のジョブへは漏れない。
  - `dispatch_agent`/`dispatch_agent_batch` の `model` 引数（5節）で1回の委譲ごとに上書きできる（引数が優先）。

## 3. `tools:` フィールドとツール名の解決

サブエージェントに渡せるツールの実体は、`src/tools/registry.py` の `_SUBAGENT_TOOLS` リスト（41-70行、メモリー系ツール定義より後に置く必要があるため `_BASE_TOOLS`（76行〜）直前に配置）に列挙された固定セットのみ（旧版ドキュメントの一覧は `execute_python_code_readonly`・`write_thread_note`/`list_thread_notes`/`read_thread_note` の追加漏れがあったため、下記は現物と突き合わせて更新済み）:

```
read_skill, read_skill_file, provide_download,
run_script, run_script_background, check_script_job, stop_script_job,
execute_python_code, execute_python_code_background, execute_python_code_readonly,
get_tool_source, check_work_dir_status, analyze_image,
read_tool(=Read), glob_tool(=Glob), grep_tool(=Grep),
json_query, search_path_memory, write_scratch_note,
write_thread_note, list_thread_notes, read_thread_note,
create_memory, update_memory, delete_memory,
read_memory, search_memory, list_memories
```

- メモリー系6ツールは `_SUBAGENT_TOOLS` に含まれてはいるが、実際に各サブエージェントへ渡るかは `agents/*.md` 側の `tools:` 次第。`explore` は読み込み系（`read_memory`/`search_memory`/`list_memories`）のみ、`worker` は全6ツール（フルアクセス）、`verifier` は含めていない。
- **`write_scratch_note` は全サブエージェント種別に共通で付与する必須ツール**（既存3種別＝`explore`/`worker`/`verifier` すべてに付与済み）。`system_prompt/subagent_common.md`（4節参照）がトークン上限による打ち切り対策として `write_scratch_note` の使い方を**全エージェント共通の注意事項として無条件に**説明する構成になっているため、`tools:` に含めないエージェント種別を作ると、本文には登場するのに実際には呼べないツールについてのガイダンスだけが渡ることになる。新規に種別を追加する場合も `tools:` に必ず含めること（8節参照）。

- `Read`/`Glob`/`Grep` のように大文字始まりでfrontmatterに書く名前は、Python側の関数名（`read_tool`等）とは別に `@tool("Read")` のようにデコレータ引数で明示された `.name` 属性。frontmatterには **`.name` の方**（`Read`/`Glob`/`Grep`）を書く。
- `_resolve_agent_types()`（`src/tools/_state.py` 603-642行）が `tool_lookup = {t.name: t for t in registry._SUBAGENT_TOOLS}`（620行）を作り、frontmatterの `tools:` に書かれた名前と突き合わせて解決する。**未知のツール名は例外を出さず警告してスキップ**（630-634行）— 誤字に気づきにくいので、追加・変更時はアプリ起動ログを必ず確認すること。
- 上記リストに **`dispatch_agent` 自体は含まれていない**。これが「サブエージェントはさらに別のサブエージェントへ委譲できない」という制約の実体（各.md本文にある「委譲する手段を持たない」という注記は、この一点のみで担保される説明であり、それ以外の特別な強制ロジックは無い）。

## 4. `{{skills}}`/`{{all_skills}}`/`{{agent_types}}` プレースホルダーと共通注意事項の自動連結

- `app.py` 525-527行。`scan_agent_types()` の後、`render_skills_block(skills)`（`src/skills.py`、`name: description` 形式のスキル一覧）を各エージェントの `system_prompt` 内の `{{skills}}` へ `str.replace` で差し込む（`dataclasses.replace` でイミュータブルに更新）。**スキルの本文そのものは含まれず、一覧のみ**（skills側の progressive disclosure 第1段階と同じ扱い）。
- `{{skills}}` に載るスキルは `filter_skills_for_agent_type()`（`src/skills.py`）で種別ごとに絞り込む。`config.ini` の `[subagent].agent_type_run_script_allowlist` にその種別の登録が1件でもあれば、登録したスキル（`["スキル名","スクリプト名"]` 指定はそのスキル）だけを載せる（`scripts/` を持たない知識系スキルも、未登録なら載せない）。登録が無い種別（`worker` 等）は全スキルを載せる。絞り込みを掛けたくない種別は、本文に `{{skills}}` の代わりに `{{all_skills}}` と書くと、登録の有無に関係なく全スキル一覧が載る（`run_script` の実行制限自体は変わらない）。`{{run_script_allowlist}}` には同じ登録内容が種別ごとに差し込まれる。
- 同じ箇所で `render_agent_types_block(agent_type_defs)`（`src/agent_types.py`、`name: description` に続けて frontmatter の `tools`（省略時は既定ツール一式を継承する旨）を「使用可能ツール:」行として添えたエージェント種別一覧。メインの `system_prompt.md` に差し込む `{{agent_types}}` と同じブロックを使い回す）を各エージェントの `system_prompt` 内の `{{agent_types}}` へも差し込む。ただし現状、本文で `{{agent_types}}` を使うエージェント種別は無い（旧 `planner` 種別のみが使っていたが2026-09-29に廃止）。サブエージェントは `dispatch_agent` を持たず孫委譲できないため `{{agent_types}}` を本文に書く必要はない。
- `app.py` 550行。`system_prompt/subagent_common.md`（作業量・トークン上限に達した際の振る舞いに加え、`write_scratch_note` での途中経過の書き残し方・最終回答を生データの羅列にせず簡潔にまとめる指示を含む共通文）を**全エージェントの system_prompt 末尾に自動連結**する。個々の `agents/*.md` 側で同様の注意書きを重複して書く必要はない。

## 5. メインエージェントからの呼び出し方法

メインエージェントは `dispatch_agent(task: str, agent_type: str, tool_call_id: ..., orchestrator_skill: str | None = None, model: str | None = None)`（`src/tools/dispatch_agent.py`）でサブエージェントに委譲する。

- `agent_type` は `agents/*.md` の `name` と一致させる必須引数（既定値なし）。
- `model` は任意。ユーザーがモデルを指定した場合だけ使う想定で、frontmatter の `model`（2節）より優先される。`dispatch_agent_batch` も同じ `model` 引数を持ち、全グループに同じ指定が適用される。
- 内部で `_AGENT_TYPES.get(agent_type)` を引き、`asyncio.create_task` で起動したバックグラウンドジョブ（`_run_dispatch_agent_job`、`src/tools/_dispatch_agent_job.py` 211-313行）の中で `run_subagent(task, resolved.tools, resolved.system_prompt, _LLM_CONFIG, job.max_iterations, on_iteration=..., llm_timeout_max_retries=..., on_cancelled=...)`（`src/subagent.py` 493行〜）を呼ぶ。`dispatch_agent` 自身はこのジョブの完了を（安全上限まで）待ち続け、完了までの間チャットへ進捗を直接pushする。サブエージェントは委譲元と**独立した ReAct ループ**（別の会話履歴）で動き、思考過程・途中のツール呼び出しは委譲元と共有されない。
- 委譲元に返るのは、サブエージェントが最後に返す「tool_calls を伴わないメッセージ」の content のみ。各 `agents/*.md` 本文が「最終回答を必ず書け、無言で終わるな」と強調しているのはこのため（空文字で終えると委譲元には何も伝わらない）。
- ジョブ実行中は `_IN_SUBAGENT` コンテキスト変数が `True` になる（`_run_dispatch_agent_job` 内、`src/tools/_dispatch_agent_job.py` 238行。変数自体の定義は `src/tools/_state.py` 38行）。

## 6. 画像をチャットメッセージ・Markdownテーブルへ埋め込む場合の注意

サブエージェントの最終回答（tool_calls を伴わない最後のメッセージの content）は、
5節の通り `dispatch_agent` の**ツール結果**としてメインエージェントの会話履歴へ
`ToolMessage` として積まれるだけであり、**ユーザーのブラウザへ直接表示される
わけではない**。ユーザー画面に実際に描画されるのは、あくまでメインエージェント
自身が最終的に生成する回答テキスト（`app.py` の `_send_answer()` が送信直前に
処理する内容）だけである。

`![説明](絶対パス)` によるMarkdown画像埋め込み（`app.py` の
`_embed_local_images_as_session_urls()` がブラウザから取得可能なURLへ
変換する仕組み。`skills/SKILLS_README.md` 4-5節参照）も例外ではない。
サブエージェントが最終
回答に `![説明](絶対パス)` を書いても、それだけでは画像は表示されない —
**メインエージェントがそのMarkdown記法を改変せず自分の最終回答へそのまま
転記して初めて**、送信直前の変換処理にかかり画像として表示される
（`system_prompt.md` の「実務上の注意」節に、メインエージェント側へこの転記を
促す指示がある）。

画像プレビュー付きの表をユーザーへ見せる作業を `worker` 等へ委譲する場合、
`agents/<name>.md` 側の指示文には次の2点を明記すること:

1. 最終回答に**貼り付けてそのまま使える完成形のMarkdown**（表全体、または
   `![説明](絶対パス)` の行そのもの）を書かせる。パスの説明文だけを返させて
   メインエージェント側で組み立てさせようとすると、パスの書き写しミスや
   `@N` の誤用（下記）が起きやすい。
2. パスは**`@N`（パスメモリー参照）ではなく実際の絶対パス文字列**を使わせる。
   `@N` はツール引数専用の解決対象であり、回答本文（Markdown）の中に書いても
   解決されない（`skills/SKILLS_README.md` 4-5節参照）。`dispatch_agent` の
   結果に含まれるパスはもともと `@N` ではなく解決済みの絶対パスなので、
   そのまま使わせればよい。

**テンプレート例**（画像を含む一覧表をユーザーへ返す委譲先の指示文の例。
`worker.md` 等に記載する想定）:

```markdown
## 最終回答の書き方

生成した画像を一覧表の一部として見せる場合は、次の形式でそのまま最終回答へ
書く（呼び出し元のメインエージェントがこの行をそのまま自分の回答へ転記して
表示する）。個別に1枚ずつ見せるだけでよい場合は、この形式ではなく
`analyze_image` を `show_in_chat=True` で呼ぶ方が簡潔（system_prompt/subagent_common.md
「画像をユーザーへ見せる方法」参照）:

    | No. | ファイル名 | プレビュー |
    |---|---|---|
    | 1 | out1.png | ![out1.png](C:\work\Locohane_Agent\data\temp\out1.png) |
    | 2 | out2.png | ![out2.png](C:\work\Locohane_Agent\data\temp\out2.png) |

パスは必ず絶対パスをそのまま書く（`@N` は使わない）。
```

## 7. 計画承認（`create_plan`/`approve_plan`）による書き込みブロック

`worker.md` が使う `run_script`/`execute_python_code` は、計画未承認だとブロックされる。

- `_prepare_script_execution()`（`src/tools/_script_job.py` 78行〜）内、同ファイル104-105行で `cl.user_session.get("plan_approved")` を判定する（`(skill_name, script_filename)` が `_PLAN_APPROVAL_EXEMPT_SCRIPTS` に含まれる場合のみ免除）。`execute_python_code`（`src/tools/execute_python_code.py` 107行）・`execute_python_code_background`（`src/tools/execute_python_code_background.py` 108行）にもそれぞれ同様の判定がある。
- `cl.user_session` は Chainlit のセッションスコープ。メインエージェントの `create_plan`（`src/tools/create_plan.py` 101行）/`approve_plan`（`src/tools/approve_plan.py` 41行・67行、いずれも `plan_approved` を `cl.user_session.set`）が更新した値を、サブエージェント内から呼ばれるツールもそのまま参照する。**サブエージェント専用の特別なロジックは無く、セッション状態の共有のみで実現されている。**
- したがって `explore`/`verifier` のように `execute_python_code`/`run_script` を持たない（または読み取り専用スクリプトしか呼ばない前提の）エージェントはこの制約と無関係だが、`worker` のように書き込み系ツールを持つエージェントは、委譲元で計画承認が済んでいないと途中でブロックされる。ブロックされた場合、サブエージェント自身は `approve_plan` を呼ぶ手段を持たないため、`worker.md` はリトライせず「計画未承認のため書き込みができなかった」旨を最終回答に明記するよう指示している。

## 8. 新しいサブエージェント種別を追加する手順

1. `agents/<agent-name>.md` を作成（frontmatter必須、`name` はファイル名(stem)と一致）。

   **名前は既存種別と接頭辞を共有させない**: 新しいagent_type名は、既存の
   名前（`explore`/`verifier`/`worker`）のいずれとも
   文字列としての接頭辞関係を持たないようにする。理由: (a)
   `_guard_main_agent_tool_limit`（`src/tools/tool_node.py` 169行、メインエージェント
   自身が書き込み系ツールを直接呼んでブロックされた際のエラーメッセージ）は
   agent_type一覧を`sorted()`で単純アルファベット順に列挙するだけで、文脈に
   応じた案内をしない。接頭辞を共有すると短い方の名前が必ず直前・直後に
   並び、誤選択を誘発しやすい。(b) 名前自体が「既存の単純な語＋修飾語」と
   いう複合語だと、単純な方の語がより強く選ばれやすい。実際に`explore-web`
   という名前が`explore`と接頭辞を共有していたためにこの2つの要因が重なり、
   重大な誤選択を引き起こした事例がある（2026-08-15、`explore-websearch`へ改名して対処。
   その後`explore-websearch`自体は廃止済み）。
2. `tools:` に必要なツール名を `_SUBAGENT_TOOLS`（3節参照）の中から選んでカンマ区切りで列挙する（省略時は全ツール継承）。**`write_scratch_note`（run_id限定の自分用スクラッチ）と `write_thread_note`/`list_thread_notes`/`read_thread_note`（thread_id共有・他agent_typeやメインエージェントとも共有されるノート）は全種別共通の必須ツールなので、`tools:` を明示的に列挙する場合は必ず含めること**（省略して全ツール継承する場合は自動的に含まれるため対応不要）。thread note系3ツールは2026-08-20のthread note機能追加時に`agents/*.md`側への追加が漏れており、2026-08-21に全種別へ追加済み。
3. 本文に、委譲元から見た役割・使ってよい/いけないツールの区別・手順・最終回答で書くべき内容（および書いてはいけない内容）を明記する。既存3種別（`explore`＝読み取り専用の汎用調査・オフィス文書/PDF調査、`verifier`＝成果物の検証専用、`worker`＝計画承認後の書き込み実作業）を参考にする。
4. **アプリを再起動する**（`app.py` の `_setup()` は起動後1回しか `scan_agent_types()` を呼ばない冪等関数のため、ホットリロードは無い。新規チャットセッションを開いただけでは再スキャンされない）。起動ログの `エージェント種別発見: <name>` を確認する。
5. 実際にチャットから、メインエージェントが `dispatch_agent(agent_type="<agent-name>", ...)` を正しく呼び出し、サブエージェントが意図した最終回答を返すことを確認する。

## 9. Anthropic（ClaudeCode）仕様との関係

`agent_types.py` 冒頭コメント（3行）に「ClaudeCode の `.claude/agents/*.md` 相当」と明記されている通り、frontmatter形式（`name`/`description`/`tools`/`model`）・`tools` 省略時の全ツール継承・カンマ区切りを主形式とする書式は、Anthropic公式のサブエージェント仕様の挙動を踏襲している。

ただし以下は本プロジェクト独自の実装であり、ClaudeCode本体のSubagentランタイムをそのまま使っているわけではない点に注意（`SKILLS_README.md` 6節と同様の位置づけ）:

- LLM本体は **llama.cpp server（OpenAI互換API）** に接続しており、Claude/Anthropic APIは使用していない（`src/graph.py` の `build_model()` 参照）。
- `model` も同じ名前のフィールドだが、値は `sonnet`/`opus`/`haiku` 等のエイリアスではなく `[llm].sub_url` の接続先の `model` 名を書く（`inherit` のみ「指定なし」として受け付ける。2節参照）。
- `dispatch_agent` はこのプロジェクトが `src/tools/dispatch_agent.py` に独自実装した委譲ツールであり、ClaudeCode本体のTaskツール実装そのものではない。
- サブエージェントは委譲元と別の独立した ReAct ループ（`src/subagent.py`）で動く自前実装であり、さらに別のサブエージェントへ再委譲する経路は `_SUBAGENT_TOOLS` に `dispatch_agent` を含めないことで意図的に塞いでいる（ClaudeCode本体でのネスト委譲可否とは無関係に、本プロジェクトの設計判断）。
- `SKILLS_README.md` が言及する「公式仕様URLへの準拠宣言」に相当する記述は `agents/` 側には無く、「`.claude/agents/*.md` 相当」というコード内コメントのみが根拠。

つまり「**frontmatter形式・tools解決ルールの設計思想はAnthropic（ClaudeCode）仕様に倣っているが、実行系（LLM・委譲の仕組み）はAnthropicのものではなく完全に自前**」というのが正確な位置づけ。
