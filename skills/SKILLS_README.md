# スキル実装仕様（Agent Skills 準拠）

このディレクトリ（`skills/`）配下に置く各スキルの実装仕様をまとめる。
準拠元: [Agent Skills specification](https://agentskills.io/specification)

実装の中核は3ファイル:
- `src/skills.py` … 第1段階 Discovery（スキル走査・システムプロンプト注入）
- `src/tools/` パッケージ  … 第2・3段階 Read/Execute（`read_skill` / `read_skill_file` / `run_script` / `analyze_image`）
- `src/graph.py`  … 上記ツールを LLM に `bind_tools` してReActループを回す

## 1. ディレクトリ構成

```
skills/
  <skill-name>/
    SKILL.md            # 必須。frontmatter + 手順本文
    scripts/             # 任意。run_script が実行できるのはここ配下のみ
      xxx.py
    references/           # 任意。read_skill_file で読む補助資料
      notes.md
    assets/               # 任意。read_skill_file で読むテンプレート等
```

- `<skill-name>` はディレクトリ名。`SKILL.md` の `name` と**完全一致必須**（不一致は起動時スキャンでスキップされ、警告ログのみで起動は継続する）。
- `scripts/` / `references/` / `assets/` という名前自体はコードで強制されていない。強制されているのは「`run_script` はスキルフォルダの `scripts/` 配下にあるファイルしか実行できない」という1点のみ（`src/tools/_safe_path.py` の `_resolve_script_filename`）。呼び出し側は `scripts/` プレフィックスを書く必要はなく、ファイル名のみを渡せば `scripts/` 配下を再帰探索して解決する（同名ファイルが複数階層にある場合は最も浅い階層を採用）。`references/` `assets/` は Agent Skills 仕様上の慣例名であり、`read_skill_file` は skills ルート配下であればどのパスでも読める。

## 2. SKILL.md の形式

```markdown
---
name: my-skill              # 必須。1〜64文字、小文字英数字・ハイフン・アンダースコアのみ、先頭末尾は区切り文字不可、区切り文字の連続不可、ディレクトリ名と一致
description: ...            # 必須。1〜1024文字。「何をするか」+「いつ使うか」を書く
license: MIT                # 任意
metadata:                   # 任意
  author: ytakamoto
  version: "1.0"
---

# 本文（Markdown 自由記述）
```

- `name` / `description` の検証ルールは `src/skills.py` の `_validate()` が唯一の正。
- 検証に落ちたスキルは黙ってスキップされる（例外で全体を落とさない設計）。ログ（`app.log`）で `仕様違反のためスキップ` を確認できる。
- `description` は **LLMがスキルを選ぶ唯一の手がかり**（第1段階でシステムプロンプトに `name: description` 形式で列挙されるのみ、本文は含まれない）。「何をするか」だけでなく「どんなユーザー発話で使うべきか」を書くこと（既存2スキルを参照）。

## 3. progressive disclosure の3段階

| 段階 | 誰が | 何を | LLMに見えるもの |
|---|---|---|---|
| 1. Discovery | `scan_skills()` (起動時1回) | 全スキルの frontmatter を走査 | システムプロンプト内の `name: description` 一覧 |
| 2. Read | `read_skill` ツール | 該当スキルの `SKILL.md` 本文全体 | Markdown本文の生テキスト |
| 3. Execute | `read_skill_file` / `run_script` / `analyze_image` ツール | 補助資料の読み込み／スクリプト実行／画像閲覧 | ファイル内容、スクリプトの実行結果テキスト、または画像そのもの（Vision入力） |

LLMがどのスキルを読むか・どのスクリプトを叩くかは**すべてLLMの推論に委ねる**。コード側に選択ロジックはない（詳細は各ツールのdocstringとシステムプロンプトの指示文）。

## 4. カスタムツール（scripts/配下）からLLMへ値を渡す方法

**ここがコードに明文化された仕様がなく、今回追記した部分。**

### 4-0. SKILL.md本文の呼び出し例は Python コマンドライン形式で書く

**SKILL.md本文でLLMへ提示する呼び出し例は、`run_script`のJSON引数形式
（`{"skill_name":..., "script_filename":..., "script_args":[...]}`）を直接
書かない。** 代わりに、他の Agent Skills 環境（Claude Code 等）と共通の
`python <script>.py <args...>` というコマンドライン形式で書く（`skills/`配下の
全SKILL.mdは実際にこの形式で統一済み）。

理由: SKILL.md本文はAgent Skills仕様に準拠しており（6節参照）、フォーマット
自体は他環境でもそのまま通用する設計になっている。呼び出し例だけLocohane
固有のJSON形式で書いてしまうと、SKILL.mdフォルダを他環境へ持ち出したときに
そのままでは使えなくなり、可搬性を損なう。

`python <script>.py <args...>` 形式から実際の`run_script`呼び出しへの変換は、
LLM側の共通指示（`system_prompt/system_prompt.md`・`system_prompt/subagent_common.md`
の「SKILL.md呼び出し例の変換ルール」節）が担う。コード側（`_script_job.py`等）には
この変換ロジックは実装されておらず、あくまでプロンプトによる変換である点に注意
（SKILL.mdを新規追加・編集する際は、この変換ルールの存在を前提に、Locohane固有の
呼び出し形式をSKILL.md本文へ書かないようにする）。

`@N`（path_memory、4-7節参照）についても同様にSKILL.md本文には原則書かない
（呼び出し変換ルールの一部としてLLM側が理解する）。例外的に、スクリプトの出力
JSONスキーマの説明として`path_memory`キー自体の存在に触れる場合はこの限りではない。

### 4-1. 受け渡しの実体

`run_script` は `subprocess.run(..., capture_output=True, text=True)` でスクリプトを実行し、その結果を次の固定フォーマットの**1本のテキスト**に整形して返す（`src/tools/run_script.py` の `run_script` 末尾）:

```
[終了コード] <returncode>
[標準出力]
<stdout の内容（末尾空白除去）>
[標準エラー]
<stderr の内容（末尾空白除去、あれば）>
```

このテキストが LangChain の `ToolMessage.content` としてそのまま会話履歴に積まれ、次の LLM 呼び出し時にコンテキストとして渡る。つまり **スクリプトが LLM に値を渡す唯一の経路は標準出力（stdout）と終了コード** であり、それ以外（戻り値オブジェクト・グローバル変数・ファイルへの書き込みだけで済ませる、等）は一切LLMに伝わらない。

### 4-2. 制約

- **テキストのみ**。`encoding="utf-8", errors="replace"` でデコードされるため、バイナリ・画像等をそのまま `run_script` の戻り値として返す経路はない。生成物を見せたい場合はファイルに保存し、そのパスを stdout に含めて後続手順で扱わせる — **画像ファイルであれば `analyze_image` ツールでLLMへ視覚情報として渡せる**（`references/`/`assets/`配下の既存画像だけでなく、`run_script` がその場で生成した画像ファイルも同じ経路で見せられる。対応拡張子: png/jpg/jpeg/gif/webp/bmp/heic/heif）。ただしこれはVision対応モデルが前提であり、テキスト専用モデルでは画像部分は無視される点に注意。
- **タイムアウトあり**（既定60秒、`config.ini` の `script_timeout` で変更可）。超過時は `run_script` が「エラー: スクリプトが N 秒でタイムアウトしました。」を返し、スクリプト側の出力は破棄される。
- **`.py` は設定された Python 実行ファイルで起動**（`config.ini` の `script_python`。`[paths].project_locohane_dir` で `{"dir": ..., "python": ...}` と専用Pythonを指定したディレクトリの `skills/` 配下はそちらが優先）。それ以外の拡張子はOSに実行を委ねる（Windowsネイティブ環境のため、shebang行は解釈されない点に注意。`.py` 以外のスクリプトを置く場合は `.bat`/`.exe`等、Windowsで直接実行可能な形式にすること）。
- **作業ディレクトリ（cwd）はスキルフォルダではなく、ユーザーの作業ディレクトリ**（`src/tools/_workdir.py` の `_resolve_workdir()`。Chainlit設定の `work_dir`、未設定時は `config.ini` の `default_workdir`）になる。スキル自身のファイル（`scripts/`内の補助モジュール等）を参照する場合は `Path(__file__).resolve().parent` を使い、cwd起点の相対パスに依存しないこと。生成物をスキル実行のたびに使い捨てたいだけなら、cwd配下のセッション専用一時フォルダ `_tmp_<name>/`（名前は環境変数 `AGENT_EXEC_TMP_NAME` で取得、無ければ `AGENT_THREAD_ID` へフォールバック。会話終了時に自動削除される。`pdf-tools` の `render_pdf_pages.py` 参照）に書くと、スキル本体のディレクトリを汚さず済む。
- 呼び出し側は `skill_name` とスクリプトのファイル名（`script_filename`）のみを渡す。`_resolve_script_filename()` が `skill_name/scripts/` 配下（`_safe_path()` により `skills/` ルート配下に強制、ディレクトリトラバーサル対策）を再帰探索して解決するため、`scripts/` プレフィックスや絶対パスを書く必要はない。見つからない場合・`scripts/` ディレクトリ自体が無い場合は実行前にエラーを返す。

### 4-3. 推奨する値渡しの規約（コード非強制・慣例）

既存スキル `excel-vba-read` に倣い、以下を推奨する:

1. スクリプトは**構造化データを1行のJSONとしてstdoutへ**出力する（`print(json.dumps(result, ensure_ascii=False))`）。
2. **正常系は終了コード0**、stdoutにJSON。
3. **異常系は終了コード非0**、エラーメッセージは**stderr**へ（stdoutを汚さない）。
4. **原因が自明でない広い例外捕捉**（`except Exception as e:` でサードパーティ
   ライブラリ（COM、openpyxl、python-pptx/docx等）由来のエラーを丸めて報告する
   箇所）では、`str(e)` 一行だけでなく **`traceback.format_exc()` も併せて
   stderrへ出力する**こと（例: `print(f"...に失敗しました: {e}\n{traceback.format_exc()}", file=sys.stderr)`）。
   `str(e)` 一行しか返らないと、LLM（特にworkerサブエージェント）が原因を
   特定できず手探りの再試行でトークンを浪費する事例が実際に起きたため
   （excel-renderのCOM保存失敗が発端）。逆に、自前のバリデーションが
   `raise ValueError("...")` 等で原因を明示しているエラーは、メッセージ自体が
   既に自明なので traceback の追加は不要。
5. `SKILL.md` の本文に、**JSONのどのキーをどう解釈してユーザーに報告すべきか**を明記する（例: 「その `lines`/`words`/`chars` をユーザーへ日本語で分かりやすく報告する」）。LLMはコードを実行するのではなく**テキストとして返ってきたJSONを読んで解釈するだけ**なので、キーの意味をSKILL.md側で説明しておかないと誤読・幻覚のもとになる。
6. エッジケース（ファイル不在、空入力等）の挙動と、その際にLLMがユーザーに何を伝えるべきかも `SKILL.md` に明記する。

JSON化は必須ではない（`excel-knowledge` のように知識のみでスクリプトを持たないスキルもある）が、構造化データを返す場合はこの規約に従うと `SKILL.md` の手順が書きやすい。

### 4-4. ファイルを生成するスキルの追加規約: `output_path` / `output_paths`

`pdf-tools`/`pptx-create` のように、スクリプトが新規ファイル（PDF/PPTX/DOCX/XLSX等）を
生成する場合は、正常終了時のJSON出力に **`output_path` キー（生成した絶対パスの文字列）を
必ず含める**こと。1回の実行で複数ファイルを生成し、それらをまとめて1メッセージの
ダウンロードボタン列として見せたい場合は、代わりに **`output_paths` キー（絶対パスの
文字列のリスト）** を使う（両方存在する場合は `output_paths` が優先される）。

`app.py` の `on_tool_end` はツール結果の文字列から `src/files.py` の
`extract_generated_files()` を使ってこれらのキーを自動検出し、実在するファイルであれば
Chainlit UI上に `cl.File`（または画像なら `cl.Image`）付きメッセージとしてダウンロード
可能な添付を自動送信する（ツール名やスキル名による分岐は行わないため、この規約さえ
守れば新しい生成スキルを追加しても `app.py` 側のコード変更は一切不要）。複数件でも
1つのメッセージにまとめてボタンが並ぶ。

```json
{"output_path": "C:\\foo\\out.pptx", "total_slides": 4, "size_bytes": 34200}
```

```json
{"output_paths": ["C:\\foo\\out1.pdf", "C:\\foo\\out2.pdf"]}
```

`output_path` / `output_paths` 以外のキー名（`path`、`file`、`paths` 等）では検出
されない点に注意。

### 4-5. 画像をチャットメッセージ・Markdownテーブルへ埋め込む規約

スキルが生成・参照する画像をユーザーへ見せる経路は2つあり、`SKILL.md` の
本文（LLMへの指示）にどちらを使わせたいかを明記する必要がある。

1. **画像そのものを独立した1メッセージとして見せるだけでよい場合**:
   `analyze_image` ツールを `show_in_chat=True` で呼ばせる（`relative_path`
   に絶対パスを渡す）。LLM自身がその場で内容を理解するのと同時にチャットへも
   表示される（表示だけして中身は見ない、という呼び方はできない仕様）。表や
   他のテキストと組み合わせる必要が無い、単純な「生成した画像を見せて」
   ケース向け。
2. **回答本文の構造（一覧表など）の一部として画像を組み込みたい場合**:
   `show_in_chat` は使わず、**LLM自身の回答テキストに直接** Markdown画像記法
   `![説明](絶対パス)` を書かせる。Markdownテーブルのセル内に書いても
   構わない。`app.py` の `_send_answer()`（送信直前フック）が
   `_embed_local_images_as_session_urls()` を通し、実在する画像ファイルを
   指す `![...](絶対パス)` だけを自動でブラウザから取得可能なURLへ変換して
   から送る（ローカル絶対パスのままではブラウザは画像を読み込めないため。
   `analyze_image(show_in_chat=True)` と同じ、Chainlitのセッションファイル
   配信の仕組みに乗せる）。この方式ではLLM自身は画像の中身を見ないため、
   内容の説明・分析が必要な場合は別途 `analyze_image`（`show_in_chat=False`）
   で先に内容を把握させてから埋め込ませること。

いずれの方式でも、`SKILL.md` の指示文には以下を明記すること:

- パスは**必ず絶対パス**（スクリプトのJSON出力にある絶対パスをそのまま使う）。
  相対パスや存在しないパスは変換されず、壊れた画像として表示される。
- 方式2（回答本文への直接埋め込み）では **`@N`（パスメモリー参照）は使えない**。
  `@N` はツール引数専用の解決対象であり、回答本文（Markdown）の中に書いても
  解決されない。`Glob` 結果の `path_memory`（`{"@N": 絶対パス, ...}`）から
  対応する実パスを取り出させること。
- 方式2で埋め込まれるのは**縮小済みのサムネイル**（既定 長辺192px・JPEG品質70、
  `config.ini` の `[images].inline_preview_max_long_side_pixels`/
  `inline_preview_jpeg_quality`。逆に小さすぎる画像は
  `inline_preview_min_long_side_pixels` まで拡大される）。Vision向けの
  `analyze_image` の画像縮小設定（`max_long_side_pixels`/
  `jpeg_quality`）とは別物で、意図的に小さい（表示帯域・ディスク使用量を
  抑える目的。細部を確認させたい／高解像度で見せたい場合は方式2ではなく
  方式1（`analyze_image` を `show_in_chat=True` で呼び、独立メッセージとして
  原寸で見せる）を使わせること）。
- 表の**前に同じ内容（対象パスの一覧など）を番号付きリスト等で重複して書かせない**
  こと。表の直前に別のブロックがあり間に空行が無いと、Markdownパーサーが表を
  独立ブロックと認識できず直前のブロックの続きとして飲み込んでしまい、表ごと
  （画像も含めて）描画されなくなることがある（実機検証で確認済みの不具合）。
  `_send_answer()`（`app.py`）が送信直前に空行を自動挿入する保険は入れてある
  が、`SKILL.md` 側でも「一覧は表だけで完結させる」よう指示しておくのが安全。

**テンプレート例**（複数の画像を生成し、一覧表で見せるスキルの `SKILL.md` 本文）:

```markdown
## 出力の解釈方法

`scripts/render_gallery.py` は正常終了時、次のJSONを標準出力へ返す:

    {"images": [
      {"label": "1枚目", "output_path": "C:\\...\\out1.png"},
      {"label": "2枚目", "output_path": "C:\\...\\out2.png"}
    ]}

ユーザーへは、`analyze_image` の `show_in_chat` は使わず、回答本文に直接次の形式でMarkdown表を
書いて画像プレビュー付きの一覧として示すこと（`output_path` は絶対パスの
まま使う。`@N` に置き換えない）:

    | No. | ラベル | プレビュー |
    |---|---|---|
    | 1 | 1枚目 | ![1枚目](C:\...\out1.png) |
    | 2 | 2枚目 | ![2枚目](C:\...\out2.png) |
```

### 4-6. 中間生成物の一時保存先: `_tmp_<name>/` と `exec_tmp_dir()`

会話終了時に自動削除されるセッション専用の一時フォルダ `_tmp_<name>/` を
作れる。中間生成物（変換途中のPDF、レンダリング画像など、最終成果物ではない
ファイル）はここに書き、作業ディレクトリ本体を汚さないこと。

`<name>` は「作成時刻（秒まで）+ thread_id の短縮ID（SHA-1 先頭10桁）」（例:
`260826012433_3f2a9c1b7e`）。先頭の時刻は default_workdir 直下に並ぶ `_tmp_*`
フォルダをファイラーで見たとき作成順に並ぶようにするため、thread_id を短縮IDに
しているのはパス長上限（260文字）を超えにくくするため。thread_id から
組み立て直すことはできないので、スキルスクリプト側はこのフォーマットを
気にせず、下記の環境変数からそのまま読めばよい。

**基準ディレクトリは常に default_workdir（環境変数 `AGENT_DEFAULT_WORKDIR`）**
であり、`run_script` の cwd（4-2参照。ユーザーが ChatSettings で指定した
work_dir になりうる）**ではない**。work_dir はユーザー指定の場所のため
config.ini `[default_workdir].retention_days` の保持日数ベース自動削除の対象外
であり、`Path.cwd()` を基準にすると `_tmp_<name>/` が work_dir 配下に
作られたまま消えずに溜まり続ける（過去に実際に発生した回帰。cwd基準は
バグであり仕様ではない）。`AGENT_DEFAULT_WORKDIR` は `run_script`/
`execute_python_code` のサブプロセス起動時に常に注入される
（`src/tools/_subprocess_env.py` の `_subprocess_env()`）。

`src/path_memory.py` に、このフォルダを作成して返すヘルパー `exec_tmp_dir()` がある
（`execute_python_code` が内部で使う `_resolve_exec_workdir()` と同じ命名規約）。
スキルスクリプトから使う場合は、`register_output_path`（4-1参照）と同じ手順で
`AGENT_SRC_DIR` 経由で import する:

```python
import os, sys
src_dir = os.environ.get("AGENT_SRC_DIR")
if src_dir and src_dir not in sys.path:
    sys.path.insert(0, src_dir)
import path_memory

out_dir = path_memory.exec_tmp_dir("pdf_pages")  # _tmp_<name>/pdf_pages を作成して返す
```

**互換性の注意**: `register_output_path`（4-1）は「`AGENT_SRC_DIR` 未設定・import失敗
時は例外を出さず None へフォールバックする」ソフト依存だが、`exec_tmp_dir()` は違う。
呼び出すには先に `import path_memory` が成功している必要があり、それが失敗する環境
（`AGENT_SRC_DIR` を注入しない Agent Skills ランタイム等）では import 文の時点で
スクリプトが動かなくなる。`_tmp_<name>/` の作成自体は本来 `os.environ` と
`pathlib` だけで完結する処理であり、これを `path_memory` の import に依存させると、
Locohane 以外の環境でも単体で動くことを前提にしたスキル（`skills/OFFICE_SKILLS_README.md`
2節参照）の可搬性を落としてしまう。

そのため、**他の Agent Skills 対応環境でも動かしたい／互換性を保ちたいスキルでは
`exec_tmp_dir()` を import せず、同じロジックをスクリプト側に直接実装する**こと
（`src/path_memory.py` に依存しない、標準ライブラリのみの実装）:

```python
import os
from pathlib import Path

def _exec_tmp_dir(category: str | None = None) -> Path:
    # ディレクトリ名は AGENT_EXEC_TMP_NAME（execute_python_code が実際に
    # 作ったディレクトリと同じ名前）を優先し、無ければ AGENT_THREAD_ID
    # （生のthread_id）へフォールバックする。
    # ここを AGENT_THREAD_ID 単独にすると、execute_python_code が作る
    # 実際のディレクトリ名（作成時刻 + 短縮ID）と食い違い、書き込み
    # ガードに「他セッションの一時ディレクトリ」として弾かれる。
    name = os.environ.get("AGENT_EXEC_TMP_NAME") or os.environ.get("AGENT_THREAD_ID") or "_no_session"
    base_dir = Path(os.environ.get("AGENT_DEFAULT_WORKDIR") or "./data/temp")
    out_dir = base_dir / f"_tmp_{name}"
    if category:
        out_dir = out_dir / category
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir
```

**`Path.cwd()` を基準にしないこと。** `AGENT_DEFAULT_WORKDIR` を必ず基準にする
（`skills/pdf-tools/scripts/_common.py` の `write_json_result` などは実際にこの
インライン実装のまま運用されている。`path_memory.exec_tmp_dir()` と処理内容は
同一だが、依存を持たせない意図で意図的に重複させている）。

### 4-7. カスタムスキルへの path_memory（`@N`）実装方法と、`@N` が実パスへ変換される場所

`path_memory` は、Read/Glob/Grep 等が返す長い絶対パスに短い数値インデックス
`@N` を割り当てて記憶し、以降のツール呼び出しでは `@N` だけ渡せば実パスへ
解決できるようにする仕組み（`src/path_memory.py`）。低パラメータモデルが
長いパス文字列を複数回のツール呼び出しにまたがって正確に再生成できず
タイプミスを頻発させる問題への対策であり、カスタムスキルが生成・更新した
ファイルも同じ仕組みに乗せることで、LLMが以後そのファイルを扱うツール
呼び出し（`run_script` の次回呼び出しや `analyze_image` 等）で `@N` を
使い回せるようになる。

#### 4-7-1. スクリプト側の実装: `register_output_path()`

`skills/office_shared/excel_common.py` や `skills/pdf-tools/scripts/_common.py`
に実装済みの以下のパターンを、新しいスキルの `scripts/` 配下にも**そのまま
コピーして使う**（各スキルの共通モジュールへ意図的に重複実装されている。
理由は4-6節の互換性の注意と同じ — `src/path_memory.py` への import
依存を避け、Locohane以外の環境でも単体で動く可搬性を保つため）:

```python
def register_output_path(path, description: str | None = None) -> dict[str, str] | None:
    """生成/更新したファイルをパスメモリーへ登録し、{"@N": 絶対パス} を返す。"""
    src_dir = os.environ.get("AGENT_SRC_DIR")
    if not src_dir:
        return None
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        import path_memory
    except ImportError:
        return None
    thread_id, pm_dir, max_entries = path_memory.env_params()
    abs_path = str(Path(path).resolve())
    idx = path_memory.register(thread_id, abs_path, pm_dir, max_entries, description=description)
    if idx is None:
        return None
    return {f"@{idx}": abs_path}
```

- `AGENT_SRC_DIR`（`run_script` が常にサブプロセスへ注入する。4-2参照）経由で
  `src/path_memory.py` を import する。未設定・import失敗時は例外を出さず
  `None` を返す（run_script以外から直接実行された場合でもスクリプト自体を
  失敗させないフェイルオープン）。
- `path_memory.env_params()` が環境変数（`AGENT_THREAD_ID` /
  `AGENT_PATH_MEMORY_DIR` / `AGENT_PATH_MEMORY_MAX_ENTRIES`。いずれも
  `_subprocess_env()` が注入）から `(thread_id, path_memory_dir, max_entries)`
  を読む。
- 正常終了時のJSON出力（4-1〜4-3の規約）に、`output_path`（4-4）とは別に
  `path_memory` キーとして戻り値をそのまま含める:

  ```json
  {"output_path": "C:\\foo\\out.xlsx", "path_memory": {"@12": "C:\\foo\\out.xlsx"}}
  ```

  `SKILL.md` の本文には、後続のツール呼び出し（次回の `run_script` や
  `analyze_image` 等）でこのファイルを再度参照する必要がある場合、絶対パス
  ではなく `path_memory` の `@N` を使うようLLMへ指示しておく。

#### 4-7-2. `@N` が実パスへ変換される場所（呼び出し側の変換であり、スクリプト側は関与しない）

**重要: `@N` はスキルの `scripts/` 配下のスクリプトに渡る前に、ツール実装側で
既に実パスへ解決済みになる。** スクリプト自身が `@N` という文字列を受け取って
自分でパースする必要は無い（＝解決するのはLLM自身でも `register_output_path()`
でもなく、`run_script` 等のツール実装コード）。

- **`run_script(script_args=[...])`**: `src/tools/_script_job.py` の
  `_resolve_run_script_command()` が、サブプロセス起動前に `script_args` の
  各要素を `_resolve_path_memory_token()`（`src/tools/_path_memory_helpers.py`）
  に通し、`@N` 形式の要素だけを実パスへ置き換える（フォルダの `@N` の後ろに子パスを続けた
  `@N/photo_01.png`・`@N\photo_01.png` も、フォルダ内のそのパスへ解決する。ファイルの `@N` に
  子パスを続けるとエラー）。つまり `scripts/xxx.py`
  が `sys.argv` で受け取る時点では既に生の絶対パス文字列になっている。
  未登録の `@N` を渡した場合はスクリプトを起動する前にエラー文字列を返す
  （スクリプトは実行されない）。
- **`analyze_image(relative_path=...)`**: `src/tools/analyze_image.py` が
  同じ `_resolve_path_memory_token()` を呼び出し内で直接使う。
- **`dispatch_agent(task=...)` のような自由記述テキスト中の `@N`**:
  文字列全体が `@N` 単体とは限らないため、正規表現で本文中の `@N` を検出して
  置換する `_resolve_path_memory_tokens_in_text()` を使う（未登録分は
  エラーにせず `@N` のまま残す）。
- **`execute_python_code(code=...)` の `code` 文字列中の `@N` は自動解決
  されない**（コード文字列は自由記述のPythonソースであり、どの部分が
  パス引数かをツール側は判別できないため）。この場合はLLM自身が生成する
  コードの中で `path_memory.resolve(thread_id, "@N", Path(pm_dir))` を
  明示的に呼ぶ必要がある（`execute_python_code.py` のdocstring参照）。

まとめると、**`run_script`/`analyze_image` に渡す引数としての `@N` は
ツール実装側が呼び出し前に解決するため、カスタムスキルの `scripts/` 配下の
スクリプトを書く際に `@N` の解決コードを自前で書く必要は無い**。書く必要が
あるのは「生成したファイルを `@N` として登録する」`register_output_path()`
（4-7-1）の方だけである。

## 5. 新しいスキルを追加する手順

1. `skills/<skill-name>/SKILL.md` を作成（frontmatter必須、`name` はディレクトリ名と一致）。

   **名前は既存スキルと接頭辞を共有させない**: 新しいスキル名は、既存
   スキル名のいずれとも文字列としての接頭辞関係を持たないようにする。
   LLMがスキルを選ぶ手がかりは`name: description`の一覧のみであり、似た
   接頭辞を持つスキルが並ぶと、より短い/単純な方の名前へ誤って吸い寄せ
   られるリスクがある（agent_type名での実例は`AGENTS_README.md` 8節参照）。
2. 必要なら `skills/<skill-name>/scripts/` にスクリプトを置く（`.py` 推奨、Windows実行環境のため）。
3. `SKILL.md` 本文に、いつ使うか・手順・呼び出しコマンド（`python <script>.py <args...>` 形式。4-0節参照）・出力の解釈方法・エッジケースを書く。
4. アプリを再起動して `scan_skills()` に発見させる（ホットリロードなし。起動ログの `スキル発見: <name>` を確認）。
5. 実際にチャットから使ってみて、`read_skill` → （必要なら `read_skill_file` / `analyze_image`）→ `run_script` の順にツールが呼ばれることを確認する。

## 6. Anthropic互換について

`SKILL.md` のfrontmatter形式・progressive disclosureの3段階構成は [Anthropic の Agent Skills 仕様](https://agentskills.io/specification)（Claude Code / Claude.ai のSkill機能と同じ仕様）に準拠している。そのため **SKILL.md自体の書式はClaude向けスキルと相互互換**であり、他のAgent Skills対応環境（Claude Code等）に同じ `skills/<name>/` フォルダを持っていけばそのまま認識される可能性が高い。

ただし以下は本プロジェクト独自の実装であり、Anthropicのランタイムをそのまま使っているわけではない点に注意:

- LLM本体は **llama.cpp server（OpenAI互換API）** に接続しており、Claude/Anthropic APIは使用していない（`src/graph.py` の `build_model()` 参照）。
- `read_skill` / `read_skill_file` / `run_script` / `analyze_image` という4ツールはこのプロジェクトが `src/tools/` パッケージに独自実装したものであり、Claude Code本体のSkillツール実装そのものではない（挙動を仕様に沿って再現しているだけ）。
- 仕様のうち本プロジェクトが実装しているのは frontmatter検証・3段階progressive disclosureのみ。仕様に存在しうるその他の付随機能（あれば）は未実装。

つまり「**SKILL.mdのフォーマット・設計思想はAnthropic仕様準拠だが、実行系（LLM・ツール実装）はAnthropicのものではなく完全に自前**」というのが正確な位置づけ。

この方針を徹底するため、SKILL.md本文の呼び出し例もLocohane固有のJSON形式ではなく
`python <script>.py <args...>` 形式で統一している（4-0節参照）。実行時のLLMが
この記法を`run_script`呼び出しへ変換する規則を`system_prompt/`側に持つことで、
SKILL.mdフォルダ自体は他のAgent Skills対応環境へそのまま持ち出せる状態を保っている。

## 7. オーケストレータースキルの規約: `dispatch_agent` への委譲時の必須ルール保持

複数のSKILLを横断的に使うよう指示するスキル（以下「オーケストレータースキル」）は
本文が長くなりやすい。サブエージェントへの委譲プロンプト（`task`）を組み立てる際に
メインエージェントがその内容を自由記述で要約しようとすると、必須ルール・禁止事項
のような細部が要約の過程で脱落しやすい（LLMによる自由記述の要約は本質的にlossy）。

### 7-1. SKILL.md側の規約

オーケストレータースキルの `SKILL.md` は、本文冒頭（frontmatter直後）に
**「本スキルはオーケストレータースキルです。」**と明記すること。この一文は
メインエージェントが `dispatch_agent` 呼び出し時に `orchestrator_skill` 引数へ
このスキル名を渡すべきか自己判断するための唯一の手がかりであり、コード側に
判定ロジックは無い（すべてLLMの推論に委ねる設計。3節の「LLMがどのスキルを
読むか」と同じ思想）。

必須ルール・禁止事項がある場合は、本文中で「## 必須ルール」「## 禁止事項」の
ように独立した見出しで明示しておくこと。後述の通りSKILL.md全文がそのまま
サブエージェントへ渡るため見出しの有無自体は動作に影響しないが、サブエージェント
側が読みやすくなる。

### 7-2. `dispatch_agent(orchestrator_skill=...)` の挙動

`dispatch_agent` ツールは `orchestrator_skill` 引数（任意）を受け取る
（`src/tools/dispatch_agent.py`）。指定すると、該当スキルの `SKILL.md` 本文
**全体**がシステム側で機械的に読み込まれ、`task` の先頭にそのまま埋め込まれた
上でサブエージェントへ渡る。メインエージェントが本文を要約・抜粋してtaskへ
書き写す必要は無く、むしろそれをしない（LLMの手でコピーしない）ことで、
必須ルール・禁止事項が委譲時に脱落するリスクそのものを無くすのが狙い
（`_task_with_work_dir_hint` / `_task_with_plan_hint` と同じ「委譲元の
書き起こしを信用せず、ground truthを機械的に注入する」パターン）。

- 存在しないスキル名を指定した場合は起動前に「エラー: オーケストレータースキル
  '...' の SKILL.md が見つかりません。スキル名が間違っている可能性が高いです。」
  を返し、job は作られない。
- 通常の（複数スキル横断ではない）委譲では `orchestrator_skill` を省略してよい。
