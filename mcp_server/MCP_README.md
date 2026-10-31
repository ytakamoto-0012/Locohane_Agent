# 独自スキル群をMCPサーバーとして配布する方法

`skills/`（Agent Skills仕様準拠、詳細は [`skills/SKILLS_README.md`](../skills/SKILLS_README.md)）に
溜めたSKILL.md群を、他のMCPクライアント（Claude Code、Claude Desktop、Cursor等）から
発見・取得・**実行**できるようにする方法をまとめる。実体は `mcp_server/` パッケージ
（後述）。

**注意: これは「MCPサーバーへ接続する」機能（`README_DETAIL.md` の「MCPサーバー接続」節、
`src/mcp_client.py`）とは逆方向の話。** そちらはLocohaneが外部MCPサーバーの
ツールを**使う側**（クライアント）。このドキュメントはLocohaneのスキルを
外部へ**配る側**（サーバー）にする方法であり、現状Locohaneにこの機能の実装は無い。

---

## 1. MCPの基礎（前提知識）

MCP（Model Context Protocol）は通信規格（JSON-RPCベース）に過ぎず、中身は自由に設計できる。
サーバーが公開できる部品は3種類:

| 部品 | 何をLLMに渡すか | 例 |
|---|---|---|
| Tools | 呼び出し可能な関数（LLMが実行を指示し、結果が返る） | DB検索、API呼び出し、スクリプト実行 |
| Resources | 読み込めるデータ（受け取った側が中身を読むだけ） | ファイル内容、ドキュメント |
| Prompts | 定型の指示テンプレート | — |

当初は **SKILL.mdをそのまま「読めるデータ」として配る（Resources方式）** のみを
実装していたが、これだけではMCPクライアント（Claude Code等）はSKILL.mdの中身を
「読める」だけで「実行」できない（Resources方式の性質上、**MCPサーバー自身は
SKILL.mdのテキストを右から左に渡すだけで、実際にスクリプトを実行するのは
受け取った側のAIエージェント**である。したがって配布先の環境にも、そのスキルが
前提とする実行環境（Python、`officecli.exe`等）が揃っている必要がある。6節参照）。

そのため現在は **Resources方式とTools方式を併用**している。役割分担は以下の通り:

| 方式 | 何をするか | 実行主体 |
|---|---|---|
| Resources（`skill://...`） | SKILL.md・補助ファイルをテキストとして読める | 受け取った側（MCPクライアントの実行環境） |
| Tools（`list_skills`/`read_skill`/`run_skill_script`） | スキルの発見・SKILL.md閲覧に加え、**scripts/配下のスクリプトをMCPサーバー自身が実行**し結果を返す | **MCPサーバー（Locohaneが動くこのマシン）** |

Tools方式の `run_skill_script` は、MCPクライアント（Claude Code等）が動く環境ではなく
**MCPサーバーが動いているマシン上**でスクリプトを実行する点に注意
（`mcp_server/skill_tools.py` が `subprocess.run` で直接起動する）。
Claude Code自身がこのマシン上で動いている場合（本プロジェクトの主な想定用途）は
差を意識する必要はないが、リモートの別マシンからMCP接続する場合は
「スクリプトの実行環境・生成物の保存先は常にサーバー側」になる点に注意。

---

## 2. なぜFastMCPのSkills Providerが使えるか

