"""Chainlit の UI 呼び出しをスタブ化し、Chainlit サーバー無しで
src.tools のツール群（run_script / execute_python_code / AskUserQuestion /
ask_user_choice / create_plan / approve_plan / update_task_progress）を
そのまま ainvoke できるようにする。

src.tools は `import chainlit as cl` 済みだが、`cl.xxx` への参照は
呼び出し時に解決される（モジュール属性ルックアップ）ため、install() を
src.tools の関数を呼ぶより前に一度実行しておけば十分。差し替えは
tools.py が実際に使っている範囲（user_session / AskActionMessage /
AskUserMessage / CustomElement / AskElementMessage / Message）に限定し、
cl.Action はコンストラクタが副作用を持たないためそのまま使う。
加えて、approve_plan/ask_user_choice/ask_user_question の3モジュールが
個別に import 済みの `_ask_with_cross_session_relay`（スレッド切り替え後の
別セッションからの回答中継を待つ本番用ロジック）も、headlessモードには
「別セッション」概念が無く中継先が永久に解決されないため、factory() を
直接呼ぶだけの版へ差し替える（patch_ask_relay() 参照。install() 単体では
不十分で、init_tools() 実行後に get_all_tools() の戻り値へ対して呼ぶ必要が
ある。run_case.py 参照）。

1プロセス=1ケース実行という前提のため、元の chainlit 属性へ戻す処理は
用意しない（run_case.py はケースごとに新規プロセスとして起動される）。
"""

from __future__ import annotations

import chainlit as cl

_state = {"auto_approve": True, "scripted_text_answers": []}


class _FakeUserSession:
    """cl.user_session の代替。プロセス内 dict だけを保持する。"""

    def __init__(self) -> None:
        self._store: dict = {}

    def get(self, key, default=None):
        return self._store.get(key, default)

    def set(self, key, value) -> None:
        self._store[key] = value


class _FakeAskActionMessage:
    """cl.AskActionMessage の代替。承認/拒否ダイアログを即答する。

    auto_approve が True なら payload.value == "approve" のアクションを選ぶ
    （run_script/execute_python_code/approve_plan の承認ダイアログ用）。
    False ならそれ以外（"deny" 等）を選ぶ。ask_user_choice のように
    approve/deny という値を持たない選択肢のみの場合は、その中から
    auto_approve=True なら先頭、False なら末尾を選ぶ。
    """

    def __init__(self, content: str = "", actions=None, timeout: int = 0, **kwargs) -> None:
        self.content = content
        self.actions = actions or []

    async def send(self):
        if not self.actions:
            return None
        target = "approve" if _state["auto_approve"] else "deny"
        for action in self.actions:
            payload = getattr(action, "payload", None) or {}
            if payload.get("value") == target:
                return {"payload": payload, "label": getattr(action, "label", "")}
        chosen = self.actions[0] if _state["auto_approve"] else self.actions[-1]
        payload = getattr(chosen, "payload", None) or {}
        return {"payload": payload, "label": getattr(chosen, "label", "")}

    async def update(self):
        # src/tools/_ask_echo.py が回答後に質問文へ戻すために呼ぶ。
        return self


class _FakeAskUserMessage:
    """cl.AskUserMessage の代替。scripted_text_answers を順に1件ずつ消費する。

    尽きた場合は None を返す（本物のタイムアウトと同じ扱いにし、
    AskUserQuestion 側の「エラー: ユーザーからの応答がありませんでした」
    という分岐も正当なテスト対象にする）。
    """

    def __init__(self, content: str = "", timeout: int = 0, **kwargs) -> None:
        self.content = content

    async def send(self):
        answers = _state["scripted_text_answers"]
        if answers:
            return {"output": answers.pop(0)}
        return None


class _FakeCustomElement:
    """cl.CustomElement の代替。name/props を保持するだけで副作用は持たない。"""

    def __init__(self, name: str = "", props: dict | None = None, **kwargs) -> None:
        self.name = name
        self.props = props or {}


class _FakeAskElementMessage:
    """cl.AskElementMessage の代替。AskUserQuestion(labels指定時)の
    "MultiTextForm" と ask_user_choice(multi_select=True)の "MultiChoiceForm"
    のみを扱う（tools.py が生成する element.name はこの2種類のみのため）。

    MultiTextForm: labels 1件につき scripted_text_answers を1件ずつ消費し、
    値のリストを返す。件数が足りなくなった時点でタイムアウト扱い（None）に
    する（_FakeAskUserMessage と同じ規約）。
    MultiChoiceForm: _FakeAskActionMessage と同じ auto_approve 規約に倣い、
    auto_approve=True なら choices の先頭1件を選択、False なら未選択のまま
    キャンセル扱い（submitted=False）にする。
    """

    def __init__(self, content: str = "", element=None, timeout: int = 0, **kwargs) -> None:
        self.content = content
        self.element = element

    async def send(self):
        props = getattr(self.element, "props", {}) or {}
        name = getattr(self.element, "name", "")
        if name == "MultiTextForm":
            labels = props.get("labels") or []
            answers = _state["scripted_text_answers"]
            if len(answers) < len(labels):
                return None
            values = [answers.pop(0) for _ in labels]
            return {"values": values}
        if name == "MultiChoiceForm":
            choices = props.get("choices") or []
            if not choices or not _state["auto_approve"]:
                return {"submitted": False}
            return {"submitted": True, "values": [choices[0]], "other": ""}
        return None

    async def update(self):
        # src/tools/_ask_echo.py が回答後に質問文へ戻すために呼ぶ。
        return self


