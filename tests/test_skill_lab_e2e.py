"""スキル研究室の通し試験: 簡単なドラフトスキルの研究テーマが、最後まで回って合格（レビュー待ち）になること。

本物の推論サーバーの代わりに、OpenAI 互換の偽 LLM サーバーをテストの中で立てる。それ以外は本番と同じ道筋を通す:
スキル調整ワーカー（Worker.run_theme）→ 評価の子プロセス（evals/run_all.py → run_case.py。本物のグラフ・
read_skill・スキルの重ね合わせ）→ 判定（judge.py）→ トライアウト → レビュー待ち。

偽 LLM はスキルの本文を読まない限り答えを知らない（合言葉はスキルの SKILL.md にだけ書いてある）ので、
研究対象のスキルが評価で読めなければ不合格になる。
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from admin import instances as inst
from admin.skill_lab import promotion, themes

SKILL_NAME = "secret-word"
SECRET = "ひまわり"


def _chunk(delta: dict, finish: str | None = None) -> dict:
    return {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


# 研究室の AI（judge 等）へのシステムプロンプト（出力言語の指示が付いているかを確かめる）
JUDGE_SYSTEMS: list[str] = []
REPORT_SYSTEMS: list[str] = []


def _reply(messages: list[dict]) -> dict:
    """スキルの本文（合言葉）を読んだ後なら答え、まだなら read_skill を呼ぶ。研究室の AI judge には合格と答える。"""
    system = str(messages[0].get("content")) if messages and messages[0].get("role") == "system" else ""
    if system.startswith("You are the judge"):
        JUDGE_SYSTEMS.append(system)
        return {"content": json.dumps({"pass": True, "basis": "", "reason": "合言葉を答えている"}, ensure_ascii=False)}
    if system.startswith("You write the end-of-loop report"):
        REPORT_SYSTEMS.append(system)
        return {"content": "総括: テスト用のまとめ。"}
    if system.startswith("You fix the skills"):
        # AI 修正: 指示がスキルと無関係なケースを「ケース自体の問題」として報告する
        problem = {"case_id": "001", "problem": "指示が挨拶だけで、スキルを使う理由が無い", "suggestion": "合言葉を尋ねる指示にする"}
        return {"content": json.dumps({"analysis": "ケースの問題", "blockers": [problem], "edits": []}, ensure_ascii=False)}
    user_text = " ".join(str(m.get("content")) for m in messages if m.get("role") == "user")
    if "合言葉" not in user_text and SKILL_NAME not in user_text:
        # スキルと関係の無い指示には、スキルを使わずに答える（本物のモデルと同じく、使う理由が無い）
        return {"content": "こんにちは。"}
    read = any(m.get("role") == "tool" and SECRET in str(m.get("content")) for m in messages)
    if read:
        return {"content": f"合言葉は{SECRET}です。"}
    return {"tool_call": {"name": "read_skill", "arguments": json.dumps({"skill_name": SKILL_NAME})}}


class _FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *_args):  # テストの出力を汚さない
        pass

    def _json(self, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - /models 等の疎通確認
        self._json({"object": "list", "data": [{"id": "m", "object": "model"}]})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not self.path.endswith("/chat/completions"):
            self._json({})
            return
        reply = _reply(body.get("messages") or [])
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if not body.get("stream"):
            message = {"role": "assistant", "content": reply.get("content", "")}
            if "tool_call" in reply:
                message["tool_calls"] = [{"id": "call_1", "type": "function", "function": reply["tool_call"]}]
            finish = "tool_calls" if "tool_call" in reply else "stop"
            self._json({"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                        "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})
            return
        if "tool_call" in reply:
            chunks = [
                _chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                              "function": {"name": reply["tool_call"]["name"], "arguments": ""}}]}),
                _chunk({"tool_calls": [{"index": 0, "function": {"arguments": reply["tool_call"]["arguments"]}}]}),
                _chunk({}, "tool_calls"),
            ]
        else:
            chunks = [_chunk({"role": "assistant", "content": reply["content"]}), _chunk({}, "stop")]
        chunks.append({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m", "choices": [], "usage": usage})
        data = ("".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_llm():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


@pytest.fixture
def lab_env(tmp_path, monkeypatch, fake_llm):
    """インスタンス org_a と、偽 LLM につながる研究室 lab1 を tmp に作る（トライアウトは1回）。"""
    root = tmp_path / "instances"
    monkeypatch.setenv("INSTANCES_DIR", str(root))
    for key in ("LOCOHANE_INSTANCE", "CONFIG_OVERRIDES_PATH", "LOCOHANE_INSTANCE_ENV", "COMMON_DATA_DIR", "PROJECT_LOCOHANE_DIR", "SKILL_LAB_TEMP_DIR"):
        monkeypatch.delenv(key, raising=False)
    endpoint =json.dumps([{"base_url": fake_llm, "api_key": "x", "model": "m", "provider": "llama_cpp"}])
    for name, kind in (("org_a", inst.KIND_APP), ("lab1", inst.KIND_SKILL_LAB)):
        (root / name).mkdir(parents=True)
        inst.write_instance(root, inst.InstanceMeta(name=name, display_name=name, app_port=18001 if name == "org_a" else 0, kind=kind))
        overrides = {"paths": {"common_data_dir": str(tmp_path / "data" / name)}}
        if name == "org_a":
            overrides["skill_lab"] = {"tryout_repeats": "1"}
        else:
            overrides["llm"] = {"main_url": endpoint}
        (root / name / "config_overrides.json").write_text(json.dumps(overrides), encoding="utf-8")
    return {"root": root, "tmp": tmp_path}


def _draft_theme(lab_env, judge: str | None = None, turn: str = "合言葉を教えて"):
    """利用者 tanaka のドラフトスキルを取り込み、仕様・ケースを書いた研究テーマ。"""
    cfg = promotion.instance_config(lab_env["root"], "org_a")
    draft = cfg.skill_draft_dir / "tanaka" / SKILL_NAME
    draft.mkdir(parents=True)
    (draft / "SKILL.md").write_text(
        f"---\nname: {SKILL_NAME}\ndescription: 合言葉を聞かれたときに使う。合言葉はこのスキルの本文にだけ書いてある。\n---\n\n"
        f"# 合言葉\n\n合言葉は「{SECRET}」。聞かれたらそのまま答える。\n",
        encoding="utf-8",
    )
    (draft / "_draft_meta.json").write_text(json.dumps({"kind": "new", "status": "draft"}), encoding="utf-8")
    directory = themes.create_theme(themes.themes_root(cfg.common_data_dir), instance="org_a", title="合言葉", lab="lab1", actor="admin")
    themes.add_asset_copy(directory, "skills", SKILL_NAME, draft, source={"kind": "draft", "origin": str(draft), "owner": "tanaka"}, actor="admin")
    themes.write_text(directory, themes.SPEC_FILENAME, "# 合言葉\n\n## Purpose\n合言葉を答える\n", actor="admin")
    case = (
        f"id: '001'\ntarget: skill_lab\nturns:\n  - {turn}\nauto_approve: true\n"
        f"expect:\n  tool_call_args_contains:\n    read_skill:\n      skill_name: {SKILL_NAME}\n  response_contains:\n    - {SECRET}\n"
        + (f"judge: {judge}\n" if judge else "")
    )
    themes.write_text(directory, "cases/001.yaml", case, actor="admin")
    return directory


@pytest.mark.parametrize("judge", [None, "合言葉を正しく答えている"], ids=["rules_only", "with_ai_judge"])
def test_simple_draft_skill_theme_reaches_review(lab_env, judge):
    JUDGE_SYSTEMS.clear()
    directory = _draft_theme(lab_env, judge)
    assert themes.start_missing(directory) == []
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED, "loop": {"mode": "loop", "by": "admin", "lang": "ja"}}))

    _worker(lab_env).run_theme("org_a", directory)

    data = themes.read(directory)
    detail = json.dumps({"loop": data.get("loop"), "iterations": data.get("iterations")}, ensure_ascii=False, default=str)[:4000]
    assert data["status"] == themes.STATUS_REVIEW, detail
    phases = [(it["phase"], it["verdict"]) for it in data["iterations"]]
    assert phases == [("eval", "pass"), ("tryout", "pass")], detail
    assert data["tryout"]["ai_verdict"] == "pass" and data["tryout"]["ai_judged"] == bool(judge)
    if judge:
        # 改良前の版の評価1回 + 評価1回 + トライアウト1回の各回を AI judge が判定し、判定の理由は日本語で書くよう指示されている
        assert len(JUDGE_SYSTEMS) == 3 and all("## Output language" in s and "Japanese" in s for s in JUDGE_SYSTEMS)
    # 取り込んだドラフト（改良前の版）も1回評価し、改良前後を比べられる
    assert data["baseline_eval"]["cases"] == {"001": {"pass": 1, "fail": 0, "error": 0}}
    # 終わったら総括（反復ごとの表と AI のまとめ）を残す
    report = data["loop"]["report"]
    assert report["status"] == "review" and [r["n"] for r in report["iterations"]] == [1, 2]
    assert report["summary"] == "総括: テスト用のまとめ。" and report["baseline"]["score"] == [1, 1.0]
    # 合格の根拠: 評価の中で研究対象のスキルを読み、合言葉を答えている
    results = json.loads((next(p for p in (directory / themes.RUNS_DIRNAME / "002").iterdir() if p.is_dir()) / "results.json").read_text(encoding="utf-8"))
    assert results[0]["rules_pass"] is True and SECRET in results[0]["final_answer"]
    # 一時フォルダは OS の一時フォルダではなく対象インスタンスの [skill_lab].temp_dir に作られ、終わると何も残らない
    temp_root = promotion.instance_config(lab_env["root"], "org_a").skill_lab_temp_dir
    assert temp_root == lab_env["tmp"] / "data" / "org_a" / "evals_temp"
    assert temp_root.is_dir() and list(temp_root.iterdir()) == []


def _worker(lab_env):
    from admin.skill_lab import worker as worker_mod

    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w.lab, w.instances_root, w.shutdown, w.base_env = "lab1", lab_env["root"], False, dict(os.environ)
    w._broken = set()
    w.config = promotion.instance_config(lab_env["root"], "lab1")
    return w


def test_loop_stops_and_reports_when_the_case_instruction_is_bad(lab_env):
    """利用者の指示が研究対象と無関係（挨拶だけ）なケースは、何度スキルを直しても合格しない。
    AI 修正がケースの問題として報告したら、スキルを書き換えずにループを止める（2026-11-13 ユーザー報告）。"""
    directory = _draft_theme(lab_env, turn="こんにちは")
    skill_md = directory / "assets" / "skills" / SKILL_NAME / "SKILL.md"
    before = skill_md.read_text(encoding="utf-8")
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED, "loop": {"mode": "loop", "by": "admin", "lang": "ja"}}))
    _worker(lab_env).run_theme("org_a", directory)

    data = themes.read(directory)
    assert data["status"] == themes.STATUS_BLOCKED, data.get("loop")
    assert [(it["phase"], it["verdict"]) for it in data["iterations"]] == [("eval", "fail")]  # 1回で止まり、上限まで回さない
    assert data["loop"]["blockers"][0]["case_id"] == "001"
    assert "スキルを使う理由が無い" in data["loop"]["message"]
    assert skill_md.read_text(encoding="utf-8") == before
