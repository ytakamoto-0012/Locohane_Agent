"""推論サーバーの reasoning（thinking）方言を実測する検証スクリプト。

llama-server と vLLM で、thinking まわりのフィールド名・パラメータの扱いが
異なる（src/llm/dialect.py 参照）。本スクリプトは同じ手順を任意の
OpenAI互換サーバーへ当て、次の3点を実測して表にする。

1. 応答フィールド: stream / 非stream で thinking が `reasoning` と
   `reasoning_content` のどちら（または両方）に入るか。
2. 履歴フィールドの反映: assistant 履歴に `reasoning_content` のみ／
   `reasoning` のみ／両方を入れたとき、テンプレート展開後のプロンプトに
   前ターンの thinking が何回現れるか（0=無視、1=反映、2=重複）。
   llama-server は POST /apply-template、vLLM は POST /tokenize →
   POST /detokenize で展開結果を得る。
3. パラメータの受理: reasoning 系・サンプラー系パラメータを単独で送り、
   HTTP ステータスと thinking / 本文の文字数を記録する。

使い方:
    python tools/probe_reasoning_dialect.py --base-url http://localhost:12430/v1 \
        --model <model> --provider llama_cpp [--json-out result.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

PRIOR_THINK_MARKER = "PRIOR_THINK_MARKER_7f3a"
QUESTION = "1+1は？数字だけ答えて。"

# (ラベル, extra_body) の組。ラベルは表の行名になる。
PARAM_CASES: list[tuple[str, dict[str, Any]]] = [
    ("baseline", {}),
    *[(f"reasoning_effort={v}", {"reasoning_effort": v})
      for v in ("none", "default", "minimal", "low", "medium", "high", "xhigh", "max")],
    ("chat_template_kwargs.enable_thinking=false", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("reasoning_budget=0", {"reasoning_budget": 0}),
    ("reasoning_budget=16", {"reasoning_budget": 16}),
    ("reasoning_budget_tokens=0", {"reasoning_budget_tokens": 0}),
    ("reasoning_budget_tokens=16", {"reasoning_budget_tokens": 16}),
    ("thinking_budget_tokens=0", {"thinking_budget_tokens": 0}),
    ("thinking_budget_tokens=16", {"thinking_budget_tokens": 16}),
    ("thinking_token_budget=16", {"thinking_token_budget": 16}),
    ("thinking_token_budget=0", {"thinking_token_budget": 0}),
    ("repeat_penalty=1.1", {"repeat_penalty": 1.1}),
    ("repetition_penalty=1.1", {"repetition_penalty": 1.1}),
    ("top_k=20", {"top_k": 20}),
    ("dry_multiplier=0.8", {"dry_multiplier": 0.8}),
    ("reasoning_format=deepseek", {"reasoning_format": "deepseek"}),
    ("reasoning_budget_message=x", {"reasoning_budget_tokens": 16, "reasoning_budget_message": "BUDGET_MSG_MARKER"}),
]

HISTORY_FIELD_CASES: list[tuple[str, dict[str, str]]] = [
    ("なし", {}),
    ("reasoning_content", {"reasoning_content": PRIOR_THINK_MARKER}),
    ("reasoning", {"reasoning": PRIOR_THINK_MARKER}),
    ("両方", {"reasoning_content": PRIOR_THINK_MARKER, "reasoning": PRIOR_THINK_MARKER}),
]

HISTORY_KWARGS_CASES: list[tuple[str, dict[str, Any] | None]] = [
    ("kwargs未指定", None),
    ("preserve_thinking=true", {"preserve_thinking": True}),
    ("preserve_thinking=false", {"preserve_thinking": False}),
    ("preserve_reasoning=true", {"preserve_reasoning": True}),
    ("preserve_reasoning=false", {"preserve_reasoning": False}),
]


def _server_root(base_url: str) -> str:
    """OpenAI互換 base_url（…/v1）からサーバーのルートURLを得る。"""
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def _error_text(resp: httpx.Response) -> str:
    return resp.text.replace("\n", " ")[:160]


def _post(client: httpx.Client, url: str, body: dict[str, Any], retries: int = 12) -> httpx.Response:
    """POST する。llama-server はテンプレート例外（500）の後にモデルを再ロード
    することがあり、その間は 503 を返すため、503 の間だけ待って再試行する。"""
    for _ in range(retries):
        resp = client.post(url, json=body)
        if resp.status_code != 503:
            return resp
        time.sleep(5)
    return resp


def probe_response_fields(client: httpx.Client, base_url: str, model: str, max_tokens: int) -> dict[str, Any]:
    """stream / 非stream で thinking がどのフィールドに入るかを調べる。"""
    body = {"model": model, "messages": [{"role": "user", "content": QUESTION}], "max_tokens": max_tokens}
    result: dict[str, Any] = {}

    resp = _post(client, f"{base_url}/chat/completions", body)
    if resp.status_code != 200:
        result["non_stream"] = {"status": resp.status_code, "error": _error_text(resp)}
    else:
        message = resp.json()["choices"][0]["message"]
        result["non_stream"] = {
            "status": 200,
            "reasoning_chars": len(message.get("reasoning") or ""),
            "reasoning_content_chars": len(message.get("reasoning_content") or ""),
            "content_chars": len(message.get("content") or ""),
        }

    counts = {"reasoning": 0, "reasoning_content": 0, "both_in_same_delta": 0}
    chars = {"reasoning": 0, "reasoning_content": 0}
    with client.stream("POST", f"{base_url}/chat/completions", json={**body, "stream": True}) as stream_resp:
        if stream_resp.status_code != 200:
            stream_resp.read()
            result["stream"] = {"status": stream_resp.status_code, "error": _error_text(stream_resp)}
            return result
        for line in stream_resp.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            choices = json.loads(payload).get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            present = [k for k in ("reasoning", "reasoning_content") if delta.get(k)]
            for k in present:
                counts[k] += 1
                chars[k] += len(delta[k])
            if len(present) == 2:
                counts["both_in_same_delta"] += 1
    result["stream"] = {"status": 200, "delta_counts": counts, "chars": chars}
    return result


def _render_prompt(client: httpx.Client, root: str, model: str, provider: str, messages: list[dict],
                   chat_template_kwargs: dict[str, Any] | None) -> tuple[int, str]:
    """テンプレート展開後のプロンプト文字列を得る。(HTTPステータス, 本文 or エラー)。"""
    extra = {"chat_template_kwargs": chat_template_kwargs} if chat_template_kwargs is not None else {}
    if provider == "vllm":
        resp = client.post(f"{root}/tokenize",
                           json={"model": model, "messages": messages, "add_generation_prompt": True, **extra})
        if resp.status_code != 200:
            return resp.status_code, _error_text(resp)
        detok = client.post(f"{root}/detokenize", json={"model": model, "tokens": resp.json()["tokens"]})
        return detok.status_code, detok.json().get("prompt", "") if detok.status_code == 200 else _error_text(detok)
    resp = client.post(f"{root}/apply-template", json={"messages": messages, **extra})
    if resp.status_code != 200:
        return resp.status_code, _error_text(resp)
    return 200, resp.json().get("prompt", "")


def probe_history_fields(client: httpx.Client, root: str, model: str, provider: str) -> list[dict[str, Any]]:
    """履歴の thinking フィールド名 × preserve系kwargs の組み合わせで展開結果を調べる。"""
    rows = []
    for field_label, fields in HISTORY_FIELD_CASES:
        for kwargs_label, kwargs in HISTORY_KWARGS_CASES:
            messages = [
                {"role": "user", "content": "質問1"},
                {"role": "assistant", "content": "回答1", **fields},
                {"role": "user", "content": "質問2"},
            ]
            status, text = _render_prompt(client, root, model, provider, messages, kwargs)
            rows.append({
                "history_field": field_label,
                "kwargs": kwargs_label,
                "status": status,
                "marker_count": text.count(PRIOR_THINK_MARKER) if status == 200 else None,
                "error": None if status == 200 else text,
            })
    return rows


def probe_params(client: httpx.Client, base_url: str, model: str, max_tokens: int) -> list[dict[str, Any]]:
    """各パラメータを単独で送り、受理されるかと thinking / 本文の文字数を調べる。"""
    rows = []
    for label, extra in PARAM_CASES:
        body = {"model": model, "messages": [{"role": "user", "content": QUESTION}], "max_tokens": max_tokens,
                **extra}
        resp = _post(client, f"{base_url}/chat/completions", body)
        row: dict[str, Any] = {"param": label, "status": resp.status_code}
        if resp.status_code == 200:
            message = resp.json()["choices"][0]["message"]
            reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
            row.update(reasoning_chars=len(reasoning), content_chars=len(message.get("content") or ""))
        else:
            row["error"] = _error_text(resp)
        rows.append(row)
        print(f"  {label}: {row}", file=sys.stderr)
    return rows


def _markdown(result: dict[str, Any]) -> str:
    lines = [f"# reasoning 方言の実測結果（provider={result['provider']}, model={result['model']}）", ""]
    lines += ["## 1. 応答フィールド", "", "```json", json.dumps(result["response_fields"], ensure_ascii=False, indent=2),
              "```", ""]
    lines += ["## 2. 履歴フィールドの反映（marker_count: 0=無視 / 1=反映 / 2=重複）", "",
              "| 履歴フィールド | kwargs | status | marker_count | error |", "|---|---|---|---|---|"]
    for r in result["history_fields"]:
        lines.append(f"| {r['history_field']} | {r['kwargs']} | {r['status']} | {r['marker_count']} | {r['error'] or ''} |")
    lines += ["", "## 3. パラメータの受理", "", "| パラメータ | status | thinking文字数 | 本文文字数 | error |",
              "|---|---|---|---|---|"]
    for r in result["params"]:
        lines.append(f"| {r['param']} | {r['status']} | {r.get('reasoning_chars', '')} | "
                     f"{r.get('content_chars', '')} | {r.get('error', '')} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="OpenAI互換 base_url（例: http://localhost:12430/v1）")
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", required=True, choices=["llama_cpp", "vllm", "openai_compatible"],
                        help="テンプレート展開に使うAPIの種類（vllm は /tokenize、それ以外は /apply-template）")
    parser.add_argument("--api-key", default="dummy")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--json-out", help="結果JSONの保存先（省略時は保存しない）")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.api_key}"}
    with httpx.Client(timeout=args.timeout, headers=headers) as client:
        print("[1/3] 応答フィールド", file=sys.stderr)
        response_fields = probe_response_fields(client, base_url, args.model, args.max_tokens)
        print("[2/3] 履歴フィールドの反映", file=sys.stderr)
        history_fields = probe_history_fields(client, _server_root(base_url), args.model, args.provider)
        print("[3/3] パラメータの受理", file=sys.stderr)
        params = probe_params(client, base_url, args.model, args.max_tokens)

    result = {"provider": args.provider, "model": args.model, "base_url": base_url,
              "response_fields": response_fields, "history_fields": history_fields, "params": params}
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    sys.stdout.reconfigure(encoding="utf-8")
    print(_markdown(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
