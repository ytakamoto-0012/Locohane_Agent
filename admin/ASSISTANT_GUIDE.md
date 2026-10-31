# Screen guide of the settings dashboard (for the admin assistant)

This file is the map of screens and settings that the admin assistant ("Ask AI" at the top right of the
dashboard) rereads for every answer. Edits take effect from the next answer without restarting the admin tool.

Screen names are written as "English name (Japanese name)". Use the name in the administrator's display
language when you answer.

## Basics

- The settings dashboard is an admin tool that runs as a separate process from Locohane itself (the chat screen).
- config.ini holds the defaults. Values changed on the screen are saved per instance in `instances/<name>/config_overrides.json`.
- Priority: environment variables > values saved on the screen (config_overrides.json) > config.ini. Keys overridden by environment variables get a badge on the screen, and saving them on the screen has no effect.
- Most config.ini settings take effect after **restarting that instance** once saved (you can choose "restart" when saving). Display settings (header.md, etc.) need no restart.
- The `[admin]` section (the settings of the admin tool itself) cannot be changed on the screen. Edit config.ini or the environment variables directly and restart the admin tool (admin.bat).
- `.env` at the project root (ADMIN_USERS and the shared AUTH_USERS) cannot be changed on the screen. Edit it with a text editor.

## Top tabs

- **Instances (インスタンス)**: the list of instance cards. Card buttons: Open, Start, Stop, Restart, Edit (display name, host, port, autostart, headless, auto reload), Monitor, Research themes, Knowledge, Settings, Delete. "+ Add instance" creates a main instance (chat) or a skill tuning worker (copying an existing instance is also possible).
- **Skill Lab (スキル研究室)**: the workspace where the AI fixes skills and sub-agents by itself until they are finished, and you approve and promote them to an instance. Nothing progresses unless a skill tuning worker (an instance of kind skill_lab) is running.
- **Display settings (表示設定)**: header.md, tab_title.md, welcome.md, icon and favicon shared by all instances.
- **Change history (変更履歴)**: the audit log of setting changes, starts/stops, user operations, etc.
- **API reference (APIリファレンス)**: how to do the same things as the screen operations with curl.

## Sub tabs of the instance details (opened from "Settings" etc. on a card)

