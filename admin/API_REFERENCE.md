# APIリファレンス（curl で管理APIを直接操作する）

設定ダッシュボードの画面操作は、すべて以下のHTTP APIで同じことができる。
スクリプトからの一括操作や自動化に使う。

> このファイル（`admin/API_REFERENCE.md`）はダッシュボードの「APIリファレンス」
> メニューにそのまま表示される。ファイルを編集すれば、次に画面を開いたときに
> 反映される（管理ツールの再起動は不要）。表示言語が英語のときは英訳の
> `admin/API_REFERENCE.en.md` を表示する。こちらが正本で、更新したら英訳も合わせて直す。

## 共通ルール

- ベースURL: `http://127.0.0.1:8001`（`[admin].host`/`port`、または `admin.bat` の `ADMIN_HOST`/`ADMIN_PORT`）
- **認証**: `POST /api/login` で受け取るCookie（`locohane_admin_session`）を以後のリクエストに付ける。
  Cookieの有効期限はリクエストのたびに延長される（`[admin].session_timeout_minutes`）。
- **CSRFヘッダ**: GET以外（POST/PUT/DELETE）には必ず `X-Locohane-Admin: 1` を付ける。
  無いと `403` になる。
- リクエストボディはJSON（`Content-Type: application/json`）。
- エラー時は `{"detail": "エラー内容"}` が返る。
- **言語**: `X-Locohane-Lang: en` を付けると、エラー内容（`detail`）と管理アシスタントの回答が英語になる
  （無ければ日本語。訳の無いメッセージは日本語のまま）。表示言語の既定値（`[ui].default_language`）は
  認証なしの `GET /ui-config.js` が `window.LOCOHANE_DEFAULT_LANGUAGE = "auto";` の形で返す。

| ステータス | 意味 |
|---|---|
| 400 | 入力値が不正（バリデーション失敗）。本文が UTF-8 のJSONとして読めない場合も `There was an error parsing the body` で400になる（cmd.exe で日本語を直接渡した場合など。下記参照） |
| 401 | 未ログイン・セッション切れ |
| 403 | CSRFヘッダ無し、または変更禁止の対象 |
| 404 | インスタンス・ユーザーが存在しない |
| 409 | 状態の競合（稼働中で削除できない、他セッションが先に保存した等） |
| 422 | 必須項目の欠落・型違い |
| 429 | ログイン失敗が続きロックアウト中 |

## 準備（変数の定義）

各例は **bash**（Git Bash 等）と **cmd**（Windows の cmd.exe）の両方を載せている。
最初に以下の変数を定義しておき、以後の例で使い回す。

bash:

```bash
BASE=http://127.0.0.1:8001
C="curl -s -b cookies.txt -c cookies.txt"
H='X-Locohane-Admin: 1'
J='Content-Type: application/json'
```

cmd:

```bat
set BASE=http://127.0.0.1:8001
set C=curl.exe -s -b cookies.txt -c cookies.txt
set H="X-Locohane-Admin: 1"
set J="Content-Type: application/json"
```

### cmd.exe での注意

- `curl` ではなく `curl.exe` と書く（Windows 10 以降は標準搭載）。
- JSON は全体を `"` で囲み、中の `"` を `\"` にする（例: `-d "{\"key\":\"value\"}"`）。
  シングルクォート `'` は使えない。
- URL に `&` を含む場合は URL 全体を `"` で囲む。
- 行の途中で改行したい場合は行末に `^` を付ける。
- **日本語など非ASCII文字を含むJSONは、コマンドラインに直接書かない。**
  cmd.exe から curl.exe へは cp932（Shift_JIS）のまま渡り、`chcp 65001` をしても
  サーバー側で UTF-8 として読めずエラーになる。UTF-8（BOMなし）で保存した
  ファイルを `-d @ファイル名` で渡すこと（下記の各例を参照）。
- `.bat` ファイルに書く場合、`for /f` の変数は `%i` ではなく `%%i` と書く。

## ログイン・ログアウト

### ログイン

ユーザーはプロジェクト直下 `.env` の `ADMIN_USERS`。

bash:

```bash
$C -X POST $BASE/api/login -H "$J" \
  -d '{"username":"admin","password":"パスワード"}'
# => {"username":"admin","version":"0.2.0"}
```

cmd（パスワードがASCIIのみの場合）:

```bat
%C% -X POST %BASE%/api/login -H %J% -d "{\"username\":\"admin\",\"password\":\"password\"}"
```

cmd（パスワードに日本語を含む場合は、UTF-8で保存した `login.json` を渡す）:

```bat
%C% -X POST %BASE%/api/login -H %J% -d @login.json
```

### ログイン中のユーザー・バージョン確認

```bash
$C $BASE/api/me
# => {"username":"admin","version":"0.2.0"}
```

```bat
%C% %BASE%/api/me
```

### ログアウト

```bash
$C -X POST $BASE/api/logout -H "$H"
```

```bat
%C% -X POST %BASE%/api/logout -H %H%
```

## インスタンス

### 一覧

```bash
$C $BASE/api/instances
```

```bat
%C% %BASE%/api/instances
```

各要素の主なフィールド: `name`, `display_name`, `app_host`, `app_port`,
`autostart`, `headless`, `watch`, `state`（`running`/`stopped`/`external`/`crashed`）,
`pid`, `url`, `is_default`, `kind`（`app`＝Locohane 本体／`skill_lab`＝スキル研究室）。

### 作成

