# API reference (operating the admin API directly with curl)

Everything you can do on the settings dashboard can also be done with the HTTP API below.
Use it for bulk operations and automation from scripts.

> This file (`admin/API_REFERENCE.en.md`) is shown as is in the dashboard's "API reference" menu
> when the display language is English. It is the English translation of `admin/API_REFERENCE.md`
> (the Japanese version is authoritative). Edits are reflected the next time the screen is opened
> (no need to restart the admin tool).

## Common rules

- Base URL: `http://127.0.0.1:8001` (`[admin].host`/`port`, or `ADMIN_HOST`/`ADMIN_PORT` in `admin.bat`)
- **Authentication**: send the cookie received from `POST /api/login` (`locohane_admin_session`) with later requests.
  The cookie's expiry is extended on every request (`[admin].session_timeout_minutes`).
- **CSRF header**: requests other than GET (POST/PUT/DELETE) must always have `X-Locohane-Admin: 1`.
  Without it you get `403`.
- Request bodies are JSON (`Content-Type: application/json`).
- On errors, `{"detail": "error message"}` is returned.
- **Language**: with `X-Locohane-Lang: en`, error messages (`detail`) and the admin assistant's answers are in English
  (Japanese without it; messages without a translation stay in Japanese). The default display language (`[ui].default_language`)
  is returned by `GET /ui-config.js` (no authentication) in the form `window.LOCOHANE_DEFAULT_LANGUAGE = "auto";`.

| Status | Meaning |
|---|---|
| 400 | Invalid input (validation failed). Also `400` with `There was an error parsing the body` when the body cannot be read as UTF-8 JSON (e.g. when non-ASCII text is passed directly from cmd.exe; see below) |
| 401 | Not logged in / session expired |
| 403 | No CSRF header, or the target cannot be changed |
| 404 | The instance or user does not exist |
| 409 | State conflict (cannot delete while running, another session saved first, etc.) |
| 422 | Required fields missing / wrong types |
| 429 | Locked out after repeated login failures |

## Preparation (defining variables)

Each example is given for both **bash** (Git Bash etc.) and **cmd** (Windows cmd.exe).
Define the following variables first and reuse them in the later examples.

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

### Notes for cmd.exe

- Write `curl.exe`, not `curl` (included as standard in Windows 10 and later).
- Enclose the whole JSON in `"` and write the inner `"` as `\"` (e.g. `-d "{\"key\":\"value\"}"`).
  Single quotes `'` cannot be used.
- If the URL contains `&`, enclose the whole URL in `"`.
- To break a line in the middle, put `^` at the end of the line.
- **Do not write JSON containing non-ASCII characters (Japanese etc.) directly on the command line.**
  cmd.exe passes it to curl.exe in the console code page (e.g. cp932), and even with `chcp 65001`
  the server cannot read it as UTF-8 and returns an error. Save it to a UTF-8 (no BOM) file
  and pass it with `-d @filename` (see the examples below).
- When writing in a `.bat` file, write `for /f` variables as `%%i`, not `%i`.

## Login / logout

### Login

Users are `ADMIN_USERS` in `.env` at the project root.

bash:

```bash
$C -X POST $BASE/api/login -H "$J" \
  -d '{"username":"admin","password":"password"}'
# => {"username":"admin","version":"0.2.0"}
```

cmd (when the password is ASCII only):

```bat
%C% -X POST %BASE%/api/login -H %J% -d "{\"username\":\"admin\",\"password\":\"password\"}"
```

cmd (when the password contains non-ASCII characters, pass `login.json` saved in UTF-8):

```bat
%C% -X POST %BASE%/api/login -H %J% -d @login.json
```

### Checking the logged-in user and version

```bash
$C $BASE/api/me
# => {"username":"admin","version":"0.2.0"}
```

```bat
%C% %BASE%/api/me
```

### Logout

```bash
$C -X POST $BASE/api/logout -H "$H"
```

```bat
%C% -X POST %BASE%/api/logout -H %H%
```

## Instances

### List

```bash
$C $BASE/api/instances
```

```bat
%C% %BASE%/api/instances
```

Main fields of each element: `name`, `display_name`, `app_host`, `app_port`,
`autostart`, `headless`, `watch`, `state` (`running`/`stopped`/`external`/`crashed`),
`pid`, `url`, `is_default`, `kind` (`app` = Locohane itself / `skill_lab` = Skill Lab).

### Create

Everything except `name` is optional. If `app_port` is omitted, a free port is chosen automatically.
Specify an existing instance name in `copy_from` to copy its settings.
Specify `skill_lab` in `kind` to create a Skill Lab worker (no chat screen, no port) (default `app`).
With `copy_extensions: true`, the source instance's own extension directory (skills and sub-agents promoted in the lab) is copied too.
The response has the same fields as one entry of the list, plus `skipped_settings` (a list of messages naming the settings of the source
that were not carried over and why: a `[paths] common_data_dir` with a fixed path that has no `${instance}`, settings not used by the kind
of instance being created, etc. Empty if none).

bash:

```bash
$C -X POST $BASE/api/instances -H "$H" -H "$J" -d '{
  "name": "test2",
  "display_name": "For testing",
  "app_host": "127.0.0.1",
  "app_port": 8002,
  "autostart": false,
  "headless": true,
  "watch": false,
  "copy_from": "default"
}'
```

