"""run_script のagent_typeごとのスキル制限ガード（_AGENT_TYPE_RUN_SCRIPT_ALLOWLIST）の回帰テスト。

背景（eval case 006, 2026-08-15）: `explore-websearch`はプロンプト上「run_scriptは
web-searchスキルのsearch_web.pyに限って呼べる」と規定されているが、コード側の
強制が無かったため、低パラメータモデルが指示を無視してexcel-readのread_excel.py
を呼び、本来explore向けの誤診断防止ルールが適用されないままxlsx調査が
進んでしまう事象が実際に発生した。_prepare_script_execution冒頭のガードが
これをコード側で強制することを検証する。

同じ理由で、explore/verifierは「書き込み系スクリプトは絶対に呼び出さない」と
プロンプト上で強く約束しているが、プロンプトの記述だけでは同種のリスクを抱える
ため、2026-08-24に両agent_typeもこのガード対象へ追加した（2026-08-27、旧
analyze-docsはexploreへ統合）。pdf-toolsスキルは読み込み専用スクリプト
（read_pdf.py/render_pdf_pages.py）と書き込みスクリプト（create_pdf.py）が
同一スキル配下に同居するため、スキル名単位ではなく(スキル名, スクリプトファイル名)
のタプル単位で許可している点も併せて検証する。
"""

from src import tools


def _set_agent_type(agent_type):
    return tools._state._SUBAGENT_AGENT_TYPE.set(agent_type)


def _call_past_guard(skill_name: str, script_filename: str):
    """ガードを通過したことだけを確認する呼び出し。

    init_tools() 未実行のテスト環境では _resolve_script_filename 以降が
    RuntimeError/ValueError で失敗するが、それ自体がこのガードで
    ブロックされなかった証拠になる（ブロックされていれば例外を投げず
    「エラー: ...限定されています」という文字列を返して即座に戻るため）。
    """
    try:
        return tools._script_job._prepare_script_execution(skill_name, script_filename, ["dummy"])
    except (RuntimeError, ValueError) as e:
        return f"passed_guard_then_failed_downstream: {e}"


def test_no_agent_type_context_is_unrestricted():
    # メインエージェント（_SUBAGENT_AGENT_TYPE未設定）はこのガードの対象外。
    result = _call_past_guard("excel-read", "read_excel.py")
    assert "限定されています" not in result


def test_worker_agent_type_is_unrestricted():
    token = _set_agent_type("worker")
    try:
        result = _call_past_guard("excel-read", "read_excel.py")
    finally:
        tools._state._SUBAGENT_AGENT_TYPE.reset(token)

    assert "限定されています" not in result


def test_explore_allowed_read_only_skill_passes_allowlist_guard():
    token = _set_agent_type("explore")
    try:
        result = _call_past_guard("excel-read", "read_excel.py")
    finally:
        tools._state._SUBAGENT_AGENT_TYPE.reset(token)

    assert "限定されています" not in result


def test_explore_blocked_from_write_skill():
    # explore.md は「edit_excel.py 等の書き込み系スクリプトは絶対に呼び出さない」
    # とプロンプト上で約束しているが、これをコード側でも強制する。
    token = _set_agent_type("explore")
    try:
        result = tools._script_job._prepare_script_execution("excel-edit", "edit_excel.py", ["dummy.xlsx"])
    finally:
        tools._state._SUBAGENT_AGENT_TYPE.reset(token)

    assert isinstance(result, str)
    assert result.startswith("Error")
    assert "explore" in result


def test_explore_allowed_pdf_tools_read_only_script_passes_allowlist_guard():
    token = _set_agent_type("explore")
    try:
        result = _call_past_guard("pdf-tools", "read_pdf.py")
    finally:
        tools._state._SUBAGENT_AGENT_TYPE.reset(token)

    assert "限定されています" not in result


def test_explore_blocked_from_pdf_tools_write_script():
    # pdf-tools スキル配下には read_pdf.py/render_pdf_pages.py（読み込み専用）と
    # create_pdf.py（書き込み）が同居するため、スキル名単位の許可だと
    # create_pdf.py まで通ってしまう。(スキル名, スクリプト名)単位で
    # ブロックされることを確認する。
    token = _set_agent_type("explore")
    try:
        result = tools._script_job._prepare_script_execution("pdf-tools", "create_pdf.py", ["dummy"])
    finally:
        tools._state._SUBAGENT_AGENT_TYPE.reset(token)

    assert isinstance(result, str)
    assert result.startswith("Error")


def test_verifier_allowed_read_only_skill_passes_allowlist_guard():
    token = _set_agent_type("verifier")
    try:
        result = _call_past_guard("docx-read", "read_docx.py")
    finally:
        tools._state._SUBAGENT_AGENT_TYPE.reset(token)

    assert "限定されています" not in result


def test_verifier_blocked_from_write_skill():
    token = _set_agent_type("verifier")
    try:
        result = tools._script_job._prepare_script_execution("docx-edit", "edit_docx.py", ["dummy.docx"])
    finally:
        tools._state._SUBAGENT_AGENT_TYPE.reset(token)

    assert isinstance(result, str)
    assert result.startswith("Error")
    assert "verifier" in result


def test_verifier_blocked_from_pdf_tools_write_script():
    token = _set_agent_type("verifier")
    try:
        result = tools._script_job._prepare_script_execution("pdf-tools", "create_pdf.py", ["dummy"])
    finally:
        tools._state._SUBAGENT_AGENT_TYPE.reset(token)

    assert isinstance(result, str)
    assert result.startswith("Error")