`name` 以外は省略可。`app_port` 省略時は空きポートを自動で選ぶ。
`copy_from` に既存インスタンス名を指定すると設定を複製する。
`kind` に `skill_lab` を指定するとスキル研究室（チャット画面・ポートを持たないワーカー）を作る（既定 `app`）。
`copy_extensions: true` なら、複製元のインスタンス専用の拡張ディレクトリ（研究室で昇格したスキル・サブエージェント）も複製する。
応答は一覧の1件と同じ項目に `skipped_settings`（複製元から引き継がなかった設定とその理由の文言のリスト。`${instance}` を含まない固定パスの
`[paths] common_data_dir`、作るインスタンスの種類では使わない設定等。無ければ空）を足したもの。

bash:

```bash
$C -X POST $BASE/api/instances -H "$H" -H "$J" -d '{
  "name": "test2",
  "display_name": "検証用",
  "app_host": "127.0.0.1",
  "app_port": 8002,
  "autostart": false,
  "headless": true,
  "watch": false,
  "copy_from": "default"
}'
```

cmd（表示名が日本語なので、上の JSON を UTF-8 で `instance.json` に保存して渡す）:

```bat
%C% -X POST %BASE%/api/instances -H %H% -H %J% -d @instance.json
```

cmd（ASCIIのみなら直接書ける）:

```bat
%C% -X POST %BASE%/api/instances -H %H% -H %J% -d "{\"name\":\"test2\",\"app_port\":8002,\"copy_from\":\"default\"}"
```

### 設定変更（表示名・ホスト・ポート等）

変更したい項目だけ送る。稼働中のホスト・ポート変更は再起動後に反映。

```bash
$C -X PUT $BASE/api/instances/test2 -H "$H" -H "$J" \
  -d '{"display_name":"検証用2","autostart":true}'
```

```bat
%C% -X PUT %BASE%/api/instances/test2 -H %H% -H %J% -d "{\"app_port\":8003,\"autostart\":true}"
```

### 削除

稼働中は `409`。先に停止する。`default` は削除できない。

ボディ省略時は `instances/<name>/` だけを削除し、永続データ（ログ・スレッド・
default_workdir 等）は残す。一緒に消したいデータは、まず削除候補を取得し、
`deletable: true` の項目の `key` を `delete_data` に指定する
（`deletable: false` の項目を指定すると `400`、何も削除しない）。

```bash
# 削除候補の一覧（key/label/path/exists/deletable/reason/default_selected）
$C $BASE/api/instances/test2/data-paths
# インスタンスのみ削除（データは残す）
$C -X DELETE $BASE/api/instances/test2 -H "$H"
# データディレクトリ全体も一緒に削除
$C -X DELETE $BASE/api/instances/test2 -H "$H" -H "$J" -d '{"delete_data":["common_data_dir"]}'
# => {"success":true,"deleted_data":["C:\\work\\Locohane_Agent\\data\\test2"]}
```

```bat
%C% %BASE%/api/instances/test2/data-paths
%C% -X DELETE %BASE%/api/instances/test2 -H %H%
%C% -X DELETE %BASE%/api/instances/test2 -H %H% -H %J% -d "{\"delete_data\":[\"log_dir\",\"default_workdir\"]}"
```

### 起動・停止・再起動

```bash
$C -X POST $BASE/api/instances/default/start   -H "$H"
$C -X POST $BASE/api/instances/default/stop    -H "$H"
$C -X POST $BASE/api/instances/default/restart -H "$H"
# => {"state":"running","pid":12345}
```

```bat
%C% -X POST %BASE%/api/instances/default/start   -H %H%
%C% -X POST %BASE%/api/instances/default/stop    -H %H%
%C% -X POST %BASE%/api/instances/default/restart -H %H%
```

## config.ini の上書き設定

管理ツールは config.ini 自体を書き換えず、
`instances/<name>/config_overrides.json` に差分を保存する。
`[admin]` セクションは変更できない。

### 現在値の取得

```bash
$C $BASE/api/instances/default/config
```

```bat
%C% %BASE%/api/instances/default/config
```

`keys` に全キーの `section`, `key`, `default`（config.iniの値）,
`override`（上書き値。無ければ null）, `effective`（実効値）,
`env_override_active`（環境変数で上書き中か）等が入る。
`mtime` は保存時の競合チェックに使う（下記）。

`keys` に入るのは、そのインスタンスの種類で効くキーだけ。スキル調整ワーカーは `[llm]` の接続先
（`main_url`・`sub_url`・`main_routing_strategy`・`sub_routing_strategy`）・`[skill_lab_worker]`・`[log]` だけで、
本体のインスタンスには `[skill_lab_worker]` が入らない。`scope_note` はワーカーのときだけ説明文が入る（本体は null）。
出していないキーを `updates` に入れると、事前確認・保存とも `400`。

### スキル名・agent_type の候補の取得

```bash
$C $BASE/api/instances/default/config/candidates
```

```bat
%C% %BASE%/api/instances/default/config/candidates
```

保存済みの設定でそのインスタンスが認識するスキル・サブエージェント（同梱の `skills/`・`agents/`、
`[paths] project_locohane_dir`、インスタンス専用の拡張ディレクトリ）を返す。設定画面で
`[subagent] agent_type_run_script_allowlist` 等のスキル名・スクリプトファイル名・agent_type・ツール名を選ぶ欄の候補に使う。

- `skills`: `name`, `description`, `origin`（`builtin` / `shared` / `instance`）, `scripts`（`scripts/*.py` のファイル名）
- `agent_types`: `name`, `description`, `origin`
- `tools`: メインエージェントのビルトインツール名（`[main_agent_tool_guard] allow_entries` のツール名の候補。MCP の動的ツールは含まない。取得できなければ空）
- `error`: 設定が読めず一覧を作れなかったときの理由（このとき `skills`・`agent_types` は空）。通常は null

### 変更内容の事前確認（保存しない）

```bash
$C -X POST $BASE/api/instances/default/config/preview -H "$J" -d '{
  "updates": {"llm": {"temperature": "0.3"}},
  "resets": [["subagent", "max_iterations"]]
}'
```