**FastMCPはAnthropic公式ではない。** 公式なのはMCPのプロトコル仕様
（[modelcontextprotocol.io](https://modelcontextprotocol.io/specification)）と、
それを素朴に実装した公式SDK（PyPI: `mcp`。Locohaneが`src/mcp_client.py`で
クライアントとして使っているのもこれ）まで。[FastMCP](https://gofastmcp.com/)
（PyPI: `fastmcp`）は公式SDKの上に被さるサードパーティ製ラッパーだが、
Python製MCPサーバー実装のデファクト標準として広く使われており、一部機能は
公式SDK側にも逆輸入された経緯がある。

FastMCP 3.0（2026年1月リリース）で追加された`SkillsDirectoryProvider`は
FastMCP独自の付加機能（公式`mcp`パッケージ単体には無い）で、ディレクトリを
渡すだけで配下の全`SKILL.md`を自動スキャンし、MCPリソースとして公開してくれる。

Locohaneの`skills/<name>/SKILL.md`（frontmatter必須、Agent Skills仕様準拠、
`skills/SKILLS_README.md` 6節）はこのプロバイダーがそのまま前提とする形式であり、
**新たにコードを書き起こす必要がない**。これが1節で「配布方式」を選ぶ最大の理由。

### 2-1. 他のMCPサーバー実装ライブラリという選択肢

MCPは仕様が公開されているだけなので、実装ライブラリは言語ごとに複数ある。
Python限定でも以下のような選択肢があり、FastMCP一強というわけではない。

| ライブラリ | 位置づけ |
|---|---|
| 公式SDK（`mcp`パッケージ） | Anthropic公式。プロトコルの生実装。Locohaneが`src/mcp_client.py`で使っているのもこれ |
| **FastMCP**（PrefectHQ） | 非公式だが事実上の標準。全MCPサーバー実装の約7割がベースに使っているとされる |
| FastAPI-MCP | 既存のFastAPIアプリをそのままMCPサーバー化したい場合向け |

FastMCPの「デコレータで書ける高レベルAPI」自体の設計思想は2024年に公式Python SDKにも
取り込まれているが、**本ドキュメントで使う`SkillsDirectoryProvider`はFastMCP独自の
付加機能であり、公式`mcp`パッケージ単体には無い**。今回FastMCPを選ぶのは、この
Skills Provider機能がSKILL.md配布という目的に対して実装コストを最小化できるため。
Locohaneは既にChainlit内でFastAPIを部分的に使っているため、将来「配布だけでなく
本格的にツールも公開したい」となった場合はFastAPI-MCPも比較対象になりうる。

---

## 3. 実装方法（動作確認済み）

以下はWeb調査だけの案ではなく、実際にLocohaneの実行環境
（`C:\work\venv_locohane\Scripts\python.exe`）に
`fastmcp==3.4.7`をインストールし、`skills/`（24スキル、131ファイル）に対して
実行して動作確認したコード。プロジェクト直下の `mcp_server/` パッケージとして
実在する（エントリーポイントは `mcp_server/server.py`。そのまま
`python mcp_server/server.py` で起動できる）。

```
mcp_server/
  __init__.py     # 空（パッケージ化のみ）
  config.py        # 設定値の集約（SKILLS_SRC/WORKDIR/タイムアウト秒数/SCRIPT_PYTHON/除外ファイル名）
  publish.py        # Resources配布用の一時コピー作成（build_publish_dir）
  skill_tools.py     # Tools方式の3ツール本体（list_skills/read_skill/run_skill_script）
  server.py          # エントリーポイント（FastMCPインスタンス生成、Resources/Tools登録）
```

`config.py`の設定値は、コードを書き換えずに以下の環境変数で上書きできる
（いずれも未設定時は既定値のまま）:

| 環境変数 | 対象 | 既定値 |
|---|---|---|
| `LOCOHANE_MCP_SKILLS_SRC` | 対象とする `skills/` ディレクトリ（`os.pathsep`区切りで複数指定可、同名スキルは先頭優先） | プロジェクト直下 `skills/` の1つのみ |
| `LOCOHANE_MCP_SKILLS_WORKDIR` | `run_skill_script` のサブプロセス cwd | 未設定（MCPサーバー自身の起動時cwdを継承） |
| `LOCOHANE_MCP_SKILLS_SCRIPT_TIMEOUT_SECONDS` | `run_skill_script` のタイムアウト秒数 | 300 |
| `LOCOHANE_MCP_SKILLS_PYTHON` | `run_skill_script` がスクリプト起動に使うPython実行ファイル | 未設定（`sys.executable`、このMCPサーバー自身のPython） |
| `LOCOHANE_MCP_SKILLS_NAME` | MCPサーバー自体の名前（FastMCPインスタンス名） | `locohane-skills` |

### 3-1. 依存追加

```
pip install fastmcp==3.4.7
```

`requirements.txt` には追記済み（`fastmcp==3.4.7`。プロジェクト直下
`CLAUDE.md` の「新しいpipライブラリのインストールが必要な時は、必ず
requirements.txtを更新する」ルールに従った）。

### 3-2. サーバー本体（.env等の機密ファイル除外つき）

**重要な前提: `SkillsDirectoryProvider`が使う`scan_skill_files()`は
`skill_dir.rglob("*")`でスキルフォルダ配下を無条件かつ再帰的に全スキャンする
（FastMCP 3.4.7のソース `fastmcp/server/providers/skills/_common.py` で確認済み）。
拡張子フィルタ・隠しファイル除外・`.gitignore`的な仕組みは一切無い。**
Locohaneの`web-search`スキルは`scripts/.env`にTAVILY_API_KEYを置く設計
（`skills/SKILLS_README.md`）のため、`skills/`をそのまま`roots`に渡すと
**APIキーがそのまま配布されてしまう**。これを避けるため、配布直前に
機密ファイルを除いた一時コピーを作ってから`roots`に渡す。

除外対象のファイル名一覧は `mcp_server/config.py` の `EXCLUDE_NAMES` に、
配布用コピー作成のロジックは `mcp_server/publish.py` の `build_publish_dir()` に
それぞれ集約している（実装は当初のプロジェクト直下 `mcp_skills_server.py` から
そのまま移動したもので、ロジック自体に変更はない）。

`supporting_files`は"template"（既定）と"resources"の2択だが、ソースコード
（`skill_provider.py`）を読むと**どちらでも`_manifest`（全ファイルのパス・
サイズ・ハッシュ一覧、JSON）は無条件で公開される**。違いは`list_resources()`
に個別ファイルが最初から並ぶかどうかだけで、"template"でも`_manifest`で
パスさえ知れば`skill://{name}/{path}`のワイルドカードURI経由で個別ファイルを
読めてしまう。**つまり"template"は一覧を隠すだけで、アクセス制御にはならない。**
機密ファイル対策は`supporting_files`の値ではなく、上記のような配布前フィルタで
行う必要がある。

公開されるリソースURIの形式（例: `pdf-tools`スキルの場合）:

```
skill://pdf-tools/SKILL.md
skill://pdf-tools/_manifest
skill://pdf-tools/references/notes.md
skill://pdf-tools/scripts/render_pdf_pages.py
```

**注意: Resources経由（`skill://...`）で配られるのは`scripts/xxx.py`の
"ソースコード（テキスト）"であり、実行結果ではない。** MCPリソースの仕組み自体は
コードを一切実行しない。「MCPサーバーがその場でスクリプトを実行して結果を返す」
には、次の3-2b節のTools方式（`run_skill_script`）を使う。

### 3-2b. Tools方式（`mcp_server/skill_tools.py`）

`@mcp.tool()`（実装では `mcp.add_tool()`）で登録している3つの関数:

- **`list_skills()`** — 全スキルの `name: description` 一覧を返す
  （Agent Skills仕様のprogressive disclosure第1段階＝Discovery相当）。
- **`read_skill(skill_name)`** — 指定スキルの `SKILL.md` 本文を返す（第2段階＝Read相当）。
- **`run_skill_script(skill_name, script_filename, script_args)`** — 指定スキルの
  `scripts/` 配下のスクリプトを `subprocess.run` で実際に実行し、
  `[exit code] N` / `[stdout]` / `[stderr]` の形式で結果を返す（失敗時は `Error: ...`。
  ツールの説明文（docstring）と同じく、MCP クライアントの LLM に渡る文言は英語）
  （第3段階＝Execute相当）。Locohane本体の `src/tools/run_script.py` と
  同じ出力フォーマットにしているため、既存SKILL.mdの「出力の解釈方法」の
  説明がClaude Code向けにもそのまま通用する。

この3ツールが見るのは（Resources配布用の`.env`除外コピーではなく）
**元の`skills/`そのもの**（`config.SKILLS_SRC`）。`run_skill_script`は
MCPサーバーと同一マシン上の信頼された呼び出し元がその場で実行するだけなので、
Resources配布（外部への機密漏洩リスクがある）とは脅威モデルが異なり、
`web-search`の`scripts/.env`（TAVILY_API_KEY）等も問題なく使える。

`run_skill_script`のサブプロセスのcwdは既定では固定しない（MCPサーバー自身の
起動時cwdをそのまま継承する。Claude Codeの場合、これは通常ワークスペース
＝プロジェクトルートと一致する）。Locohaneのスキルは生成物の出力先を
`output_path`等の明示的な引数で受け取る設計（`skills/SKILLS_README.md`
4-4節）のため、cwdを強制で固定する必要は薄い——絶対パスで指定すれば
cwdに関係なく書き出せる。Claude Desktop等cwdが不定なクライアントから
使う場合や、生成物の既定置き場を固定したい場合は環境変数
`LOCOHANE_MCP_SKILLS_WORKDIR`でcwdを上書きできる（`mcp_server/config.py`）。

タイムアウトは同ファイルの `SCRIPT_TIMEOUT_SECONDS`（既定300秒、Locohane本体の
`config.ini` `[scripts].timeout` と同値）。環境変数
`LOCOHANE_MCP_SKILLS_SCRIPT_TIMEOUT_SECONDS`（整数）で上書きできる。

スクリプトを起動するPython実行ファイルは既定で`sys.executable`
（このMCPサーバー自身を起動しているPython）だが、Locohane本体の
`config.ini` `[scripts].python`（スキルが前提とする依存関係の入った
仮想環境）と一致しない起動経路（Claude Desktop等、MCPクライアントの
設定にある`command`が`env_local_agent_system`以外のPythonを指す場合）も
あるため、環境変数`LOCOHANE_MCP_SKILLS_PYTHON`で明示的に上書きできる
（`mcp_server/config.py`の`SCRIPT_PYTHON`）。Locohane本体にある書き込み
サンドボックスガードや`create_plan`/`approve_plan`の承認フロー（Chainlitの
マルチユーザー運用が前提）はここには無い。MCP接続元（Claude Code等）は
既にこのマシン上でファイルシステムへフルアクセスできる前提のため、追加の
安全境界を設けても意味がないと判断した。

### 3-3. サーバーとしての実行方法

**単体起動**（プロジェクト直下で）:

```
C:\work\venv_locohane\Scripts\python.exe mcp_server\server.py
```

実行するとstdioトランスポートで待ち受け続けるフォアグラウンドプロセスが
起動する（標準入出力でJSON-RPCをやり取りするため、ターミナルに文字が
流れているように見えるのが正常。ログはstderrに出る）。**このプロセス自体を
単独で終了させたい場合はCtrl+Cで止める。** ターミナルを閉じずに待機させて
おくか、クライアント側の設定（4節）から自動起動させる運用が基本になる。

**クライアント側からの自動起動（通常の使い方）**: MCPのstdioトランスポートは
「クライアントがサーバーをサブプロセスとして起動する」設計のため、通常は
上記コマンドを手動で実行しっぱなしにする必要はない。4節の
`claude mcp add`やClaude Desktopの設定ファイルに`command`/`args`として
`python mcp_server\server.py`（絶対パス）を登録しておけば、クライアント
（Claude Code / Claude Desktop）が接続時に自動でこのプロセスを起動し、
切断時に終了させる。`server.py`はcwdに依存せず`__file__`基準で
プロジェクトルートを解決するため（冒頭の`sys.path.insert`）、絶対パスで
起動する限りどのディレクトリから呼ばれても動く。

**実際に別プロセスとして起動し、stdio経由で接続できることを確認済み**
（プロジェクト直下で以下を実行。`Client`にファイルパスを渡すとFastMCPが
自動的に`python mcp_server/server.py`をサブプロセス起動して接続する）:

```python
import asyncio
from fastmcp import Client

async def main():
    async with Client("mcp_server/server.py") as client:
        print(await client.ping())                       # -> True
        print(len(await client.list_resources()))         # -> 114
        print(len(await client.list_tools()))              # -> 3

asyncio.run(main())
```

確認結果: `ping()`が`True`を返し、別プロセスとして起動した
`mcp_server/server.py`から114件のリソースと3件のツールが取得できることを
確認した（起動時にFastMCPのバナーとログがstderrへ出力される）。

### 3-4. in-memoryでの動作確認（実施済み）

FastMCPの`Client`はサーバーオブジェクトをそのまま渡すとin-memoryで接続できる
（サブプロセス起動もネットワークも不要、単体テスト向け）。以下を実際に実行して確認した:

```python
import asyncio
from fastmcp import Client
from mcp_server.server import mcp

async def main():
    async with Client(mcp) as client:
        resources = await client.list_resources()
        print(len(resources))  # -> 114

asyncio.run(main())
```

確認結果:

- 除外処理なしでは131件、`.env`/`.env.local`/`__pycache__`除外後は**114件**の
  リソースが公開される。
- `client.list_resources()`の結果に`.env`系ファイルが**含まれないこと**を確認済み
  （`ENV_LEAKS=[]`）。
- `client.read_resource("skill://pdf-tools/SKILL.md")`でSKILL.mdの本文
  （frontmatter含む）がUTF-8のテキストとして正しく取得できることを確認済み。

コマンドラインからMCP Inspector（ブラウザのデバッグUI）で確認したい場合:

```
fastmcp dev mcp_server/server.py
```

（この方法自体は今回未実施。上記のin-memory Client方式で同等の検証は完了している）

---

## 4. クライアント側の接続設定

### Claude Code

#### CLI版

`command`にはシステムPATHの`python`ではなく、Locohaneの実行環境の
フルパスを指定すること（`python`が素のまま解決できず接続失敗する
既知の不具合があるため。実測: stdioサーバーのstderrに
「'python' は、内部コマンドまたは外部コマンドとして認識されていません」）。

```
claude mcp add --transport stdio locohane-skills -- "C:\work\venv_locohane\Scripts\python.exe" "C:\work\Locohane_Agent\mcp_server\server.py"
```

登録後、`claude mcp get locohane-skills`で`Status: ✔ Connected`、
`/mcp`で`3 tools`（`list_skills`/`read_skill`/`run_skill_script`）が
表示されることを確認する。

上記はscope指定なしだと既定で**localスコープ**（`~/.claude.json`の
`projects`オブジェクトへ、現在の作業ディレクトリの絶対パス文字列をキーとして
保存）に登録される。project scope/user scopeで登録したい場合は`--scope`
オプションを付ける（以下もCLI版のコマンド）。

**方法1: project scope（推奨）** — プロジェクトルートに`.mcp.json`が
作られ、パス文字列キーに依存しないため、CLI版・VSCode拡張版どちらからも
確実に認識される。`command`/`args`にはこのマシンのフルパスがそのまま
書き込まれるため、`.mcp.json`はgit管理外（`.gitignore`）にしてあり、
共有するのは例の`.mcp.json.example`だけ。

```
claude mcp add --transport stdio --scope project locohane-skills -- "C:\work\venv_locohane\Scripts\python.exe" "C:\work\Locohane_Agent\mcp_server\server.py"
```

**方法2: user scope** — `~/.claude.json`のトップレベル`mcpServers`に
登録され、このマシン上のこのユーザーであればどのプロジェクトからでも
使える。`.mcp.json`としてリポジトリには残らないため、他マシン・
他ユーザーとは共有されない。

```
claude mcp add --transport stdio --scope user locohane-skills -- "C:\work\venv_locohane\Scripts\python.exe" "C:\work\Locohane_Agent\mcp_server\server.py"
```

どちらの方法でも、混乱を避けるため元のlocalスコープ登録は削除しておくとよい:

```
claude mcp remove locohane-skills -s local
```

#### VSCode拡張版

**VSCode拡張版には`claude mcp add`のようなCLIコマンドは無い。** 設定ファイルを
エディタ等で直接手動作成・編集して登録する。

**方法1: project scope（推奨）** — プロジェクトルート
（`c:\work\Locohane_Agent`）に`.mcp.json`を手動で作成し、以下の内容を書く。
CLI版・VSCode拡張版どちらからも確実に認識される。`.mcp.json.example`を
`.mcp.json`にコピーしてパスを直してもよい（`.mcp.json`はこのマシンのフルパスを
含むためgit管理外）。

```jsonc
{
  "mcpServers": {
    "locohane-skills": {
      "type": "stdio",
      "command": "C:\\work\\venv_locohane\\Scripts\\python.exe",
      "args": ["C:\\work\\Locohane_Agent\\mcp_server\\server.py"]
    }
  }
}
```

**方法2: user scope** — `~/.claude.json`（Windowsでは
`%USERPROFILE%\.claude.json`）をエディタで開き、トップレベルの
`mcpServers`オブジェクトに同じ内容を手動で追記する（既にファイルが存在し
`projects`等の他キーがある場合は、それらは残したまま`mcpServers`キーだけを
追加・マージする）。このマシン上のこのユーザーであればどのプロジェクトからでも
使える。`.mcp.json`としてリポジトリには残らないため、他マシン・他ユーザーとは
共有されない。

```jsonc
{
  "mcpServers": {
    "locohane-skills": {
      "type": "stdio",
      "command": "C:\\work\\venv_locohane\\Scripts\\python.exe",
      "args": ["C:\\work\\Locohane_Agent\\mcp_server\\server.py"]
    }
  }
}
```

どちらの方法でも、混乱を避けるため元のlocalスコープ登録は（CLI版で
`claude mcp remove locohane-skills -s local`を実行して）削除しておくとよい。

**補足（既知の問題）**: CLI版で（scopeオプション無しの既定＝localスコープで）
登録した場合、`~/.claude.json`の`projects`オブジェクトへ現在の作業ディレクトリの
絶対パス文字列をキーとして保存される。実際に確認された問題として、CLI版で
登録した際のキーが`C:/work/Locohane_Agent`（大文字ドライブレター＋スラッシュ
区切り）で保存される一方、VSCode拡張機能側が使う作業ディレクトリの表記が
`c:\work\Locohane_Agent`（小文字＋バックスラッシュ区切り）等になっていると、
文字列として一致せず別プロジェクト扱いになり、VSCode拡張側では
`locohane-skills`が一切認識されない（`/mcp`のツール一覧に出てこない）。
CLI版のlocalスコープ登録だけで済ませず、上記の方法1・方法2のいずれかで
設定ファイルを直接作成しておくのはこのためである。

#### 環境変数の設定（`.mcp.json`の`env`フィールド）

3節の表で挙げた`config.py`の5つの環境変数は、`.mcp.json`（project scope）の
`mcpServers.locohane-skills.env`フィールドに直接書ける。`command`/`args`と
同じ階層に`env`オブジェクトを追加するだけで、サブプロセス起動時の環境変数
として渡される。

```jsonc
{
  "mcpServers": {
    "locohane-skills": {
      "type": "stdio",
      "command": "C:\\work\\venv_locohane\\Scripts\\python.exe",
      "args": ["C:\\work\\Locohane_Agent\\mcp_server\\server.py"],
      "env": {
        "LOCOHANE_MCP_SKILLS_SRC": "",
        "LOCOHANE_MCP_SKILLS_WORKDIR": "",
        "LOCOHANE_MCP_SKILLS_SCRIPT_TIMEOUT_SECONDS": "",
        "LOCOHANE_MCP_SKILLS_PYTHON": "",
        "LOCOHANE_MCP_SKILLS_NAME": ""
      }
    }
  }
}
```

空文字のまま（または未設定）であれば`config.py`側の`os.environ.get(...)`が
偽値と判定して既定値にフォールバックするため、上書きが不要な項目は空文字の
ままでよい。値を指定する場合の例:

```jsonc
"env": {
  "LOCOHANE_MCP_SKILLS_SRC": "C:\\work\\Locohane_Agent\\skills;C:\\other\\skills",
  "LOCOHANE_MCP_SKILLS_WORKDIR": "C:\\work\\Locohane_Agent\\output",
  "LOCOHANE_MCP_SKILLS_SCRIPT_TIMEOUT_SECONDS": "600",
  "LOCOHANE_MCP_SKILLS_PYTHON": "C:\\work\\venv_locohane\\Scripts\\python.exe",
  "LOCOHANE_MCP_SKILLS_NAME": "locohane-skills-dev"
}
```

`LOCOHANE_MCP_SKILLS_SRC`は複数パス指定時`os.pathsep`（Windowsでは`;`）区切り。
user scope登録（`~/.claude.json`）やClaude Desktopの`claude_desktop_config.json`
でも同様に`env`フィールドが使える。

### Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json` に追記:

```jsonc
{
  "mcpServers": {
    "locohane-skills": {
      "command": "C:\\work\\venv_locohane\\Scripts\\python.exe",
      "args": ["C:\\work\\Locohane_Agent\\mcp_server\\server.py"]
    }
  }
}
```

設定後はClaude Desktopを完全に再起動する。

### プログラムから直接取得する場合

```python
from pathlib import Path
from fastmcp import Client
from fastmcp.utilities.skills import list_skills, sync_skills
from mcp_server.server import mcp

async with Client(mcp) as client:
    skills = await list_skills(client)          # -> 20件（office_shared共通モジュール置き場はSKILL.mdが無く対象外）
    await sync_skills(client, Path("./downloaded_skills"))
```

**Windowsでの既知の制約（実際に発生・回避確認済み）**: `sync_skills`/`download_skill`
（`fastmcp/utilities/skills.py`）は内部で`file_path.write_text(content.text)`を
encoding指定なしで呼んでおり、Windows既定のcp932では日本語（Locohaneの
SKILL.mdは全て日本語）を含むファイルの保存時に`UnicodeEncodeError`で
落ちる。回避するには実行前に`PYTHONUTF8=1`環境変数を設定する:

```
set PYTHONUTF8=1
python your_sync_script.py
```

### 4-1. 接続後の使い方（Claude Codeの会話で実際に呼び出す）

`claude mcp add`で登録し`/mcp`で`Connected`を確認できたら、**特別な構文は
不要**。通常の会話でスキルに関係する頼み事をするだけで、Claude Code側の
LLMが必要なツールを自律的に選んで呼び出す（`list_skills`→`read_skill`→
`run_skill_script`の順で連鎖的に呼ばれることが多い）。

例えば以下のように頼むだけでよい:

```
locohane-skillsで使えるスキル一覧を見せて
```

```
pdf-toolsスキルを使って sample.pdf の1〜3ページを画像化して
```

このとき裏側で起きているのは:

1. Claude Codeが`list_skills`を呼び、スキル一覧（`name: description`）を取得
2. 該当しそうなスキル（例: `pdf-tools`）が見つかれば`read_skill("pdf-tools")`で
   SKILL.md本文を読み、`scripts/`配下のどのファイルをどんな引数で呼ぶべきか把握
3. 実行が必要なら`run_skill_script("pdf-tools", "render_pdf_pages.py", [...])`を
   呼び、結果（終了コード・標準出力・標準エラー）を受け取って会話に反映

ツール名を明示したい場合は`mcp__locohane-skills__list_skills`のように
完全修飾名で言及すると意図が伝わりやすい（`/mcp`のツール一覧に表示される
名前と対応する）。

Resources（`skill://...`）側は「実行」ではなく「読む」ための経路なので、
Claude Codeの会話中で`@`によるリソースメンション（`/mcp`のresources一覧
から選択、またはリソースURIを直接貼り付け）でSKILL.mdや補助ファイルの
中身をコンテキストに含めることができる。ただしこれはテキストを読み込む
だけで、`officecli`等の外部CLIを伴うスクリプトはこの経路では実行されない
（6節）。実行が必要な場面ではTools方式（`run_skill_script`）が使われる。

---

## 5. 既存のMCPサーバー接続（クライアント側）との違い

| | クライアント側（既存, `src/mcp_client.py`） | サーバー側（本ドキュメント） |
|---|---|---|
| 役割 | 外部MCPサーバーのツールをLocohaneが**使う** | Locohaneのスキルを外部が**使う** |
| 設定ファイル | `.locohane/settings.json` | なし（`mcp_server/`パッケージが実体） |
| 実装状況 | 実装済み（未検証） | 実装済み・動作確認済み（`mcp_server/`、114リソース公開・`.env`非公開・SKILL.md読込・3ツール実行を確認） |

両者は独立しており、どちらか一方を実装してももう一方には影響しない。

---

## 6. 制約・注意点

- **Resources経由（`skill://...`）で配られるのはテキスト（SKILL.mdおよび
  `supporting_files="resources"`時は`scripts/`配下の`.py`ソースコードを含む
  全ファイル）であり、実行環境そのものではない**。`officecli`のような外部
  CLIツール本体は配らない。受け取った側が自前でPython・`officecli.exe`等の
  実行環境を用意できていなければ、Resources経由で受け取ったスクリプトの
  ソースコードだけでは実行できない（この制約はResources方式のみに適用され、
  `run_skill_script`はMCPサーバー側の実行環境をそのまま使うためこの制約を受けない）。
- Locohaneのスキルの多くは「`python <script>.py <args...>` の形式で呼び出す」
  という前提でSKILL.mdが書かれている（`skills/SKILLS_README.md` 4-0節）。これは
  可搬性を意識した設計であり、`supporting_files="resources"`で`scripts/`一式が
  丸ごと配られるため、受け取り側は取得したファイルをそのまま同じ相対配置で
  保存すれば動く可能性が高い。
- `excel-vba-edit`のようにWindows COM（Excel本体）に依存するスキルは、
  受け取り側もWindows＋Office環境でなければ実行できない。
- `web-search`スキルの`scripts/.env`（TAVILY_API_KEY）は`mcp_server/config.py`の
  `EXCLUDE_NAMES`でResources配布対象から除外済み（3-2節、動作確認済み）。
  Resources経由の受け取り側は`web-search`スキル自体は使えるが、TAVILY_API_KEYは
  別途自分で用意する必要がある（`run_skill_script`経由ならMCPサーバー側の
  `.env`がそのまま使われるためこの制約はない）。新しいスキルに`.env`系ファイルを
  追加した場合は`EXCLUDE_NAMES`への追記を忘れないこと。
- `sync_skills`/`download_skill`（FastMCP側のクライアントユーティリティ）は
  Windows環境で日本語ファイルの保存時に`UnicodeEncodeError`になる既知の制約が
  ある（4節参照、`PYTHONUTF8=1`で回避）。自前で`client.read_resource()`を呼んで
  `encoding="utf-8"`を明示して保存する場合はこの制約を受けない。

---

## 7. 参考

- [Model Context Protocol 公式仕様](https://modelcontextprotocol.io/specification)
- [FastMCP 公式ドキュメント](https://gofastmcp.com/)
- [FastMCP Skills Provider](https://gofastmcp.com/servers/providers/skills)
- [FastMCP ソースコード（`fastmcp/server/providers/skills/`、v3.4.7）](https://github.com/PrefectHQ/fastmcp) — `pip download fastmcp-slim==3.4.7`で取得し実装詳細を直接確認した
- [Agent Skills specification](https://agentskills.io/specification)
- [Claude Code: Connect to MCP servers](https://code.claude.com/docs/en/mcp)
