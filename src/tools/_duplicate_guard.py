"""読み取り専用ツールの重複呼び出しガード。"""

from __future__ import annotations

import chainlit as cl

from . import _state
from ._state import _duplicate_guard_session_key


def _check_file_tools_duplicate(tool_label: str, signature: str) -> str | None:
    """Read/Glob/Grep/json_query/read_skill/read_skill_file/get_tool_source 共通の重複呼び出しガード。

    旧 _run_script_impl の file-tools 分岐（読み取り専用スキルのため、
    同一引数での再実行は結果が変わらないと保証できる）を汎用化したもの。
    config.ini [file_tools_duplicate_guard] の enabled/max_calls/
    carry_over_to_main をそのまま参照する。

    read_skill/read_skill_file はコンテキスト圧縮でメッセージ履歴から
    過去の実行結果が消えた後もLLMが同じ大きいSKILL.md等を何度も読み直す
    thrashingを防ぐ目的でも使う（2026-08-10 issue参照）。この記録は
    cl.user_session（LLMへ送るメッセージ履歴とは別のサーバー側状態）に
    残るため、圧縮でメッセージ履歴が消えてもガードの記憶自体は消えない。

    Args:
        tool_label: エラー文言に出すツール名（例: "Read"）。
        signature: 呼び出し引数から組み立てた、その呼び出しを一意に表す文字列。

    Returns:
        重複と判定されればエラー文字列、そうでなければ None
        （None の場合、呼び出し元は通常通り処理を続けてよい）。
    """
    cfg = _state._LLM_CONFIG
    guard_enabled = cfg.file_tools_duplicate_guard_enabled if cfg else True
    if not guard_enabled:
        return None
    guard_max_calls = cfg.file_tools_duplicate_guard_max_calls if cfg else 1
    session_key = _duplicate_guard_session_key("file_tools_call_signatures")
    if _record_and_check_duplicate(session_key, signature, guard_max_calls):
        return (
            f"Error: {tool_label} has already been called with the same arguments up to the limit ({guard_max_calls} times). "
            f"{tool_label} is read-only, so running it again does not change the result. "
            "Refer to the previous result in the conversation history, or "
            "switch to different arguments or a different approach."
        )
    return None

def _record_and_check_duplicate(session_key: str, signature: str, max_calls: int = 1) -> bool:
    """同一シグネチャでの呼び出しが会話（セッション）中に上限回数まで既にあったかを判定し、記録する。

    小型ローカルモデルは、進捗が無いまま同一引数のツール呼び出しを会話全体を
    通じて散発的に繰り返すことがある（tune-prompt調査で glob_file.py の同一引数
    2回連続呼び出し、analyze_image で同じ画像を14回重複して呼ぶ実例あり）。直前1回
    との比較だけでは検知できないため、会話（thread）単位で見たシグネチャごとの
    呼び出し回数を cl.user_session に保持し、上限に達しているかどうかで判定する。

    Args:
        session_key: cl.user_session に保存する呼び出し回数辞書のキー
            （呼び出し元ごとに別集合にするため、対象を含める）。
        signature: 呼び出し引数から組み立てた、その呼び出しを一意に表す文字列。
        max_calls: 同一シグネチャを何回まで許可するか（既定1回。
            [file_tools_duplicate_guard].max_calls 等、呼び出し元が設定値を
            渡す場合がある）。

    Returns:
        True であれば、このシグネチャは今回のセッション内で既に上限回数まで
        記録済み（＝今回は重複呼び出し、拒否すべき）。False であれば
        まだ上限に達していない（呼び出し元はその旨をこの関数が記録済みなので、
        通常通り処理を続けてよい）。
    """
    if max_calls <= 0:
        # 0以下は「無制限」を表す（config.ini側の運用上の逃げ道。ガードが
        # モデルの挙動と噛み合わずループ等を起こす場合にここで無効化できる）。
        return False
    counts = cl.user_session.get(session_key)
    if counts is None:
        counts = {}
    count = counts.get(signature, 0)
    is_duplicate = count >= max_calls
    counts[signature] = count + 1
    cl.user_session.set(session_key, counts)
    return is_duplicate


def reset_call_history_guards_after_compaction() -> None:
    """コンテキスト圧縮（要約）で古い会話履歴が消えた際、Read/Glob等の
    重複呼び出しガードが記録しているシグネチャ履歴も合わせてリセットする。

    _check_file_tools_duplicate / analyze_image の重複ガードは
    cl.user_session に会話（thread）全体を通じた呼び出し履歴を保持し続ける
    仕組みだが、要約後のモデルは要約に含まれなかった個々の呼び出し
    （どのファイルをどの引数で読んだか等）を覚えていない。記憶が無いのに
    ガードだけが「既に呼び出し済み」として拒否し続けると、モデルは
    エラーの理由を理解できないまま同じような呼び出しを繰り返し、
    抜け出せないループに陥る。要約が確定した直後に呼ばれる想定
    （app.py・src/subagent.py の圧縮成功パス。token_usage_cumulative_main の
    リセットと同様の位置づけ）。記録先キーは _duplicate_guard_session_key() で
    決まるため、サブエージェント内から呼ぶとそのサブエージェントの記録が消える。
    """
    cl.user_session.set(_duplicate_guard_session_key("file_tools_call_signatures"), None)
    cl.user_session.set(_duplicate_guard_session_key("analyze_image_call_signatures"), None)


_FAILURE_STREAK_THRESHOLD = 4

def _track_failure_streak(session_key: str, failed: bool, tool_label: str) -> str:
    """同一ツールの連続失敗回数を cl.user_session に記録し、閾値超過時は警告文を返す。

    小型ローカルモデルは、同じコード・引数の微修正を繰り返すだけで根本的な
    アプローチを変えないまま失敗を重ねることがある（tune-prompt調査で
    execute_python_code が13回連続で構文エラーを繰り返した実例あり）。
    system_prompt.md の「連続失敗3回で切り替える」という自己申告ルールだけ
    では守られないことがあるため、ツール自体が連続失敗回数を数え、閾値を
    超えたら結果メッセージに強制的な警告を追記する（会話（thread）単位で
    独立させるため cl.user_session を使う）。

    Args:
        session_key: cl.user_session に保存するカウンタのキー
            （ツールごとに別カウンタにするため、ツール名を含める）。
        failed: 今回の実行が失敗（非0終了コード）だったか。
        tool_label: 警告文に出すツール名（例: "execute_python_code"）。

    Returns:
        閾値に達していれば先頭に改行を含む警告文、そうでなければ空文字列
        （空文字列は呼び出し側で無視してよい）。
    """
    if not failed:
        cl.user_session.set(session_key, 0)
        return ""
    streak = cl.user_session.get(session_key, 0) + 1
    cl.user_session.set(session_key, streak)
    if streak < _FAILURE_STREAK_THRESHOLD:
        return ""
    return (
        f"\n\n[System warning] {tool_label} has failed {streak} times in a row. "
        "Stop patching the same code/arguments little by little; switch to a fundamentally different way of writing it or "
        "a different approach, or consider an alternative without sticking to this one."
    )