```bat
%C% -X POST %BASE%/api/instances/default/config/preview -H %J% -d "{\"updates\":{\"llm\":{\"temperature\":\"0.3\"}},\"resets\":[[\"subagent\",\"max_iterations\"]]}"
```

- `updates`: `{"セクション": {"キー": "config.iniに書くのと同じ形式の文字列"}}`
- `resets`: 上書きを消して config.ini の値に戻すキーの `[セクション, キー]` の配列

### 保存

`base_mtime` には直前に取得した `mtime` をそのまま入れる
（一度も保存していないインスタンスでは `null`）。他で先に保存されていると `409`。
以下の例では `mtime` の取り出しに Python を使っている。

bash:

```bash
MTIME=$($C $BASE/api/instances/default/config | python -c "import sys,json; print(json.dumps(json.load(sys.stdin)['mtime']))")
$C -X PUT $BASE/api/instances/default/config -H "$H" -H "$J" -d "{
  \"updates\": {\"llm\": {\"temperature\": \"0.3\"}},
  \"resets\": [],
  \"base_mtime\": $MTIME
}"
# => {"overrides":{...},"needs_restart":true,"mtime":...}
```

cmd（`.bat` に書く場合は `%i` を `%%i` にする）:

```bat
for /f "delims=" %i in ('%C% %BASE%/api/instances/default/config ^| python -c "import sys,json; print(json.dumps(json.load(sys.stdin)['mtime']))"') do set MTIME=%i
%C% -X PUT %BASE%/api/instances/default/config -H %H% -H %J% -d "{\"updates\":{\"llm\":{\"temperature\":\"0.3\"}},\"resets\":[],\"base_mtime\":%MTIME%}"
```

`needs_restart` が `true` なら、反映にはインスタンスの再起動が必要。

### バックアップ一覧・復元

```bash
$C $BASE/api/instances/default/backups
$C -X POST $BASE/api/instances/default/backups/config_overrides_20260930_120000.json/restore -H "$H"
```

```bat
%C% %BASE%/api/instances/default/backups
%C% -X POST %BASE%/api/instances/default/backups/config_overrides_20260930_120000.json/restore -H %H%
```

### 他のインスタンスから設定をコピー

`copy-preview` で、コピー元（`source`）と値が違う設定（`changes`。`new` が `null` のキーは既定値に戻る）・写さない設定（`skipped`）・
コピー先の `mtime` を返す。`copy` で写す（`keys` に `[section, key]` を並べるとそのキーだけ、省略すると `changes` を全部）。
`base_mtime` には `copy-preview` の `mtime` を渡す（他のセッションが先に保存していれば 409）。`[paths] common_data_dir`・
コピー先で効かない設定はコピー先の値のまま。保存前の内容はバックアップされ、変更履歴には `config_copy` で残る。

```bash
$C -X POST $BASE/api/instances/test2/config/copy-preview -H "$J" -d '{"source":"default"}'
$C -X POST $BASE/api/instances/test2/config/copy -H "$H" -H "$J" -d '{"source":"default","keys":[["llm","temperature"]],"base_mtime":1759200000.0}'
```

```bat
%C% -X POST %BASE%/api/instances/test2/config/copy-preview -H %J% -d "{\"source\":\"default\"}"
%C% -X POST %BASE%/api/instances/test2/config/copy -H %H% -H %J% -d "{\"source\":\"default\",\"keys\":[[\"llm\",\"temperature\"]],\"base_mtime\":1759200000.0}"
```

## ログインユーザー（Locohane本体）

`instances/<name>/.env` の `AUTH_USERS` を操作する。
インスタンス側に未設定の間はプロジェクト直下 `.env` のユーザーを継承しており
（`inherited_from_project_env: true`）、継承中のユーザーは変更・削除できない（`403`）。

bash:

```bash
# 一覧
$C $BASE/api/instances/default/users
# 追加
$C -X POST $BASE/api/instances/default/users -H "$H" -H "$J" \
  -d '{"username":"alice","password":"secret"}'
# パスワード変更
$C -X PUT $BASE/api/instances/default/users/alice -H "$H" -H "$J" \
  -d '{"password":"new-secret"}'
# メモリーの優先順位に使うランク（[memory] user_ranks へ保存。null で外す。継承中のユーザーにも設定できる）
$C -X PUT $BASE/api/instances/default/users/alice/rank -H "$H" -H "$J" -d '{"rank":3}'
# 削除
$C -X DELETE $BASE/api/instances/default/users/alice -H "$H"
# まとめて削除（1人でも削除できなければ何も削除しない）
$C -X POST $BASE/api/instances/default/users/delete -H "$H" -H "$J" \
  -d '{"usernames":["alice","bob"]}'
# JWT署名鍵（CHAINLIT_AUTH_SECRET）の再生成
$C -X POST $BASE/api/instances/default/auth-secret -H "$H"
```

cmd:

```bat
rem 一覧
%C% %BASE%/api/instances/default/users
rem 追加
%C% -X POST %BASE%/api/instances/default/users -H %H% -H %J% -d "{\"username\":\"alice\",\"password\":\"secret\"}"
rem パスワード変更
%C% -X PUT %BASE%/api/instances/default/users/alice -H %H% -H %J% -d "{\"password\":\"new-secret\"}"
rem メモリーの優先順位に使うランク（null で外す）
%C% -X PUT %BASE%/api/instances/default/users/alice/rank -H %H% -H %J% -d "{\"rank\":3}"
rem 削除
%C% -X DELETE %BASE%/api/instances/default/users/alice -H %H%
rem まとめて削除（1人でも削除できなければ何も削除しない）
%C% -X POST %BASE%/api/instances/default/users/delete -H %H% -H %J% -d "{\"usernames\":[\"alice\",\"bob\"]}"
rem JWT署名鍵（CHAINLIT_AUTH_SECRET）の再生成
%C% -X POST %BASE%/api/instances/default/auth-secret -H %H%
```

