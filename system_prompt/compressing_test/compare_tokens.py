#!/usr/bin/env python3
"""
system_prompt/compressing_test 内の md ファイルの入力トークン数比較スクリプト

- original/system_prompt.md を基準として、compressing_test 直下の各 md
  ファイルを system プロンプトとして実際に LLM（llama-server）へ送信し、
  同一の簡単な質問と合わせたレスポンスの usage.prompt_tokens を比較・集計する。
- 接続先は config.ini の [llm] main_url（1件目）を使う。事前に llama-server
  を起動しておくこと。
"""

from __future__ import annotations

import ast
import configparser
import sys
from pathlib import Path

from openai import OpenAI

# Windows のコンソール（既定cp932）でも日本語出力が文字化けしないようにする。
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.ini"
ORIGINAL = BASE_DIR / "original" / "system_prompt.md"

# 全ファイル共通で送る簡単な質問。system プロンプト側の差分だけを
# 比較対象にするため、内容はどのファイルでも固定する。
TEST_QUESTION = "こんにちは。あなたの役割を一文で教えてください。"

REQUEST_TIMEOUT_SECONDS = 120.0


def load_endpoint() -> tuple[str, str, str]:
    """config.ini の [llm] main_url（1件目）から接続先を読み取る。"""
    parser = configparser.ConfigParser()
    if not parser.read(CONFIG_PATH, encoding="utf-8"):
        print(f"Error: {CONFIG_PATH} not found.", file=sys.stderr)
        sys.exit(1)
    raw = parser.get("llm", "main_url")
    endpoints = ast.literal_eval(raw)
    first = endpoints[0]
    return str(first["base_url"]), str(first.get("api_key") or "dummy-not-used"), str(first["model"])


def count_prompt_tokens(client: OpenAI, model: str, system_text: str) -> int:
    """system_text をsystemプロンプトにして質問を送信し、prompt_tokensを返す。"""
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_text},
            {"role": "user", "content": TEST_QUESTION},
        ],
        max_tokens=1,
    )
    if response.usage is None:
        raise RuntimeError("レスポンスに usage 情報が含まれていません（llama-server起動時に有効化されているか確認してください）")
    return response.usage.prompt_tokens


def main():
    if not ORIGINAL.exists():
        print(f"Error: {ORIGINAL} not found.", file=sys.stderr)
        sys.exit(1)

    base_url, api_key, model = load_endpoint()
    print(f"接続先: {base_url} (model={model})")
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)

    try:
        base_tokens = count_prompt_tokens(client, model, ORIGINAL.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error: 基準ファイルの送信に失敗しました（llama-serverが起動しているか確認してください）: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"=== 基準ファイル: {ORIGINAL.name} ({base_tokens:,} トークン) ===")
    print()

    targets = sorted(BASE_DIR.glob("*.md"))
    targets = [t for t in targets if t != ORIGINAL]

    if not targets:
        print("比較対象のmdファイルが見つかりません。")
        return

    print(f"{'ファイル名':<45} {'トークン数':>10} {'差':>8} {'比率':>8}")
    print("-" * 76)
    for f in targets:
        n = count_prompt_tokens(client, model, f.read_text(encoding="utf-8"))
        diff = n - base_tokens
        ratio = (n / base_tokens * 100) if base_tokens else 0
        print(f"{f.name:<45} {n:>10,} {diff:>+8,} {ratio:>7.1f}%")

    print("-" * 76)
    print(f"基準: {base_tokens:,} トークン")


if __name__ == "__main__":
    main()