cmd (if the display name contains non-ASCII characters, save the JSON above in UTF-8 as `instance.json` and pass it):

```bat
%C% -X POST %BASE%/api/instances -H %H% -H %J% -d @instance.json
```

cmd (ASCII only can be written directly):

```bat
%C% -X POST %BASE%/api/instances -H %H% -H %J% -d "{\"name\":\"test2\",\"app_port\":8002,\"copy_from\":\"default\"}"
```

### Changing settings (display name, host, port, etc.)

Send only the fields you want to change. Host and port changes of a running instance take effect after a restart.

```bash
$C -X PUT $BASE/api/instances/test2 -H "$H" -H "$J" \
  -d '{"display_name":"For testing 2","autostart":true}'
```

```bat
%C% -X PUT %BASE%/api/instances/test2 -H %H% -H %J% -d "{\"app_port\":8003,\"autostart\":true}"
```

### Delete

`409` while running; stop it first. `default` cannot be deleted.

Without a body, only `instances/<name>/` is deleted and the persistent data (logs, threads,
default_workdir, etc.) is kept. To delete data together with it, first get the deletion candidates,
and put the `key` of items with `deletable: true` in `delete_data`
(specifying an item with `deletable: false` gives `400` and deletes nothing).

```bash
# List of deletion candidates (key/label/path/exists/deletable/reason/default_selected)
$C $BASE/api/instances/test2/data-paths
# Delete only the instance (keep the data)
$C -X DELETE $BASE/api/instances/test2 -H "$H"
# Also delete the whole data directory
$C -X DELETE $BASE/api/instances/test2 -H "$H" -H "$J" -d '{"delete_data":["common_data_dir"]}'
# => {"success":true,"deleted_data":["C:\\work\\Locohane_Agent\\data\\test2"]}
```

```bat
%C% %BASE%/api/instances/test2/data-paths
%C% -X DELETE %BASE%/api/instances/test2 -H %H%
%C% -X DELETE %BASE%/api/instances/test2 -H %H% -H %J% -d "{\"delete_data\":[\"log_dir\",\"default_workdir\"]}"
```

### Start / stop / restart

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

## config.ini overrides

The admin tool does not rewrite config.ini itself; it saves the differences in
`instances/<name>/config_overrides.json`.
The `[admin]` section cannot be changed.

### Getting the current values

```bash
$C $BASE/api/instances/default/config
```

```bat
%C% %BASE%/api/instances/default/config
```

`keys` contains, for every key, `section`, `key`, `default` (the config.ini value),
`override` (the override value, null if none), `effective` (the effective value),
`env_override_active` (whether an environment variable overrides it), etc.
`mtime` is used for the conflict check when saving (see below).

`keys` contains only the keys that apply to the instance's kind. For a Skill Lab worker, only the `[llm]` endpoints
(`main_url`, `sub_url`, `main_routing_strategy`, `sub_routing_strategy`), `[skill_lab_worker]` and `[log]`;
regular instances do not get `[skill_lab_worker]`. `scope_note` has an explanation only for workers (null otherwise).
Putting a key that is not listed in `updates` gives `400` for both preview and save.

### Getting the candidate skill names and agent_types

```bash
$C $BASE/api/instances/default/config/candidates
```

```bat
%C% %BASE%/api/instances/default/config/candidates
```

Returns the skills and sub-agents that the instance recognizes with its saved settings (the bundled `skills/` and `agents/`,
`[paths] project_locohane_dir`, and the instance-only extension directory). The settings screen uses them as the choices
for the skill name, script file name, agent_type and tool name fields of `[subagent] agent_type_run_script_allowlist` and similar keys.

- `skills`: `name`, `description`, `origin` (`builtin` / `shared` / `instance`), `scripts` (file names of `scripts/*.py`)
- `agent_types`: `name`, `description`, `origin`
- `tools`: built-in tool names of the main agent (the choices for tool names in `[main_agent_tool_guard] allow_entries`; MCP dynamic tools are not included; empty if they cannot be obtained)
- `error`: the reason when the list could not be built because the settings could not be read (`skills` and `agent_types` are then empty). Normally null

### Previewing changes (without saving)

```bash
$C -X POST $BASE/api/instances/default/config/preview -H "$J" -d '{
  "updates": {"llm": {"temperature": "0.3"}},
  "resets": [["subagent", "max_iterations"]]
}'
```

```bat
%C% -X POST %BASE%/api/instances/default/config/preview -H %J% -d "{\"updates\":{\"llm\":{\"temperature\":\"0.3\"}},\"resets\":[[\"subagent\",\"max_iterations\"]]}"
```

- `updates`: `{"section": {"key": "a string in the same format as written in config.ini"}}`
- `resets`: an array of `[section, key]` for keys whose override is removed, returning them to the config.ini value

### Save

Put the `mtime` you got just before in `base_mtime` as is
(`null` for an instance that has never been saved). If someone else saved first, you get `409`.
The examples below use Python to extract `mtime`.

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

cmd (in a `.bat` file, write `%i` as `%%i`):

```bat
for /f "delims=" %i in ('%C% %BASE%/api/instances/default/config ^| python -c "import sys,json; print(json.dumps(json.load(sys.stdin)['mtime']))"') do set MTIME=%i
%C% -X PUT %BASE%/api/instances/default/config -H %H% -H %J% -d "{\"updates\":{\"llm\":{\"temperature\":\"0.3\"}},\"resets\":[],\"base_mtime\":%MTIME%}"
```