## 環境変数（インスタンスの .env）

`AUTH_USERS`/`CHAINLIT_AUTH_SECRET` 以外の任意の環境変数。
一覧ではパスワード・キー等の値はマスクされる。

bash:

```bash
# 一覧
$C $BASE/api/instances/default/env
# 追加・更新
$C -X PUT $BASE/api/instances/default/env -H "$H" -H "$J" \
  -d '{"key":"MY_VAR","value":"hello"}'
# 削除
$C -X DELETE $BASE/api/instances/default/env/MY_VAR -H "$H"
```

cmd:

```bat
rem 一覧
%C% %BASE%/api/instances/default/env
rem 追加・更新
%C% -X PUT %BASE%/api/instances/default/env -H %H% -H %J% -d "{\"key\":\"MY_VAR\",\"value\":\"hello\"}"
rem 削除
%C% -X DELETE %BASE%/api/instances/default/env/MY_VAR -H %H%
```

## メモリー（インスタンスの永続メモリー）

そのインスタンスの実効設定の `[paths] memory_dir` 配下のメモリーを読み書きする。メモリーはユーザー別で、
所有者はクエリ `owner`（ログインユーザー名。本体と同じ規則でフォルダ名へ整える）で指定する。省略・空なら共通の
メモリー（ユーザー別になる前からあるもの）。管理 API では公開範囲に関係なくすべて読み書きできる。
`type` は `user`/`feedback`/`project`/`reference`、`visibility` は `private`/`shared`（ユーザーのメモリーだけ。
省略時は作成なら `shared`、編集なら今の値のまま。そのインスタンスの `[memory]` で `private` にした種類と、有効にした目印に
当たるものは `private` になる）。
編集では `name`・所有者は変えられない（元ファイルの記録 `sources` は残る）。
編集・削除の前の内容は `instances/<name>/backups/memory/<所有者（共通は _common）>/<メモリー名>/` に残る。
翻訳は、説明と本文を `X-Locohane-Lang`（`ja`/`en`。無ければ `ja`）の言語へ LLM に訳させて返すだけで、保存しない
（LLM は「管理者設定」の「回答に使う LLM」、空なら対象インスタンス自身の `[llm]` 接続先）。

bash:

```bash
# 一覧（memory_dir・types・visibilities・owners・memories[]: name/description/type/owner（共通は null）/visibility/mtime/sources（元ファイルの数））
$C $BASE/api/instances/default/memories
# 1件（content と、元ファイルごとの照合結果 sources[]: path/state）
$C "$BASE/api/instances/default/memories/my-note?owner=alice"
# 作成（同じ所有者に同じ名前があれば 409。owner を省くと共通。応答に実際の owner・visibility）
$C -X POST $BASE/api/instances/default/memories -H "$H" -H "$J" \
  -d '{"name":"my-note","description":"一行の説明","type":"feedback","content":"本文","owner":"alice","visibility":"shared"}'
# 編集（応答に実際の visibility）
$C -X PUT "$BASE/api/instances/default/memories/my-note?owner=alice" -H "$H" -H "$J" \
  -d '{"description":"一行の説明","type":"feedback","content":"新しい本文","visibility":"private"}'
# 削除
$C -X DELETE "$BASE/api/instances/default/memories/my-note?owner=alice" -H "$H"
# まとめて削除（1件でも見つからなければ何も削除しない。owner を省くと共通）
$C -X POST $BASE/api/instances/default/memories/delete -H "$H" -H "$J" \
  -d '{"items":[{"name":"my-note","owner":"alice"},{"name":"old-note"}]}'
# 翻訳（英語へ。保存はしない。応答は description/content/language/llm_instance）
$C -X POST $BASE/api/instances/default/memories/translate -H "$H" -H "$J" -H "X-Locohane-Lang: en" \
  -d '{"description":"一行の説明","content":"本文"}'
# [memory] shared_criteria / private_criteria の清書（kind は shared / private。X-Locohane-Lang の言語で清書する。
# 保存はしない。応答は text/language/llm_instance）
$C -X POST $BASE/api/instances/default/memories/polish-criteria -H "$H" -H "$J" \
  -d '{"kind":"private","text":"本人・家族のこと"}'
# ドラフトスキルの作成先ユーザーの候補（owners・default（anonymous）・draft_dir・other_users_drafts）
$C $BASE/api/instances/default/memories/draft-skill-owners
# 選んだメモリーからドラフトスキルを作成（LLM が SKILL.md を下書きし、[skill_creator] draft_dir/<owner>/<skill_name>/ へ保存。
# owner を省くと anonymous、skill_name を省くと LLM が決める。最大20件。X-Locohane-Lang の言語で書く。
# 応答は owner/skill_name/description/path/content/private_memories_of_others/llm_instance）
$C -X POST $BASE/api/instances/default/memories/draft-skill -H "$H" -H "$J" \
  -d '{"items":[{"name":"my-note","owner":"alice"},{"name":"old-note"}],"owner":"alice","skill_name":"monthly-check","request":"月次の確認用"}'
```

cmd:

```bat
rem 一覧
%C% %BASE%/api/instances/default/memories
rem 編集
%C% -X PUT "%BASE%/api/instances/default/memories/my-note?owner=alice" -H %H% -H %J% -d "{\"description\":\"desc\",\"type\":\"feedback\",\"content\":\"text\",\"visibility\":\"shared\"}"
rem 削除
%C% -X DELETE "%BASE%/api/instances/default/memories/my-note?owner=alice" -H %H%
```

## 表示設定（ヘッダー・タブタイトル・ウェルカムメッセージ・アイコン）

`?instance=<name>` を付けるとそのインスタンス専用の設定、
付けないと全インスタンス共通（`public/settings/`）が対象。

