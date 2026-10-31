"""チャット画面向けの表示言語（日本語 / 英語）の切り替え。

画面に出す固定文言（cl.Message のシステム通知・ボタンのラベル・Step の表示名・
ChatSettings の項目名など）だけをここで翻訳する。LLM へ渡す文言（ツールの戻り値・
system_prompt.md・ツールの docstring）は翻訳しない。英語のセッションでは、
system_prompt の末尾に response_language_directive() を足して英語で回答させる
（evals でチューニングした日本語のプロンプト本体を変えないため）。

セッションの言語は cl.user_session の "lang"（app.py の on_chat_start /
on_chat_resume が接続時の userEnv から、set_language アクションが画面の切り替えで
設定する）。セッション文脈の外（テスト・起動処理）では既定言語を使う。

フロントエンド側の文言は frontend/src/i18n.tsx、設定ダッシュボードは
admin/static/i18n.js が持つ（言語の決め方は3者で揃える）。
"""

from __future__ import annotations

import chainlit as cl

SUPPORTED_LANGUAGES = ("ja", "en")
DEFAULT_LANGUAGE = "ja"
SESSION_KEY = "lang"
# frontend/src/App.tsx が connect({ userEnv }) で送るキーと一致させる。
USER_ENV_KEY = "LOCOHANE_LANG"

# [ui].default_language（"auto" はブラウザの言語に従う意味で、言語はフロントエンドが
# 決めて userEnv で送ってくる。サーバー側だけで決める場面では日本語にする）。
_default_language = DEFAULT_LANGUAGE


def normalize(lang: object) -> str | None:
    """言語コードを SUPPORTED_LANGUAGES のいずれかへ正規化する（不明なら None）。

    "en-US" のような地域付きのコードも受け付ける。
    """
    if not isinstance(lang, str):
        return None
    code = lang.strip().lower().split("-", 1)[0].split("_", 1)[0]
    return code if code in SUPPORTED_LANGUAGES else None


def set_default_language(value: str) -> None:
    """[ui].default_language をセッション文脈外・未設定時の既定言語として登録する。"""
    global _default_language
    _default_language = normalize(value) or DEFAULT_LANGUAGE


def current_lang() -> str:
    """このセッションの表示言語を返す（セッション文脈外・未設定なら既定言語）。"""
    try:
        lang = normalize(cl.user_session.get(SESSION_KEY))
    except Exception:  # noqa: BLE001 (ChainlitContextException を含むセッション文脈外の全般)
        lang = None
    return lang or _default_language


def t(key: str, lang: str | None = None, **kwargs: object) -> str:
    """表示文言を引く（lang 省略時はこのセッションの言語）。

    英語訳が無いキーは日本語を返す（日本語が正本）。kwargs は str.format で埋め込む。

    Raises:
        KeyError: カタログに無いキーを指定した場合（typo を早く見つけるため）。
    """
    entry = MESSAGES[key]
    text = entry.get(normalize(lang) or current_lang()) or entry["ja"]
    return text.format(**kwargs) if kwargs else text


# system_prompt の末尾に足す応答言語の指示（プロンプト本体は英語。system_prompt.md の
# "Language" 行がこの節を参照する）。
_RESPONSE_LANGUAGE_NAMES = {"ja": "Japanese", "en": "English"}


def response_language_directive(lang: str | None = None) -> str:
    """system_prompt の末尾に足す、このセッションの応答言語の指示。

    プロンプト・ツールの説明・SKILL.md は英語で書いているため、画面で選んだ言語
    （セッション外・evals では既定言語）で回答するよう明示する。
    """
    name = _RESPONSE_LANGUAGE_NAMES[normalize(lang) or current_lang()]
    return (
        "\n\n## Response language\n"
        f"The user's display language is {name}. Always write your replies to the user in {name}, "
        "even though these instructions, tool descriptions and skills are written in English. This also "
        "applies to tool arguments shown to the user: the steps (content/activeForm) and detail_markdown of "
        "create_plan, and the questions and choices of ask_user_choice/AskUserQuestion. Keep code "
        "identifiers, tool names, tool argument names and file paths unchanged. If the user explicitly asks for "
        "another language, follow that."
    )