If `needs_restart` is `true`, the instance needs a restart for the change to take effect.

### Backup list / restore

```bash
$C $BASE/api/instances/default/backups
$C -X POST $BASE/api/instances/default/backups/config_overrides_20260930_120000.json/restore -H "$H"
```

```bat
%C% %BASE%/api/instances/default/backups
%C% -X POST %BASE%/api/instances/default/backups/config_overrides_20260930_120000.json/restore -H %H%
```

### Copy settings from another instance

`copy-preview` returns the settings whose values differ from the source (`source`) as `changes` (a key whose `new` is `null` is reset
to the default), the settings that are not copied (`skipped`), and the target's `mtime`. `copy` applies them (list `[section, key]`
pairs in `keys` to copy only those; omit it to copy all `changes`). Pass the `mtime` from `copy-preview` as `base_mtime` (409 if
another session saved first). `[paths] common_data_dir` and settings that do not apply to the target keep the target's values.
The previous content is backed up, and the change is recorded in the change history as `config_copy`.

```bash
$C -X POST $BASE/api/instances/test2/config/copy-preview -H "$J" -d '{"source":"default"}'
$C -X POST $BASE/api/instances/test2/config/copy -H "$H" -H "$J" -d '{"source":"default","keys":[["llm","temperature"]],"base_mtime":1759200000.0}'
```

```bat
%C% -X POST %BASE%/api/instances/test2/config/copy-preview -H %J% -d "{\"source\":\"default\"}"
%C% -X POST %BASE%/api/instances/test2/config/copy -H %H% -H %J% -d "{\"source\":\"default\",\"keys\":[[\"llm\",\"temperature\"]],\"base_mtime\":1759200000.0}"
```

## Login users (Locohane itself)

Operates `AUTH_USERS` in `instances/<name>/.env`.
While the instance has none set, it inherits the users of `.env` at the project root
(`inherited_from_project_env: true`), and inherited users cannot be changed or deleted (`403`).

bash:

```bash
# List
$C $BASE/api/instances/default/users
# Add
$C -X POST $BASE/api/instances/default/users -H "$H" -H "$J" \
  -d '{"username":"alice","password":"secret"}'
# Change password
$C -X PUT $BASE/api/instances/default/users/alice -H "$H" -H "$J" \
  -d '{"password":"new-secret"}'
# Rank used to prioritize memories (saved in [memory] user_ranks; null removes it; can be set for inherited users too)
$C -X PUT $BASE/api/instances/default/users/alice/rank -H "$H" -H "$J" -d '{"rank":3}'
# Delete
$C -X DELETE $BASE/api/instances/default/users/alice -H "$H"
# Delete several at once (nothing is deleted if any of them cannot be)
$C -X POST $BASE/api/instances/default/users/delete -H "$H" -H "$J" \
  -d '{"usernames":["alice","bob"]}'
# Regenerate the JWT signing key (CHAINLIT_AUTH_SECRET)
$C -X POST $BASE/api/instances/default/auth-secret -H "$H"
```

cmd:

```bat
rem List
%C% %BASE%/api/instances/default/users
rem Add
%C% -X POST %BASE%/api/instances/default/users -H %H% -H %J% -d "{\"username\":\"alice\",\"password\":\"secret\"}"
rem Change password
%C% -X PUT %BASE%/api/instances/default/users/alice -H %H% -H %J% -d "{\"password\":\"new-secret\"}"
rem Rank used to prioritize memories (null removes it)
%C% -X PUT %BASE%/api/instances/default/users/alice/rank -H %H% -H %J% -d "{\"rank\":3}"
rem Delete
%C% -X DELETE %BASE%/api/instances/default/users/alice -H %H%
rem Delete several at once (nothing is deleted if any of them cannot be)
%C% -X POST %BASE%/api/instances/default/users/delete -H %H% -H %J% -d "{\"usernames\":[\"alice\",\"bob\"]}"
rem Regenerate the JWT signing key (CHAINLIT_AUTH_SECRET)
%C% -X POST %BASE%/api/instances/default/auth-secret -H %H%
```

## Environment variables (the instance's .env)

Any environment variables other than `AUTH_USERS`/`CHAINLIT_AUTH_SECRET`.
In the list, values such as passwords and keys are masked.

bash:

```bash
# List
$C $BASE/api/instances/default/env
# Add / update
$C -X PUT $BASE/api/instances/default/env -H "$H" -H "$J" \
  -d '{"key":"MY_VAR","value":"hello"}'
# Delete
$C -X DELETE $BASE/api/instances/default/env/MY_VAR -H "$H"
```

cmd:

```bat
rem List
%C% %BASE%/api/instances/default/env
rem Add / update
%C% -X PUT %BASE%/api/instances/default/env -H %H% -H %J% -d "{\"key\":\"MY_VAR\",\"value\":\"hello\"}"
rem Delete
%C% -X DELETE %BASE%/api/instances/default/env/MY_VAR -H %H%
```

## Memory (the instance's persistent memory)