テキストのファイル名は `header.md` / `tab_title.md` / `welcome.md`、
画像の種別は `icon` / `favicon`。

bash:

```bash
# 実効値の取得
$C "$BASE/api/settings?instance=default"

# テキストの保存（共通）
$C -X PUT $BASE/api/settings/header.md -H "$H" -H "$J" \
  -d '{"content":"社内AIアシスタント"}'

# テキストの保存（インスタンス専用）
$C -X PUT "$BASE/api/settings/welcome.md?instance=default" -H "$H" -H "$J" \
  -d '{"content":"ようこそ。使えるスキル:\n{skills}"}'

# インスタンス専用テキストを削除して共通に戻す（instance 必須）
$C -X DELETE "$BASE/api/settings/welcome.md?instance=default" -H "$H"

# 画像のアップロード（base64で送る）
B64=$(base64 -w0 logo.png)
$C -X PUT "$BASE/api/settings/images/icon?instance=default" -H "$H" -H "$J" \
  -d "{\"filename\":\"logo.png\",\"content_base64\":\"$B64\"}"

# インスタンス専用画像を削除して共通に戻す（instance 必須）
$C -X DELETE "$BASE/api/settings/images/icon?instance=default" -H "$H"
```

cmd（テキストは日本語を含むことが多いので、`{"content":"..."}` を UTF-8 で
`header.json` 等に保存して渡す。画像は PowerShell で base64 化した JSON を作って渡す）:

```bat
rem 実効値の取得
%C% "%BASE%/api/settings?instance=default"

rem テキストの保存（共通）
%C% -X PUT %BASE%/api/settings/header.md -H %H% -H %J% -d @header.json

rem テキストの保存（インスタンス専用）
%C% -X PUT "%BASE%/api/settings/welcome.md?instance=default" -H %H% -H %J% -d @welcome.json

rem インスタンス専用テキストを削除して共通に戻す（instance 必須）
%C% -X DELETE "%BASE%/api/settings/welcome.md?instance=default" -H %H%

rem 画像のアップロード（logo.png を base64 にした JSON を body.json に作ってから送る）
powershell -NoProfile -Command "@{filename='logo.png'; content_base64=[Convert]::ToBase64String([IO.File]::ReadAllBytes('logo.png'))} | ConvertTo-Json | Set-Content -Encoding ascii body.json"
%C% -X PUT "%BASE%/api/settings/images/icon?instance=default" -H %H% -H %J% -d @body.json

rem インスタンス専用画像を削除して共通に戻す（instance 必須）
%C% -X DELETE "%BASE%/api/settings/images/icon?instance=default" -H %H%
```

## モニター（稼働状況・会話閲覧・トークン推移・ログ・LLM接続先）

すべて読み取り専用（GET）。`<name>` はインスタンス名。会話内容の取得
（`/monitor/threads/<thread_id>`）だけは、閲覧した事実を変更履歴へ
`conversation_view` として記録する。

```bash
# 全インスタンスの接続中ユーザー・セッション数・生成中スレッド数。context_series に
# 生成中スレッドごとの今回の生成開始以降のLLMリクエストの入力トークン
# （points[].value はメイン直近とその後のサブ・圧縮処理の最大値）
$C "$BASE/api/monitor/overview"
# 1インスタンスの詳細（接続中セッション・生成中スレッド・直近24時間の警告件数）
$C "$BASE/api/instances/default/monitor/runtime"
# ユーザー別のスレッド数・トークン累計・最終利用日時
$C "$BASE/api/instances/default/monitor/users"
# スレッド一覧（owner・q（名前/IDの部分一致）・limit・offset で絞り込み）
$C "$BASE/api/instances/default/monitor/threads?owner=alice&limit=50"
# 会話内容（internal=true でツール実行・思考・UI制御メッセージも含める）
$C "$BASE/api/instances/default/monitor/threads/<thread_id>?internal=false"
# LLM呼び出しごとのトークン使用量（アプリログから復元）
$C "$BASE/api/instances/default/monitor/threads/<thread_id>/tokens"
# アプリログ（level 以上を新しい順に。q・thread_id で絞り込み）
$C "$BASE/api/instances/default/monitor/logs?level=WARNING&limit=200"
# LLM接続先の到達確認（llama_cpp はスロット使用状況も。/slots が無効なサーバーは
# /health で到達を確認し、reachable=true のまま error にその旨が入る）
$C "$BASE/api/instances/default/monitor/endpoints"
```

```bat
%C% "%BASE%/api/instances/default/monitor/runtime"
%C% "%BASE%/api/instances/default/monitor/threads/<thread_id>/tokens"
```

## 利用量・料金

月別・ユーザー別のトークン使用量と、手入力した AWS 利用料金からの金額（README_DETAIL.md「利用量・料金」節）。

```bash
# 全インスタンス（スキル調整ワーカーを除く）の月別合計。months[].instances / months[].users に内訳、
# cost_incomplete / jpy_incomplete に料金・為替レートが未入力のインスタンス名、errors に読めなかったインスタンス
$C "$BASE/api/usage/overview"
# 月別の使用量（usage）・料金設定（settings）・計算した金額（costs）
$C "$BASE/api/instances/default/usage"
# 料金設定の保存（pricing_model は monthly_bill / hourly / per_token。数値は空欄=未入力。
# months は "YYYY-MM" -> {"bill": 請求額, "hours": 稼働時間}。応答は GET と同じ形）
$C -X PUT $BASE/api/instances/default/usage/cost-settings -H "$H" -H "$J" \
  -d '{"pricing_model":"hourly","hourly_rate":1.006,"fixed_monthly":20,"exchange_rate":150,"months":{"2026-10":{"hours":300}}}'
```