async def _fake_ask_with_cross_session_relay(thread_id: str, factory, timeout: int):
    """src.tools._ask_relay_helper._ask_with_cross_session_relay の代替。

    本物は「スレッド切り替え後の別セッションから回答が中継されてくる」ケースを
    asyncio.wait(FIRST_COMPLETED) で待つが、headlessモードには「別セッション」
    という概念自体が無いため中継先（relay_future）が永久に解決されない。
    config.ini の [timeouts] ask_user_question_seconds 等が 0（無期限待ち＝
    _resolve_ask_timeout により 2**31-1 秒に変換される）の環境では、
    scripted_text_answers が尽きた・想定外の ask 系ツール呼び出しが発生した
    場合に事実上無限にハングしていた（2026-09-02 発覚）。headlessモードでは
    中継を待つ意味が無いため、factory() の結果をそのまま返す。
    """
    return await factory()


class _FakeMessage:
    """cl.Message の代替。create_plan/update_task_progress からの表示更新のみを吸収する。"""

    def __init__(self, content: str = "", **kwargs) -> None:
        self.content = content

    async def send(self):
        return self

    async def update(self):
        return self

    async def stream_token(self, token: str) -> None:
        self.content += token


def install(auto_approve: bool = True, scripted_text_answers: list[str] | None = None) -> None:
    """chainlit モジュールの属性をヘッドレス用スタブへ差し替える。

    run_case.py の冒頭、src.tools の関数を呼ぶより前に一度だけ呼ぶ。

    Args:
        auto_approve: run_script/execute_python_code/approve_plan の
            承認ダイアログを自動で承認するか拒否するか。
        scripted_text_answers: AskUserQuestion が呼ばれるたびに消費して返す
            回答のリスト。labels 省略時（単一質問）は1件、labels 指定時は
            その件数だけまとめて消費する。尽きればタイムアウト扱い（None）。
    """
    _state["auto_approve"] = auto_approve
    _state["scripted_text_answers"] = list(scripted_text_answers or [])

    cl.user_session = _FakeUserSession()
    cl.AskActionMessage = _FakeAskActionMessage
    cl.AskUserMessage = _FakeAskUserMessage
    cl.CustomElement = _FakeCustomElement
    cl.AskElementMessage = _FakeAskElementMessage
    cl.Message = _FakeMessage


_ASK_RELAY_TOOL_NAMES = ("approve_plan", "ask_user_choice", "AskUserQuestion")


def patch_ask_relay(tools) -> None:
    """approve_plan/ask_user_choice/AskUserQuestion の
    `_ask_with_cross_session_relay`（スレッド切り替え後の別セッションからの
    回答中継を待つ本番用ロジック）を、headlessモード向けの即時解決版へ
    差し替える。init_tools() 実行後、get_all_tools() が返す実際のツール
    オブジェクトに対して呼ぶこと（run_case.py 参照）。

    3モジュールが `from ._ask_relay_helper import _ask_with_cross_session_relay`
    で名前を自分の名前空間へ直接束縛しているため、
    `src.tools._ask_relay_helper` 側や `sys.modules['src.tools.xxx']` 経由の
    属性差し替えでは反映されないことがある（2026-09-02、init_tools() 実行後に
    該当モジュールの sys.modules エントリが差し替え前と異なるオブジェクトに
    なっている事例を確認した。原因不明だが、下記のように呼び出し対象の
    StructuredTool 自身が保持する coroutine 関数の `__globals__` を直接
    書き換える方式であれば、実際に呼ばれる関数と同じ名前空間を確実に
    更新できる）。

    Args:
        tools: get_all_tools() の戻り値（BaseTool のリスト）。
    """
    for t in tools:
        if t.name not in _ASK_RELAY_TOOL_NAMES:
            continue
        coroutine = getattr(t, "coroutine", None)
        if coroutine is None or not hasattr(coroutine, "__globals__"):
            continue
        coroutine.__globals__["_ask_with_cross_session_relay"] = _fake_ask_with_cross_session_relay