- **Monitor (モニター)**: connected users, threads being generated, WARNING/ERROR counts in the last 24 hours, reachability of the LLM endpoints and slot usage. Look here first when "the LLM cannot be reached" or "it is slow".
- **Conversations (会話)**: conversation contents and token usage per user and thread (not available with `[thread_store] enabled=false`; token usage only with `[llm] track_token_usage=true`).
- **Logs (ログ)**: shows `app_*.log` filtered by level, text and thread_id. Use it to investigate the cause of errors.
- **config.ini**: the screen for editing config keys. The search box in the left sidebar searches key names and descriptions. Preset buttons (parallelism, token control, skills, vector DB) narrow the list to frequently changed keys. Check the diff with "Review changes" before "Save".
- **Users (ユーザー)**: add users who log in to the instance's chat screen (AUTH_USERS), change passwords, delete users, and regenerate the JWT signing key. While none are set, the users in `.env` at the project root are inherited.
- **Environment variables (環境変数)**: environment variables only for the instance (`instances/<name>/.env`).
- **Display settings (表示設定)**: header.md etc. only for the instance (items not set use the shared settings).
- **Backups (バックアップ)**: restore from the settings snapshots taken on every save. Use this when the settings got broken.
- **Skill Lab (スキル研究室)**: the research themes of the instance.
- **Knowledge (ナレッジ)**: create vector DB collections, ingest documents, and schedule automatic ingestion. Needs `[vector_db] enabled=true` and an embedding server (`embedding_url`). Ingestion is done by the admin tool, which rereads the instance's settings on every ingestion, so the `[vector_db]` settings about ingestion (`max_file_mb`, `chunk_size`, `include_extensions`, etc.) take effect from the next ingestion once saved (no admin tool restart needed; settings about searching from the chat take effect after restarting the instance). `ingest_workers` (files prepared at once), `ingest_processes` and `ingest_embed_requests` (embedding requests sent at once; 0 = same as ingest_workers) are the exception: they are shared by all instances, cannot be changed per instance (change them on the "Admin settings" top tab, or in config.ini), and cap the total parallelism of all ingestions across instances and collections (the rest waits for a free worker); a change also applies from the next ingestion. The admin tool's own log (ingestion/deletion progress and errors) is instances/_admin_logs/admin_*.log.
- **Admin settings (管理者設定)** (top tab): settings shared by all instances that cannot be changed in an instance's config.ini tab: [vector_db] ingest_workers, ingest_processes and ingest_embed_requests (total knowledge ingestion parallelism: files prepared at once, and embedding requests sent at once), [vector_db] scan_workers (folders read at once when scanning the source), max_files_per_collection (maximum files per collection), max_file_mb (maximum size of one file) and office_timeout_seconds (time limit of one Office-to-PDF export) and ingest_temp_dir (folder for temporary ingestion files; empty is the system temporary folder), [vector_db] qdrant_max_retries (retries when Qdrant cannot be reached or closes the connection; -1 is unlimited, 0 does not retry; searches are retried at most once) and qdrant_retry_interval_seconds (interval between those retries), [admin] assistant_llm_instance (which instance's [llm] main_url writes the answers of "Ask AI"; empty means the instance being asked about) and [admin] assistant_embedding_instance. Saved to <instances_dir>/admin_settings.json, they take precedence over config.ini and apply without restarting the admin tool. If "Ask AI" cannot get a response from the LLM (common during the first setup), tell the user to pick an instance with a running LLM in "LLM used for answers" there and use "Check connection".
- **Data backup (データバックアップ)** (top tab): saves [admin] instances_dir and every instance's [paths] common_data_dir as a dated zip (locohane_backup_YYYYMMDD_HHMMSS.zip) to a destination folder, which may be a file share (UNC path). Settings (destination, scheduled times/days, generations to keep, excluding the working folder/logs, extra patterns) are edited on that screen and saved to <instances_dir>/data_backup.json; the defaults come from config.ini ([data_backup]), and it cannot be overridden per instance. SQLite files are copied consistently even while in use; Qdrant data is not included. Scheduled backups run only while the admin tool runs. Restore by stopping everything and extracting the zip back (manifest.json lists the original locations). The data itself must stay on a local disk: instances_dir, common_data_dir, checkpoint_db, [thread_store] db and [vector_db] dir reject UNC paths and network drives (startup error / save error), because SQLite on a share causes "database is locked".

## Common problems and where to look

- **The chat screen does not open / does not start**: the state on the card (stopped, crashed, configuration error) -> "Logs" in the details. A port conflict is reported as an error when creating or starting. "Running externally" means it was started directly with app.bat and cannot be stopped or restarted from the admin tool.
- **The LLM cannot be reached / no response**: the LLM endpoint reachability in "Monitor" -> `[llm] main_url` and `sub_url` in config.ini. Also check that the inference server (llama.cpp / vLLM) itself is running.
- **Responses are slow / users wait when using it at the same time**: the "parallelism" preset (`[llm] max_concurrent_requests`, `[graph] max_parallel`, `[subagent] max_parallel`), the number of parallel slots on the inference server, `[llm] main_routing_strategy`.
- **Long conversations fail / the context overflows**: the "token control" preset (`[context_compaction]`, `[context_trim]`, `token_guard_*`). Decide together with the context length of the inference server.
- **Timeouts**: `[llm] request_timeout_seconds`, `[user_response_timeouts]`, `[scripts] timeout`, `[llm] stream_chunk_timeout_seconds`.
- **Skills cannot be used / script runs are refused**: the "skills" preset (`[paths]`, `[main_agent_tool_guard]`, `[plan] plan_approval_exempt_scripts`, `[subagent] agent_type_run_script_allowlist`).
- **Cannot log in (chat screen)**: "Users" in the details. The `[auth]` settings.
- **Cannot log in (admin tool)**: ADMIN_USERS in `.env` at the project root (cannot be changed on the screen). Repeated failures cause a temporary lockout.
- **Want to change the look of the screen / the welcome message**: "Display settings" (shared ones in the top tab, instance-only ones in the details sub tab).
- **Want it to search internal documents**: "Knowledge" in the details and the "vector DB" preset.
- **Changed a setting but it has no effect**: was the instance restarted, is it overridden by an environment variable (badge), was the right instance edited.
- **Want to undo settings**: "Reset to default" per key, or restore from "Backups" for many at once.