```bat
%C% -X PUT %BASE%/api/instances/default/usage/cost-settings -H %H% -H %J% -d "{\"pricing_model\":\"monthly_bill\",\"months\":{\"2026-10\":{\"bill\":123.45}}}"
```

## スキル研究室

研究テーマの操作は、すべて対象インスタンスの下（`/api/instances/<name>/skill-lab/...`）にある。
他のインスタンスのテーマ ID を指定しても 404 になる。`<name>` にスキル調整ワーカーは指定できない。

一覧・取り込み候補:

```bash
$C $BASE/api/skill-lab/overview                       # インスタンスごとの件数と研究室の稼働状況
$C $BASE/api/instances/default/skill-lab/sources       # ドラフト・正式スキル・正式エージェント・tools に書けるツール名
$C $BASE/api/instances/default/skill-lab/themes
```

テーマの作成・詳細（`lab` は担当のスキル調整ワーカー。ワーカーが1つなら省略可）:

```bash
$C -X POST $BASE/api/instances/default/skill-lab/themes -H "$H" -H "$J" -d '{"title": "見積書チェック", "lab": "lab1"}'
$C $BASE/api/instances/default/skill-lab/themes/<テーマID>
# テーマの削除（フォルダごと。元に戻せない。実行中・開始待ち・AI への依頼の処理中は 409。
# 終わっていないテーマなら取り込み元のドラフトの印を外す）
$C -X DELETE $BASE/api/instances/default/skill-lab/themes/<テーマID> -H "$H"
```

研究対象（スキル・サブエージェント）の追加（`mode`: `draft`＝ドラフトの取り込み（`origin` にドラフトのフォルダ）／`official`＝正式なものの複製／
`new`＝`content` で新規／`ai`＝`request` を AI に下書きさせる）と削除:

```bash
$C -X POST $BASE/api/instances/default/skill-lab/themes/<テーマID>/assets -H "$H" -H "$J" \
  -d '{"mode": "ai", "asset_type": "agents", "name": "estimate-checker", "request": "見積書の金額と明細の整合を確かめる"}'
$C -X DELETE $BASE/api/instances/default/skill-lab/themes/<テーマID>/assets/agents/estimate-checker -H "$H"
```

ファイル（`spec.md`・`config_patch.json`・`assets/...`・`cases/...`）の読み書きと、ケース（フォーム用の構造）・入力ファイル:

```bash
$C "$BASE/api/instances/default/skill-lab/themes/<テーマID>/files?path=spec.md"
$C -X PUT $BASE/api/instances/default/skill-lab/themes/<テーマID>/files -H "$H" -H "$J" -d '{"path": "spec.md", "content": "# ..."}'
# ?create=true を付けると、同じ名前のケースがあれば上書きせずに 409（新しく作るとき）
$C -X PUT "$BASE/api/instances/default/skill-lab/themes/<テーマID>/cases/001_basic?create=true" -H "$H" -H "$J" \
  -d '{"data": {"turns": ["見積書を確認して"], "expect": {"tool_call_args_contains": {"dispatch_agent": {"agent_type": "estimate-checker"}}}}}'
$C -X POST $BASE/api/instances/default/skill-lab/themes/<テーマID>/fixtures -H "$H" -H "$J" \
  -d '{"folder": "sample1", "filename": "見積書.xlsx", "content_base64": "<base64>"}'
```

AI への依頼（`type`: `trial`＝1回試行（`params.case_id`）／`draft_spec`／`draft_cases`（`params.count`）／`draft_asset`）と、
ループの開始（`mode`: `loop`＝スキル調整ループ／`tryout`＝トライアウトだけ）・停止・取りやめ。
開始には `spec.md` の「目的」・研究対象1つ以上・読めるケース1件以上が要り、足りなければ 400（テーマ詳細の `start_missing` に足りないものの一覧が入る）。
下書きの依頼が残っている間・状態が変わった後の開始は 409。開始すると担当（`lab`）が決まる（変えるのは `PUT .../themes/<テーマID>` の `lab`）。
停止は、担当のワーカーが動いていなければその場で `stopped` になる。差し戻し（`return`）は取り込み元のドラフトがあるテーマだけ:

```bash
$C -X POST $BASE/api/instances/default/skill-lab/themes/<テーマID>/tasks -H "$H" -H "$J" -d '{"type": "trial", "params": {"case_id": "001_basic"}}'
$C -X POST $BASE/api/instances/default/skill-lab/themes/<テーマID>/start -H "$H" -H "$J" -d '{"mode": "loop"}'
$C -X POST $BASE/api/instances/default/skill-lab/themes/<テーマID>/stop -H "$H"
$C -X POST $BASE/api/instances/default/skill-lab/themes/<テーマID>/cancel -H "$H"
```

結果・差分・昇格・差し戻し:

```bash
$C $BASE/api/instances/default/skill-lab/themes/<テーマID>/iterations/3
$C $BASE/api/instances/default/skill-lab/themes/<テーマID>/diff
$C "$BASE/api/instances/default/skill-lab/themes/<テーマID>/promote-check?distribute_to=sales"
$C -X POST $BASE/api/instances/default/skill-lab/themes/<テーマID>/promote -H "$H" -H "$J" \
  -d '{"distribute_to": [], "discard_source_changes": false, "note": "judge を確認済み"}'
$C -X POST $BASE/api/instances/default/skill-lab/themes/<テーマID>/return -H "$H" -H "$J" \
  -d '{"reason": "金額の丸め方が仕様と違う", "apply_fixes": true, "reject": false}'
```

## ナレッジ（ベクターDB のコレクションと取り込み）

インスタンスの社内文書コレクション（`[vector_db]`、README_DETAIL.md「ベクターDB（社内文書の意味検索）」）を操作する。
取り込みは管理ツール内のバックグラウンドジョブで行い、`.../job` で進捗・結果を確認する。
スキル調整ワーカーのインスタンスは対象外（400）。