MESSAGES: dict[str, dict[str, str]] = {
    # --- 作業ディレクトリ（サイドパネル・歯車の ChatSettings） ---
    "workdir.unset": {
        "ja": "未設定（既定値 {default} を使用）",
        "en": "Not set (using the default {default})",
    },
    "workdir.missing": {
        "ja": "存在しません（このPCから直接アクセスできません）",
        "en": "Does not exist (cannot be accessed directly from this PC)",
    },
    "workdir.unreadable": {
        "ja": "アクセスできません（読み取り不可）",
        "en": "Not accessible (cannot be read)",
    },
    "workdir.readonly": {
        "ja": "読み取り専用（書き込み不可）",
        "en": "Read-only (cannot be written)",
    },
    "workdir.state": {"ja": "状態: {label}", "en": "Status: {label}"},
    "workdir.redirect_note": {
        "ja": "※書き込みが必要な処理は既定フォルダ（{default}）を自動的に使用します。",
        "en": "* Anything that needs to write files will automatically use the default folder ({default}).",
    },
    "workdir.setting_label": {"ja": "作業ディレクトリ", "en": "Working directory"},
    "workdir.setting_placeholder": {
        "ja": "例: C:\\Users\\me\\project（未入力なら既定値 {default} を使用）",
        "en": "e.g. C:\\Users\\me\\project (leave empty to use the default {default})",
    },
    "workdir.probe_timeout": {
        "ja": "サーバー（Locohaneが動いているPC）からフォルダの確認が時間内に終わりませんでした: {path}",
        "en": "The server (the PC running Locohane) could not check the folder in time: {path}",
    },
    "workdir.probe_invalid": {
        "ja": "サーバー（Locohaneが動いているPC）からこのパスを解釈できません: {path}（{error}）",
        "en": "The server (the PC running Locohane) cannot interpret this path: {path} ({error})",
    },
    "workdir.not_exist": {
        "ja": "サーバー（Locohaneが動いているPC）から見て存在しないフォルダです: {path}",
        "en": "This folder does not exist on the server (the PC running Locohane): {path}",
    },
    "workdir.not_readable": {
        "ja": "サーバー（Locohaneが動いているPC）からこのフォルダを読み取れません: {path}",
        "en": "The server (the PC running Locohane) cannot read this folder: {path}",
    },
    "workdir.change_failed": {
        "ja": "⚠️ 作業ディレクトリを変更できませんでした。{error}",
        "en": "⚠️ Could not change the working directory. {error}",
    },
    # --- チャット開始時 ---
    "welcome.default": {
        "ja": "Locohaneを起動しました。利用可能なスキル: {skills}\n\n"
        "スクリプト実行時の作業ディレクトリは、入力欄の📁アイコンから指定できます。",
        "en": "Locohane is ready. Available skills: {skills}\n\n"
        "You can set the working directory for scripts from the 📁 icon in the input box.",
    },
    "welcome.no_skills": {"ja": "（なし）", "en": "(none)"},
    "list.separator": {"ja": "、", "en": ", "},
    # --- トークン使用量カード ---
    "usage.main": {"ja": "リクエスト1回あたり（main）", "en": "Per request (main)"},
    "usage.total": {"ja": "会話累計", "en": "Conversation total"},
    "usage.compaction": {"ja": "圧縮処理（直近1回）", "en": "Compaction (last run)"},
    # --- Step の表示名 ---
    "step.batch_suffix": {"ja": "（一括）", "en": " (batch)"},
    "step.group": {"ja": "グループ", "en": "group"},
    "step.batch_background": {
        "ja": "安全上限に達したため、このグループはバックグラウンドで実行を続けています"
        "（結果は check_dispatch_agent_job で取得します）。",
        "en": "The safety limit was reached, so this group keeps running in the background "
        "(its result is collected with check_dispatch_agent_job).",
    },
    # --- 実行中のシステム通知 ---
    "notice.session_reinit_failed": {
        "ja": "セッションの再初期化に失敗しました。反応がなければページを再読み込みしてください。",
        "en": "Failed to reinitialize the session. If nothing responds, please reload the page.",
    },
    "notice.input_too_long": {
        "ja": "入力が長すぎます（{total:,}文字 / 上限{limit:,}文字）。内容を減らして再送信してください。",
        "en": "Your input is too long ({total:,} / {limit:,} characters). Please shorten it and send again.",
    },
    "notice.compaction_start": {
        "ja": "会話が長くなったため、履歴を要約して整理しています（数十秒〜数分かかる場合があります）…",
        "en": "The conversation has grown long, so the history is being summarized "
        "(this may take from tens of seconds to a few minutes)…",
    },
    "notice.compaction_done": {"ja": "会話履歴の整理が完了しました。", "en": "The conversation history has been summarized."},
    "notice.compaction_skipped": {
        "ja": "会話履歴の整理は対象外だったためスキップしました。",
        "en": "The conversation history did not need summarizing, so it was skipped.",
    },
    "notice.subagent_truncated": {
        "ja": "サブエージェントのタスクが打ち切られました（反復回数や応答内容の上限に到達したため）。"
        "ここまでに収集できた結果を踏まえて続行します。",
        "en": "The sub-agent task was cut short (it reached the iteration or response limit). "
        "Continuing with the results collected so far.",
    },
    "notice.generated_files": {"ja": "生成ファイル: {names}", "en": "Generated files: {names}"},
    "notice.plan_denied": {
        "ja": "実行計画が却下されたため、処理を終了しました。ご指示をお待ちしています。",
        "en": "The plan was rejected, so processing has stopped. Waiting for your instructions.",
    },
    "notice.recursion_limit": {
        "ja": "反復上限（recursion_limit={limit}）に達したため、処理を打ち切りました。"
        "タスクを分割するか、別のアプローチを試してください。",
        "en": "Processing stopped because the iteration limit (recursion_limit={limit}) was reached. "
        "Try splitting the task or taking a different approach.",
    },
    "notice.tool_call_loop": {
        "ja": "全く同じツール呼び出しが繰り返され、{retries}回リトライしましたが改善しなかったため停止しました。\n\n{detail}",
        "en": "The exact same tool call kept repeating and {retries} retries did not help, so processing stopped."
        "\n\n{detail}",
    },
    "notice.generation_loop": {
        "ja": "生成がループし、{retries}回リトライしましたが改善しなかったため停止しました。",
        "en": "The generation got stuck in a loop and {retries} retries did not help, so processing stopped.",
    },
    "notice.connection_error": {
        "ja": "通信エラーのため中断しました。 少し待って「続けて」と送信してください。",
        "en": "Stopped because of a connection error. Wait a moment and send \"continue\".",
    },
    "notice.other_thread_busy": {
        "ja": "他の会話が処理中です。完了してから送信してください。",
        "en": "Another conversation is being processed. Please send after it finishes.",
    },
    # --- 実行計画・質問（src/tools/） ---
    "plan.heading": {"ja": "### 実行計画", "en": "### Plan"},
    "plan.approve_question": {
        "ja": "この計画を承認しますか？承認後は各ステップの書き込み系ツール"
        "（run_script/execute_python_code）が実行できるようになります。",
        "en": "Do you approve this plan? Once approved, the write tools for each step "
        "(run_script/execute_python_code) can run.",
    },
    "plan.approve_button": {"ja": "✅ 計画を承認", "en": "✅ Approve plan"},
    "plan.deny_button": {"ja": "🚫 却下", "en": "🚫 Reject"},
    "plan.auto_approved": {
        "ja": "⚙️ config.ini の [plan].auto_approve が有効なため、ユーザー確認をスキップして計画を自動承認しました。",
        "en": "⚙️ [plan].auto_approve is enabled in config.ini, so the plan was approved automatically "
        "without asking.",
    },
    "ask.cancel": {"ja": "❌ キャンセル", "en": "❌ Cancel"},
    "ask.other": {"ja": "✏️ その他（自由入力）", "en": "✏️ Other (free text)"},
    "ask.none_selected": {"ja": "(選択なし)", "en": "(nothing selected)"},
    # --- サブエージェントの進捗（src/tools/_dispatch_agent_job.py・dispatch_agent_batch.py） ---
    "activity.thinking": {"ja": "思考中", "en": "Thinking"},
    "activity.tools": {"ja": "ツール: {names}", "en": "Tool: {names}"},
    "activity.compacting": {"ja": "会話履歴の圧縮中", "en": "Compacting conversation history"},
    "activity.llm_wait": {"ja": "LLMの空き待ち", "en": "Waiting for a free LLM"},
    "activity.wrap": {"ja": "【{activity}】", "en": " [{activity}]"},
    "progress.waiting": {
        "ja": "順番待ちです（他のサブエージェントの完了待ち・待機 {waited} 秒・job_id={job_id}）。",
        "en": "Waiting in line (for other sub-agents to finish; waited {waited} s, job_id={job_id}).",
    },
    "progress.running": {
        "ja": "実行中です（経過 {elapsed} 秒・反復 {iteration}/{max_iterations} 回・job_id={job_id}）。",
        "en": "Running (elapsed {elapsed} s, iteration {iteration}/{max_iterations}, job_id={job_id}).",
    },
    "progress.note_tail": {"ja": "[進捗メモ（末尾）]\n{tail}", "en": "[Progress notes (tail)]\n{tail}"},
    "progress.batch": {
        "ja": "一括実行中です（経過 {elapsed} 秒・全{total}グループ: 完了 {done}・実行中 {running}・順番待ち {waiting}）。",
        "en": "Batch running (elapsed {elapsed} s, {total} groups: done {done}, running {running}, waiting {waiting}).",
    },
    "progress.batch_group": {
        "ja": "- グループ{index}: 反復 {iteration}/{max_iterations} 回（job_id={job_id}）",
        "en": "- Group {index}: iteration {iteration}/{max_iterations} (job_id={job_id})",
    },
    # --- ツールの表示名（src/tool_labels.py） ---
    "tool.read_skill": {"ja": "スキル読み込み", "en": "Read skill"},
    "tool.read_skill_file": {"ja": "ファイル読み込み", "en": "Read file"},
    "tool.run_script": {"ja": "スクリプト実行", "en": "Run script"},
    "tool.analyze_image": {"ja": "画像解析", "en": "Analyze image"},
    "tool.dispatch_agent": {"ja": "サブエージェント実行", "en": "Run sub-agent"},
    "tool.dispatch_agent_batch": {"ja": "サブエージェント一括実行", "en": "Run sub-agents (batch)"},
    "tool.AskUserQuestion": {"ja": "ユーザーへの質問（自由記述）", "en": "Ask user (free text)"},
    "tool.ask_user_choice": {"ja": "ユーザーへの質問（選択式）", "en": "Ask user (choices)"},
    "tool.create_plan": {"ja": "実行計画作成", "en": "Create plan"},
    "tool.approve_plan": {"ja": "計画承認確認", "en": "Request plan approval"},
    "tool.update_task_progress": {"ja": "進捗更新", "en": "Update progress"},
    "tool.get_plan_status": {"ja": "計画状態確認", "en": "Check plan status"},
    "tool.lock_plan_mode": {"ja": "Plan Modeへ復帰", "en": "Return to Plan Mode"},
    "tool.create_memory": {"ja": "メモリー保存", "en": "Save memory"},
    "tool.update_memory": {"ja": "メモリー更新", "en": "Update memory"},
    "tool.delete_memory": {"ja": "メモリー削除", "en": "Delete memory"},
    "tool.read_memory": {"ja": "メモリー読み込み", "en": "Read memory"},
    "tool.search_memory": {"ja": "メモリー検索", "en": "Search memory"},
    "tool.list_memories": {"ja": "メモリー一覧", "en": "List memories"},
    "tool.help": {"ja": "ヘルプ表示", "en": "Show help"},
}
