"""単発のLLM呼び出しヘルパー。Locohane本体のPython実行環境で動かす前提。

evals.run_case のように ReAct ループ全体を回すのではなく、
`src.llm.build_model(config)` を使って1回だけ chat completion を叩く。
description 最適化ループ（propose_description.py）が「失敗したトリガー
クエリを踏まえて改善案を出す」ために使う。

このファイル自体は run_script（config.ini の [scripts].python、
Locohane本体とは別のPython環境）からは直接importされず、
propose_description.py がサブプロセスとして
`<MAIN_PYTHON> _llm_helper.py <input.json>` の形で起動する。

入力ファイル（JSON）: {"system": "...", "user": "..."}（system は省略可）
標準出力: {"text": "..."} または {"error": "..."}
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path


def _project_root() -> Path:
    # skills/skill-creator/scripts/_llm_helper.py -> parents[3] がプロジェクトルート
    return Path(__file__).resolve().parents[3]


async def _ask(payload: dict) -> dict:
    root = _project_root()
    sys.path.insert(0, str(root))
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415

    from src.config import load_config  # noqa: PLC0415
    from src.llm import build_model  # noqa: PLC0415

    config = load_config()
    model = await build_model(config)

    messages = []
    if payload.get("system"):
        messages.append(SystemMessage(content=payload["system"]))
    messages.append(HumanMessage(content=payload["user"]))

    response = await model.ainvoke(messages)
    content = response.content if isinstance(response.content, str) else str(response.content)
    return {"text": content}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python _llm_helper.py <input.json>", file=sys.stderr)
        return 2

    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:
        result = asyncio.run(_ask(payload))
    except Exception as e:  # noqa: BLE001 - 呼び出し元へ理由を伝えるため意図的に広く捕捉
        print(json.dumps(
            {"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()},
            ensure_ascii=False,
        ))
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