bash:

```bash
# 設定の要約（埋め込みモデル・対象の拡張子・image_input 等）とコレクション一覧（各コレクションの最新ジョブ付き）
$C $BASE/api/instances/default/knowledge
# コレクションを作成（取り込み元フォルダは管理ツールの PC から見える絶対パス。source_folders で複数指定できる。
# 1つだけなら従来どおり "source_folder":"D:/shared/manuals" でもよい。recursive=true のときは入れ子のフォルダは指定できない）
$C -X POST $BASE/api/instances/default/knowledge/collections -H "$H" -H "$J" \
  -d '{"name":"manuals","description":"品質管理規程","source_folders":["D:/shared/manuals","D:/shared/procedures"],"recursive":true}'
# 説明・フォルダ・サブフォルダ有無の変更（変える項目だけ）
$C -X PUT $BASE/api/instances/default/knowledge/collections/manuals -H "$H" -H "$J" -d '{"description":"品質規程"}'
# 取り込み除外キーワード（取り込み元フォルダより下のパス・フォルダ名・ファイル名に含むものは取り込まない。作成時にも指定できる。*. で始めると拡張子の指定。[] で解除）
$C -X PUT $BASE/api/instances/default/knowledge/collections/manuals -H "$H" -H "$J" -d '{"exclude_keywords":["旧版","/old/","*.xlsx"]}'
# 取り込み対象キーワード（あれば、パス・フォルダ名・ファイル名にどれかを含むファイルだけを取り込む。書き方は除外と同じ。[] で解除）
$C -X PUT $BASE/api/instances/default/knowledge/collections/manuals -H "$H" -H "$J" -d '{"include_keywords":["*.pdf","*.docx"]}'
# 時間指定の自動取り込み（差分）。schedule_times は "HH:MM" の配列、schedule_weekdays は 0=月〜6=日（空なら毎日）。
# 作成時にも同じキーを渡せる。一覧の各コレクションに next_scheduled_at（次回の予定）が付き、自動で始まったジョブは trigger が "schedule"
$C -X PUT $BASE/api/instances/default/knowledge/collections/manuals -H "$H" -H "$J" \
  -d '{"schedule_enabled":true,"schedule_times":["02:00"],"schedule_weekdays":[0,1,2,3,4]}'
# 取り込み開始（差分）。rebuild=true で全ベクターを消して取り込み直す
$C -X POST $BASE/api/instances/default/knowledge/collections/manuals/ingest -H "$H" -H "$J" -d '{"rebuild":false}'
# 進捗・結果（state: queued/running/done/error/stopped、done/total、result.failed。kind は ingest/copy/delete。
# queued は同じインスタンスのコレクションの削除の終わり待ち）
$C $BASE/api/instances/default/knowledge/collections/manuals/job
# 取り込みの停止
$C -X POST $BASE/api/instances/default/knowledge/collections/manuals/stop -H "$H"
# コレクションの削除（ベクターも削除。取り込み元の文書は消えない）。同じインスタンスで取り込み・コピーが動いていれば
# 削除のジョブ（kind が "delete"）を順番待ちにして {"ok":true,"queued":true,"job":{...}} を返し、それらが終わってから消す
# （進捗は .../collections/manuals/job。順番待ちの間は .../stop で取りやめられる）
$C -X DELETE $BASE/api/instances/default/knowledge/collections/manuals -H "$H"
# 別のインスタンス（org_b）へコピー。with_vectors=true はベクター・キーワード索引・ファイル記録まで複製する
# （コピー先と埋め込みモデル・document_prefix・距離関数が違えば 400。false なら定義だけ）。進捗はコピー先の
# .../org_b/knowledge/collections/<dest_name>/job（kind が "copy"）。停止はコピー先の .../org_b/knowledge/collections/<dest_name>/stop。
# コピー元の一覧の各コレクションの outgoing_copies にも、動いているコピーが dest_instance（コピー先）つきで出る。
# コピー中のコピー元は取り込み・削除が 409
$C -X POST $BASE/api/instances/default/knowledge/collections/manuals/copy -H "$H" -H "$J" \
  -d '{"dest_instance":"org_b","dest_name":"manuals","with_vectors":true,"include_schedule":false}'
# コピー先・参照元に選べるインスタンスと、そのコレクション・埋め込みの設定の一覧
$C $BASE/api/knowledge/sources
# 取り込みワーカー（全インスタンス共通の並列数の枠）の状態。load（準備するファイル）・embed（埋め込みのリクエスト）は枠ごとに
# {instance, collection, detail（処理中のファイル）} か null（空き）。requests は埋め込みのリクエストの並列数。
# files は埋め込みを待っている・埋め込み中のファイルごとの {instance, collection, detail（パス）, done・total（埋め込み済み・
# 全体の件数）, requests（送信中のリクエスト数）, idle_seconds（最後に進んでからの秒数）}。waiting は空きを待っている取り込み
# （GET .../knowledge の workers にも同じものが付く）
$C $BASE/api/knowledge/workers
# 別のインスタンス（org_a）のコレクションを複製せずに参照する（name はこのインスタンスでの名前）。
# 一覧の references に status（ok / incompatible / missing）と reason が付く
$C -X POST $BASE/api/instances/default/knowledge/references -H "$H" -H "$J" \
  -d '{"name":"org_a_manuals","source_instance":"org_a","source_collection":"manuals"}'
# 参照をやめる（参照元のコレクションは消えない）
$C -X DELETE $BASE/api/instances/default/knowledge/references/org_a_manuals -H "$H"
```

cmd:

```bat
%C% %BASE%/api/instances/default/knowledge
%C% -X POST %BASE%/api/instances/default/knowledge/collections -H %H% -H %J% -d "{\"name\":\"manuals\",\"source_folder\":\"D:/shared/manuals\"}"
%C% -X POST %BASE%/api/instances/default/knowledge/collections/manuals/ingest -H %H% -H %J% -d "{\"rebuild\":false}"
%C% %BASE%/api/instances/default/knowledge/collections/manuals/job
```

## 管理者設定

全インスタンス共通の設定（`[vector_db] ingest_workers`・`ingest_processes`・`ingest_embed_requests`・`scan_workers`・`max_files_per_collection`・`max_file_mb`・`office_timeout_seconds`・`ingest_temp_dir`（空はシステムの一時フォルダ）・`qdrant_max_retries`（`-1` は無制限）・`qdrant_retry_interval_seconds`、`[admin] assistant_llm_instance`・`assistant_embedding_instance`）。
`<instances_dir>/admin_settings.json` に保存し、config.ini より優先する（再起動なしで効く）。

```bash
# 各項目の現在値（value）・config.ini の値（default）・出どころ（source: dashboard / config.ini / env）と、インスタンス一覧（llm に main_url の接続先）
$C $BASE/api/admin-settings
# 保存（キーは "<section>.<key>"。config.ini と同じ値は上書きとして持たない。不正な値・無いインスタンスは 400）
$C -X PUT $BASE/api/admin-settings -H "$H" -H "$J" \
  -d '{"values":{"vector_db.ingest_workers":2,"vector_db.ingest_processes":0,"vector_db.ingest_embed_requests":8,"admin.assistant_llm_instance":"s_lab"}}'
# 画面で保存した値を消して config.ini の値に戻す
$C -X DELETE $BASE/api/admin-settings -H "$H"
# インスタンスの [llm] main_url へ接続できるか（各接続先へ GET /models。ok・endpoints[].ok/detail）
$C -X POST $BASE/api/admin-settings/check-llm -H "$H" -H "$J" -d '{"instance":"s_lab"}'
```

## データのバックアップ

`[admin] instances_dir` と全インスタンスの `[paths] common_data_dir` を、日時つきの zip にまとめて保存先へ退避する（全インスタンス共通）。

```bash
# 実効の設定（settings）・項目ごとの出どころ（sources: dashboard / config.ini）・実行状態（job: state は running/done/error/stopped、
# done/total、result.file・result.failed）・次回予定（next_run_at）・保存先の zip の一覧（backups）
$C $BASE/api/data-backup
# 設定を保存（<instances_dir>/data_backup.json。config.ini の [data_backup] より優先。保存先がバックアップ元の中なら 400）
$C -X PUT $BASE/api/data-backup/settings -H "$H" -H "$J" \
  -d '{"enabled":true,"dest_dir":"\\\\fileserver\\share\\locohane_backup","times":["02:00"],"weekdays":[],"keep":7,"exclude_workdir":true,"exclude_logs":true,"exclude":[]}'
# 画面で保存した設定を消して config.ini の既定に戻す
$C -X DELETE $BASE/api/data-backup/settings -H "$H"
# 今すぐバックアップ（保存先が未設定なら 400、実行中なら 409）・停止
$C -X POST $BASE/api/data-backup/run -H "$H"
$C -X POST $BASE/api/data-backup/stop -H "$H"
```

## 管理アシスタント（AIに相談）

困りごとを送ると、関係する設定キー・画面を AI が助言する（設定は読むだけで変更しない）。
`GET /api/assistant/status` で有効か（`[admin] assistant_enabled`）と、回答に使う LLM の取得元インスタンス（固定していれば）が分かる。

`POST /api/assistant/chat` は NDJSON（1行1イベント）で少しずつ返す。`messages` は会話全体（サーバーは保存しないため、
続きの質問では前の回答も含めて送る）で、最後は `role: "user"`。`view` は任意（今開いている画面の手がかり）。

| イベント | 内容 |
|---|---|
| `{"type":"delta","text":"..."}` | 回答の続き（思考部分は除く） |
| `{"type":"done","text":"...","html":"...","keys":[{"section":"llm","key":"max_concurrent_requests"}]}` | 回答の全文・HTML・回答中で参照された実在するキー |
| `{"type":"error","message":"..."}` | LLM に接続できない等 |

```bash
$C $BASE/api/assistant/status
# => {"enabled":true,"llm_instance":null}
$C -N -X POST $BASE/api/assistant/chat -H "$H" -H "$J"   -d '{"instance":"default","messages":[{"role":"user","content":"2人同時に使うと待たされる"}]}'
```

```bat
%C% %BASE%/api/assistant/status
%C% -N -X POST %BASE%/api/assistant/chat -H %H% -H %J% -d "{\"instance\":\"default\",\"messages\":[{\"role\":\"user\",\"content\":\"2人同時に使うと待たされる\"}]}"
```

## 変更履歴

新しい順に最大 `limit` 件（既定200）。`instance` で絞り込める。

```bash
$C "$BASE/api/audit?limit=50&instance=default"
```

```bat
%C% "%BASE%/api/audit?limit=50&instance=default"
```

## 管理ツールログ

管理ツール自身のログ（`<instances_dir>/_admin_logs/admin_*.log`）を、`level` 以上で新しい順に最大 `limit` 件
（既定200、上限2000）。`q` でメッセージを絞り込める。各エントリの `thread_name` は Python のスレッド名。

```bash
$C "$BASE/api/admin-logs?level=WARNING&limit=200"
```

```bat
%C% "%BASE%/api/admin-logs?level=WARNING&limit=200"
```

## このリファレンス自体

`X-Locohane-Lang: en` を付けると英訳（`admin/API_REFERENCE.en.md`）を返す。

```bash
$C $BASE/api/docs/api-reference
# => {"markdown":"...","html":"..."}
```

```bat
%C% %BASE%/api/docs/api-reference
```