Reads and writes the memories under `[paths] memory_dir` of the instance's effective settings. Memory is per user;
specify the owner with the query `owner` (the login username, normalized into the folder name by the same rule as the
app). If omitted or empty, it means the common memories (those from before memory became per user). The admin API can
read and write all of them regardless of visibility.
`type` is `user`/`feedback`/`project`/`reference`/`glossary`; `visibility` is `private`/`shared` (users' memories only; if omitted,
`shared` on create and the current value on edit; types set to `private` and content matching the checks turned on in
`[memory]` of the instance become `private`). Editing cannot change `name` or the owner (the recorded source files `sources` are kept).
The content before an edit or deletion is kept in `instances/<name>/backups/memory/<owner (_common for common)>/<memory name>/`.
Translation has the LLM translate the description and content into the language of `X-Locohane-Lang` (`ja`/`en`;
`ja` if absent) and only returns the result without saving (the LLM is the "LLM used for answers" in "Admin
settings", or the `[llm]` endpoints of the instance itself if empty).

bash:

```bash
# List (memory_dir, types, visibilities, owners, memories[]: name/description/type/owner (null for common)/visibility/mtime/sources (number of source files))
$C $BASE/api/instances/default/memories
# One memory (content and the check of each source file, sources[]: path/state)
$C "$BASE/api/instances/default/memories/my-note?owner=alice"
# Create (409 if the owner already has the name; common if owner is omitted; the response has the actual owner and visibility)
$C -X POST $BASE/api/instances/default/memories -H "$H" -H "$J" \
  -d '{"name":"my-note","description":"One-line description","type":"feedback","content":"Text","owner":"alice","visibility":"shared"}'
# Edit (the response has the actual visibility)
$C -X PUT "$BASE/api/instances/default/memories/my-note?owner=alice" -H "$H" -H "$J" \
  -d '{"description":"One-line description","type":"feedback","content":"New text","visibility":"private"}'
# Delete
$C -X DELETE "$BASE/api/instances/default/memories/my-note?owner=alice" -H "$H"
# Delete several at once (nothing is deleted if any of them is not found; omit owner for common)
$C -X POST $BASE/api/instances/default/memories/delete -H "$H" -H "$J" \
  -d '{"items":[{"name":"my-note","owner":"alice"},{"name":"old-note"}]}'
# Copy to another instance (the source is unchanged; each memory is handled on its own, and failures are reported with a reason).
# Omit target_owner to keep each memory's owner, "" for common, or a user name to put them all under that user.
# A private memory is copied only to the same owner. A memory whose name exists at the destination is skipped (never replaced).
# Response: copied/skipped/failed/skipped_items/failed_items
$C -X POST $BASE/api/instances/default/memories/copy -H "$H" -H "$J" \
  -d '{"items":[{"name":"my-note","owner":"alice"}],"target_instance":"other","target_owner":null}'
# Translate (into English; not saved; the response has description/content/language/llm_instance)
$C -X POST $BASE/api/instances/default/memories/translate -H "$H" -H "$J" -H "X-Locohane-Lang: en" \
  -d '{"description":"(one-line description in another language)","content":"(content in another language)"}'
# Polish [memory] shared_criteria / private_criteria (kind is shared / private; written in the language of X-Locohane-Lang;
# not saved; the response has text/language/llm_instance)
$C -X POST $BASE/api/instances/default/memories/polish-criteria -H "$H" -H "$J" -H "X-Locohane-Lang: en" \
  -d '{"kind":"private","text":"(a rough note of what should be private)"}'
# Candidate users to create a draft skill for (owners, default (anonymous), draft_dir, other_users_drafts)
$C $BASE/api/instances/default/memories/draft-skill-owners
# Create a draft skill from selected memories (the LLM drafts SKILL.md, saved in [skill_creator] draft_dir/<owner>/<skill_name>/;
# owner defaults to anonymous, and the LLM chooses skill_name if omitted; up to 20 memories; written in the language of
# X-Locohane-Lang; the response has owner/skill_name/description/path/content/private_memories_of_others/llm_instance)
$C -X POST $BASE/api/instances/default/memories/draft-skill -H "$H" -H "$J" -H "X-Locohane-Lang: en" \
  -d '{"items":[{"name":"my-note","owner":"alice"},{"name":"old-note"}],"owner":"alice","skill_name":"monthly-check","request":"(what kind of skill you want)"}'
```

cmd:

```bat
rem List
%C% %BASE%/api/instances/default/memories
rem Edit
%C% -X PUT "%BASE%/api/instances/default/memories/my-note?owner=alice" -H %H% -H %J% -d "{\"description\":\"desc\",\"type\":\"feedback\",\"content\":\"text\",\"visibility\":\"shared\"}"
rem Delete
%C% -X DELETE "%BASE%/api/instances/default/memories/my-note?owner=alice" -H %H%
```

## Display settings (header, tab title, welcome message, icons)

With `?instance=<name>`, the settings of that instance only are targeted;
without it, the settings shared by all instances (`public/settings/`).

Text file names are `header.md` / `tab_title.md` / `welcome.md`,
image kinds are `icon` / `favicon`.

bash:

```bash
# Get the effective values
$C "$BASE/api/settings?instance=default"

# Save text (shared)
$C -X PUT $BASE/api/settings/header.md -H "$H" -H "$J" \
  -d '{"content":"Company AI assistant"}'

# Save text (instance only)
$C -X PUT "$BASE/api/settings/welcome.md?instance=default" -H "$H" -H "$J" \
  -d '{"content":"Welcome. Available skills:\n{skills}"}'

# Delete the instance-only text and go back to the shared one (instance required)
$C -X DELETE "$BASE/api/settings/welcome.md?instance=default" -H "$H"

# Upload an image (sent as base64)
B64=$(base64 -w0 logo.png)
$C -X PUT "$BASE/api/settings/images/icon?instance=default" -H "$H" -H "$J" \
  -d "{\"filename\":\"logo.png\",\"content_base64\":\"$B64\"}"

# Delete the instance-only image and go back to the shared one (instance required)
$C -X DELETE "$BASE/api/settings/images/icon?instance=default" -H "$H"
```

cmd (if the text contains non-ASCII characters, save `{"content":"..."}` in UTF-8 as
`header.json` etc. and pass it. For images, create the base64 JSON with PowerShell and pass it):

```bat
rem Get the effective values
%C% "%BASE%/api/settings?instance=default"

rem Save text (shared)
%C% -X PUT %BASE%/api/settings/header.md -H %H% -H %J% -d @header.json

rem Save text (instance only)
%C% -X PUT "%BASE%/api/settings/welcome.md?instance=default" -H %H% -H %J% -d @welcome.json

rem Delete the instance-only text and go back to the shared one (instance required)
%C% -X DELETE "%BASE%/api/settings/welcome.md?instance=default" -H %H%

rem Upload an image (create body.json with logo.png in base64 first, then send it)
powershell -NoProfile -Command "@{filename='logo.png'; content_base64=[Convert]::ToBase64String([IO.File]::ReadAllBytes('logo.png'))} | ConvertTo-Json | Set-Content -Encoding ascii body.json"
%C% -X PUT "%BASE%/api/settings/images/icon?instance=default" -H %H% -H %J% -d @body.json

rem Delete the instance-only image and go back to the shared one (instance required)
%C% -X DELETE "%BASE%/api/settings/images/icon?instance=default" -H %H%
```

## Monitor (status, conversation viewing, token trends, logs, LLM endpoints)

All read only (GET). `<name>` is the instance name. Only getting conversation contents
(`/monitor/threads/<thread_id>`) records the fact of viewing in the change history
as `conversation_view`.

```bash
# Connected users, session counts and generating thread counts of all instances. context_series has,
# per generating thread, the input tokens of LLM requests since the current generation started
# (points[].value is the maximum of the latest main request and the sub/compaction requests after it)
$C "$BASE/api/monitor/overview"
# Details of one instance (connected sessions, generating threads, warning count in the last 24 hours)
$C "$BASE/api/instances/default/monitor/runtime"
# Per-user thread count, cumulative tokens, last used time
$C "$BASE/api/instances/default/monitor/users"
# Thread list (filter with owner, q (partial match of name/ID), limit, offset)
$C "$BASE/api/instances/default/monitor/threads?owner=alice&limit=50"
# Conversation contents (internal=true also includes tool runs, thinking and UI control messages)
$C "$BASE/api/instances/default/monitor/threads/<thread_id>?internal=false"
# Token usage per LLM call (restored from the app log)
$C "$BASE/api/instances/default/monitor/threads/<thread_id>/tokens"
# App log (level and above, newest first; filter with q and thread_id)
$C "$BASE/api/instances/default/monitor/logs?level=WARNING&limit=200"
# Reachability check of the LLM endpoints (for llama_cpp, slot usage too. For servers with /slots disabled,
# reachability is checked with /health, and error says so while reachable stays true)
$C "$BASE/api/instances/default/monitor/endpoints"
```

```bat
%C% "%BASE%/api/instances/default/monitor/runtime"
%C% "%BASE%/api/instances/default/monitor/threads/<thread_id>/tokens"
```

## Usage & cost

Monthly token usage per user, and costs calculated from AWS charges entered by hand (README_DETAIL.en.md "Usage & cost").

```bash
# Monthly totals, costs and cost settings (settings) for all instances (except skill tuning workers).
# Allocated breakdown in months[].instances / months[].users, unreadable instances in errors
$C "$BASE/api/usage/overview"
# Save the cost settings shared by all instances (pricing_model is monthly_bill / hourly / per_token. Empty numbers = not entered.
# months is "YYYY-MM" -> {"bill": amount billed, "hours": running hours, "emb_bill": embedding part of the bill}.
# embedding_per_million is the embedding price per 1M tokens; embedding_in_share is whether to include embedding tokens in the
# allocation ratio in months without an embedding cost (true/false). The response has the same shape as GET /api/usage/overview.
# emb_tokens / emb_calls in the usage are embeddings (knowledge search, ingestion, etc.); total etc. are generation tokens)
$C -X PUT $BASE/api/usage/cost-settings -H "$H" -H "$J" \
  -d '{"pricing_model":"hourly","hourly_rate":1.006,"fixed_monthly":20,"exchange_rate":150,"months":{"2026-10":{"hours":300}}}'
# Monthly usage of one instance (usage) and the amounts allocated to it from the shared cost (costs)
$C "$BASE/api/instances/default/usage"
```

```bat
%C% -X PUT %BASE%/api/usage/cost-settings -H %H% -H %J% -d "{\"pricing_model\":\"monthly_bill\",\"months\":{\"2026-10\":{\"bill\":123.45}}}"
```

## Skill Lab

All operations on research themes are under the target instance (`/api/instances/<name>/skill-lab/...`).
Specifying a theme ID of another instance gives 404. A Skill Lab worker cannot be specified as `<name>`.

Lists and import candidates:

```bash
$C $BASE/api/skill-lab/overview                       # counts per instance and the lab's running status
$C $BASE/api/instances/default/skill-lab/sources       # drafts, official skills, official agents, tool names usable in tools
$C $BASE/api/instances/default/skill-lab/themes
```

Creating a theme and its details (`lab` is the Skill Lab worker in charge; can be omitted if there is only one worker):

```bash
$C -X POST $BASE/api/instances/default/skill-lab/themes -H "$H" -H "$J" -d '{"title": "Quote check", "lab": "lab1"}'
$C $BASE/api/instances/default/skill-lab/themes/<theme ID>
# Delete a theme (the whole folder; cannot be undone; 409 while running, waiting to start or processing AI requests;
# for a theme that is not finished, the mark on the source drafts is removed)
$C -X DELETE $BASE/api/instances/default/skill-lab/themes/<theme ID> -H "$H"
```

Adding research targets (skills and sub-agents) (`mode`: `draft` = import a draft (the draft's folder in `origin`) / `official` = copy an official one /
`new` = new from `content` / `ai` = have the AI draft it from `request`) and deleting them:

```bash
$C -X POST $BASE/api/instances/default/skill-lab/themes/<theme ID>/assets -H "$H" -H "$J" \
  -d '{"mode": "ai", "asset_type": "agents", "name": "estimate-checker", "request": "Check that the amounts and line items of a quote match"}'
$C -X DELETE $BASE/api/instances/default/skill-lab/themes/<theme ID>/assets/agents/estimate-checker -H "$H"
```

Reading and writing files (`spec.md`, `config_patch.json`, `assets/...`, `cases/...`), cases (structure for the form) and input files:

```bash
$C "$BASE/api/instances/default/skill-lab/themes/<theme ID>/files?path=spec.md"
$C -X PUT $BASE/api/instances/default/skill-lab/themes/<theme ID>/files -H "$H" -H "$J" -d '{"path": "spec.md", "content": "# ..."}'
# With ?create=true, 409 instead of overwriting if a case with the same name exists (when creating a new one)
$C -X PUT "$BASE/api/instances/default/skill-lab/themes/<theme ID>/cases/001_basic?create=true" -H "$H" -H "$J" \
  -d '{"data": {"turns": ["Check the quote"], "expect": {"tool_call_args_contains": {"dispatch_agent": {"agent_type": "estimate-checker"}}}}}'
$C -X POST $BASE/api/instances/default/skill-lab/themes/<theme ID>/fixtures -H "$H" -H "$J" \
  -d '{"folder": "sample1", "filename": "quote.xlsx", "content_base64": "<base64>"}'
```

Requests to the AI (`type`: `trial` = one trial (`params.case_id`) / `draft_spec` / `draft_cases` (`params.count`) / `draft_asset`), and
starting the loop (`mode`: `loop` = skill tuning loop / `tryout` = tryout only), stopping and cancelling.
Starting requires the "Purpose" of `spec.md`, at least one research target and at least one readable case; otherwise 400 (`start_missing` in the theme details lists what is missing).
Starting while draft requests remain, or after the state has changed, gives 409. Starting decides the worker in charge (`lab`) (change it with `lab` of `PUT .../themes/<theme ID>`).
Stopping becomes `stopped` immediately if the worker in charge is not running. Sending back (`return`) is only for themes with an imported draft:

```bash
$C -X POST $BASE/api/instances/default/skill-lab/themes/<theme ID>/tasks -H "$H" -H "$J" -d '{"type": "trial", "params": {"case_id": "001_basic"}}'
$C -X POST $BASE/api/instances/default/skill-lab/themes/<theme ID>/start -H "$H" -H "$J" -d '{"mode": "loop"}'
$C -X POST $BASE/api/instances/default/skill-lab/themes/<theme ID>/stop -H "$H"
$C -X POST $BASE/api/instances/default/skill-lab/themes/<theme ID>/cancel -H "$H"
```

Results, diffs, promotion and sending back:

```bash
$C $BASE/api/instances/default/skill-lab/themes/<theme ID>/iterations/3
$C $BASE/api/instances/default/skill-lab/themes/<theme ID>/diff
$C "$BASE/api/instances/default/skill-lab/themes/<theme ID>/promote-check?distribute_to=sales"
$C -X POST $BASE/api/instances/default/skill-lab/themes/<theme ID>/promote -H "$H" -H "$J" \
  -d '{"distribute_to": [], "discard_source_changes": false, "note": "judge checked"}'
$C -X POST $BASE/api/instances/default/skill-lab/themes/<theme ID>/return -H "$H" -H "$J" \
  -d '{"reason": "Rounding of amounts differs from the spec", "apply_fixes": true, "reject": false}'
```

## Knowledge (vector DB collections and ingestion)

Operates the instance's internal document collections (`[vector_db]`, README_DETAIL.en.md "Vector DB (semantic search of internal documents)").
Ingestion runs as a background job in the admin tool; check progress and results with `.../job`.
Skill Lab worker instances are not supported (400).

bash:

```bash
# Settings summary (embedding model, target extensions, image_input, etc.) and the collection list (with each collection's latest job)
$C $BASE/api/instances/default/knowledge
# Create a collection (source folders are absolute paths visible from the admin tool's PC; source_folders accepts several.
# For a single folder, "source_folder":"D:/shared/manuals" still works. With recursive=true, nested folders are not allowed)
$C -X POST $BASE/api/instances/default/knowledge/collections -H "$H" -H "$J" \
  -d '{"name":"manuals","description":"Quality control rules","source_folders":["D:/shared/manuals","D:/shared/procedures"],"recursive":true}'
# Change the description, folder or subfolder setting (only the fields to change)
$C -X PUT $BASE/api/instances/default/knowledge/collections/manuals -H "$H" -H "$J" -d '{"description":"Quality rules"}'
# Exclusion keywords (paths/folder/file names below the source folder containing one are not ingested; *.ext = extension; also at creation; [] clears)
$C -X PUT $BASE/api/instances/default/knowledge/collections/manuals -H "$H" -H "$J" -d '{"exclude_keywords":["draft","/old/","*.xlsx"]}'
# Target keywords (if any, only files whose path/folder/file name contains one are ingested; same syntax as exclusion; [] clears)
$C -X PUT $BASE/api/instances/default/knowledge/collections/manuals -H "$H" -H "$J" -d '{"include_keywords":["*.pdf","*.docx"]}'
# Scheduled automatic ingestion (incremental). schedule_times is an array of "HH:MM", schedule_weekdays is 0=Mon to 6=Sun (every day if empty).
# The same keys can be passed when creating. Each collection in the list gets next_scheduled_at (the next scheduled time), and automatically started jobs have trigger "schedule"
$C -X PUT $BASE/api/instances/default/knowledge/collections/manuals -H "$H" -H "$J" \
  -d '{"schedule_enabled":true,"schedule_times":["02:00"],"schedule_weekdays":[0,1,2,3,4]}'
# Start ingestion (incremental). rebuild=true deletes all vectors and ingests again
$C -X POST $BASE/api/instances/default/knowledge/collections/manuals/ingest -H "$H" -H "$J" -d '{"rebuild":false}'
# Progress and results (state: queued/running/done/error/stopped, done/total, result.failed; kind is ingest/copy/delete;
# queued means waiting for a collection deletion in the same instance to finish)
$C $BASE/api/instances/default/knowledge/collections/manuals/job
# Stop ingestion
$C -X POST $BASE/api/instances/default/knowledge/collections/manuals/stop -H "$H"
# Delete a collection (the vectors are deleted too; the source documents are not). If ingestions or copies are running in the same
# instance, a delete job (kind "delete") is queued and {"ok":true,"queued":true,"job":{...}} is returned; it deletes after they finish
# (progress: .../collections/manuals/job; while queued it can be cancelled with .../stop)
$C -X DELETE $BASE/api/instances/default/knowledge/collections/manuals -H "$H"
# Copy to another instance (org_b). with_vectors=true duplicates the vectors, keyword index and file records
# (400 if the embedding model, document_prefix or distance differs from the destination; false copies only the definition).
# Progress: the destination's .../org_b/knowledge/collections/<dest_name>/job (kind "copy"). Stop: the destination's .../org_b/knowledge/collections/<dest_name>/stop.
# Each collection in the source's list also shows running copies in outgoing_copies, with dest_instance (the destination).
# Ingesting/deleting the source while copying is 409
$C -X POST $BASE/api/instances/default/knowledge/collections/manuals/copy -H "$H" -H "$J" \
  -d '{"dest_instance":"org_b","dest_name":"manuals","with_vectors":true,"include_schedule":false}'
# Instances selectable as a copy destination or reference source, with their collections and embedding settings
$C $BASE/api/knowledge/sources
# State of the ingestion workers (the parallelism shared by all instances). load (files being prepared) and embed (embedding requests)
# have one entry per worker: {instance, collection, detail (the file being processed)} or null (idle). requests is the embedding request
# parallelism. files has one entry per file waiting for or being embedded: {instance, collection, detail (path), done/total (items embedded /
# all items), requests (requests in flight), idle_seconds (seconds since the last progress)}. waiting lists the ingestions waiting for a free worker
# (GET .../knowledge also returns the same as workers)
$C $BASE/api/knowledge/workers
# Reference a collection of another instance (org_a) without copying it (name is the name in this instance).
# Each entry of references in the overview has status (ok / incompatible / missing) and reason
$C -X POST $BASE/api/instances/default/knowledge/references -H "$H" -H "$J" \
  -d '{"name":"org_a_manuals","source_instance":"org_a","source_collection":"manuals"}'
# Remove a reference (the source collection is not deleted)
$C -X DELETE $BASE/api/instances/default/knowledge/references/org_a_manuals -H "$H"
```

cmd:

```bat
%C% %BASE%/api/instances/default/knowledge
%C% -X POST %BASE%/api/instances/default/knowledge/collections -H %H% -H %J% -d "{\"name\":\"manuals\",\"source_folder\":\"D:/shared/manuals\"}"
%C% -X POST %BASE%/api/instances/default/knowledge/collections/manuals/ingest -H %H% -H %J% -d "{\"rebuild\":false}"
%C% %BASE%/api/instances/default/knowledge/collections/manuals/job
```

## Admin settings

Settings shared by all instances (`[vector_db] ingest_workers`, `ingest_processes`, `ingest_embed_requests`, `scan_workers`, `max_files_per_collection`, `max_file_mb`, `office_timeout_seconds`, `ingest_temp_dir` (empty is the system temporary folder), `qdrant_max_retries` (`-1` is unlimited), `qdrant_retry_interval_seconds`, `[admin] assistant_llm_instance`, `assistant_embedding_instance`).
Saved to `<instances_dir>/admin_settings.json`; they take precedence over config.ini (and apply without a restart).

```bash
# Current value (value), config.ini value (default) and source (source: dashboard / config.ini / env) of each item, and the instances (llm: main_url endpoints)
$C $BASE/api/admin-settings
# Save (keys are "<section>.<key>"; values equal to config.ini are not kept as overrides; invalid values or unknown instances are 400)
$C -X PUT $BASE/api/admin-settings -H "$H" -H "$J" \
  -d '{"values":{"vector_db.ingest_workers":2,"vector_db.ingest_processes":0,"vector_db.ingest_embed_requests":8,"admin.assistant_llm_instance":"s_lab"}}'
# Delete the values saved on the screen and go back to the config.ini values
$C -X DELETE $BASE/api/admin-settings -H "$H"
# Whether the instance's [llm] main_url can be reached (GET /models to each endpoint; ok, endpoints[].ok/detail)
$C -X POST $BASE/api/admin-settings/check-llm -H "$H" -H "$J" -d '{"instance":"s_lab"}'
```

## Data backup

Saves `[admin] instances_dir` and the `[paths] common_data_dir` of all instances to the destination as a dated zip (shared by all instances).

```bash
# Effective settings (settings), where each item comes from (sources: dashboard / config.ini), run state (job: state is running/done/error/stopped,
# done/total, result.file, result.failed), next scheduled run (next_run_at) and the zips in the destination (backups)
$C $BASE/api/data-backup
# Save the settings (<instances_dir>/data_backup.json; takes precedence over [data_backup] in config.ini; 400 if the destination is inside a backup source)
$C -X PUT $BASE/api/data-backup/settings -H "$H" -H "$J" \
  -d '{"enabled":true,"dest_dir":"\\\\fileserver\\share\\locohane_backup","times":["02:00"],"weekdays":[],"keep":7,"exclude_workdir":true,"exclude_logs":true,"exclude":[]}'
# Delete the settings saved on the screen and go back to the config.ini defaults
$C -X DELETE $BASE/api/data-backup/settings -H "$H"
# Back up now (400 if no destination is set, 409 if one is running) / stop
$C -X POST $BASE/api/data-backup/run -H "$H"
$C -X POST $BASE/api/data-backup/stop -H "$H"
```

## Admin assistant (Ask AI)

Send a problem and the AI advises on the related settings keys and screens (it only reads settings and does not change them).
`GET /api/assistant/status` tells whether it is enabled (`[admin] assistant_enabled`) and the instance whose LLM is used for answers (if fixed).

`POST /api/assistant/chat` returns the answer bit by bit as NDJSON (one event per line). `messages` is the whole conversation (the server does not store it,
so for follow-up questions include the previous answers too), ending with `role: "user"`. `view` is optional (a hint about the screen currently open).

| Event | Content |
|---|---|
| `{"type":"delta","text":"..."}` | Continuation of the answer (excluding thinking) |
| `{"type":"done","text":"...","html":"...","keys":[{"section":"llm","key":"max_concurrent_requests"}]}` | The full answer, its HTML, and the existing keys referred to in the answer |
| `{"type":"error","message":"..."}` | Cannot connect to the LLM, etc. |

```bash
$C $BASE/api/assistant/status
# => {"enabled":true,"llm_instance":null}
$C -N -X POST $BASE/api/assistant/chat -H "$H" -H "$J"   -d '{"instance":"default","messages":[{"role":"user","content":"Two people using it at once have to wait"}]}'
```

```bat
%C% %BASE%/api/assistant/status
%C% -N -X POST %BASE%/api/assistant/chat -H %H% -H %J% -d "{\"instance\":\"default\",\"messages\":[{\"role\":\"user\",\"content\":\"Two people using it at once have to wait\"}]}"
```

## Change history

Up to `limit` entries (default 200), newest first. Can be filtered with `instance`.

```bash
$C "$BASE/api/audit?limit=50&instance=default"
```

```bat
%C% "%BASE%/api/audit?limit=50&instance=default"
```

## Admin tool log

The admin tool's own log (`<instances_dir>/_admin_logs/admin_*.log`), `level` and above, up to `limit` entries
(default 200, max 2000), newest first. Filter messages with `q`. `thread_name` of each entry is the Python thread name.

```bash
$C "$BASE/api/admin-logs?level=WARNING&limit=200"
```

```bat
%C% "%BASE%/api/admin-logs?level=WARNING&limit=200"
```

## This reference itself

Returns the English version with `X-Locohane-Lang: en`, the Japanese version otherwise.

```bash
$C $BASE/api/docs/api-reference
# => {"markdown":"...","html":"..."}
```

```bat
%C% %BASE%/api/docs/api-reference
```
