"""画像付きメッセージ（analyze_image の画像フォローアップ）のコンテキスト削減の回帰テスト。

2026-10-01、006 レシピ画像ケースで発覚:
- 要約・thread note 強制書き出しの入力テキスト化で base64 画像をそのまま str() し、
  1回のリクエストが1000万トークン規模になってコンテキスト長超過で必ず失敗していた。
  画像は元画像の参照（`@N 絶対パス`）に置き換える。
- トリムで古い画像も消す対策は、読み直しが重複ガードに拒否されて処理が欠落する
  退行を招いたため取り消した（画像はトリムせず、圧縮予告→圧縮で扱う）。
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai.chat_models.base import _convert_message_to_dict

from src.context_compaction import _messages_to_text
from src.context_trim import trim_old_tool_messages
from src.images import IMAGE_REFS_KEY, image_followup_message

_DATA_URL = "data:image/jpeg;base64," + "A" * 100_000


def _image_iteration(call_id: str) -> list:
    return [
        AIMessage(content="", tool_calls=[{"name": "analyze_image", "args": {}, "id": call_id}]),
        ToolMessage(content="画像を読み込みました", name="analyze_image", tool_call_id=call_id),
        HumanMessage(content=[{"type": "image_url", "image_url": {"url": _DATA_URL}}]),
    ]


def test_messages_to_text_replaces_images_with_refs() -> None:
    text = _messages_to_text(
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": "説明"},
                    {"type": "image_url", "image_url": {"url": _DATA_URL}},
                    {"type": "image_url", "image_url": {"url": _DATA_URL}},
                ],
                additional_kwargs={IMAGE_REFS_KEY: [r"@3 C:\img\a.jpg", r"C:\img\b.jpg"]},
            ),
            # 参照を持たない古い履歴の画像
            HumanMessage(content=[{"type": "image_url", "image_url": {"url": _DATA_URL}}]),
        ]
    )

    assert "base64" not in text
    assert "説明" in text
    assert r"[image: @3 C:\img\a.jpg]" in text and r"[image: C:\img\b.jpg]" in text
    assert "[image]" in text


def test_image_followup_carries_ref_but_does_not_send_it_to_llm() -> None:
    message = image_followup_message({"image_url": _DATA_URL, "ref": r"@3 C:\img\a.jpg"})

    assert message.additional_kwargs[IMAGE_REFS_KEY] == [r"@3 C:\img\a.jpg"]
    # 参照は要約用のメタデータで、LLMへのリクエストには含めない
    assert "a.jpg" not in str(_convert_message_to_dict(message))


def test_trim_keeps_old_images() -> None:
    # 古い画像を消すと、書き出し前のモデルが再読込を試みて重複ガードに拒否され、
    # 処理が欠落する（006 で80枚が未処理になった）。トリムでは画像を残す。
    messages = [*_image_iteration("c1"), *_image_iteration("c2")]

    result = trim_old_tool_messages(messages, keep_recent_iterations=1, max_chars=50)

    assert result[2] is messages[2]
