"""LLM へ返す STEP の概要（src/cad/step_xcaf.py が data["step"] に入れたものから作る）。標準ライブラリだけで動く。

本体のプロセス（src/tools/cad_viewer.py）から使うので、OCP・cadquery を import しない。
"""

from __future__ import annotations

from typing import Any

# LLM へ返す概要に入れる PMI の文の数の上限（全部は data["step"]["pmi"] にある）。
SUMMARY_MAX_PMI = 20


def summarize_step(step: dict[str, Any]) -> dict[str, Any]:
    """LLM へ返す STEP の概要（大きさが件数によらずほぼ一定）。"""
    header = step.get("header") or {}
    out: dict[str, Any] = {k: header[k] for k in ("ap", "edition", "originating_system", "preprocessor_version", "organization", "author") if header.get(k)}
    if header.get("schema"):
        out["schema"] = header["schema"][0] if len(header["schema"]) == 1 else header["schema"]
    pmi = step.get("pmi") or {}
    if pmi:
        out["pmi"] = {k: len(v) for k, v in pmi.items()}
        texts = [i["text"] for k in ("dimensions", "tolerances", "datums") for i in pmi.get(k, [])]
        out["pmi_texts"] = texts[:SUMMARY_MAX_PMI]
        if len(texts) > SUMMARY_MAX_PMI:
            out["pmi_more"] = len(texts) - SUMMARY_MAX_PMI
    prods = step.get("products") or []
    with_attrs = [p for p in prods if p.get("attributes")]
    if with_attrs:
        out["attributes"] = {"products": len(with_attrs), "count": sum(len(p["attributes"]) for p in with_attrs)}
        # 製品が 1 つなら属性名を見せる（値はファイルで）。
        if len(with_attrs) == 1:
            out["attributes"]["names"] = [a["name"] for a in with_attrs[0]["attributes"][:30]]
    if any(p.get("material") for p in prods):
        out["materials"] = sorted({str(m.get("value") or m.get("name")) for p in prods for m in (p.get("material") or [])[:1]})[:10]
    if step.get("layers"):
        out["layers"] = [lay["name"] for lay in step["layers"]][:20]
    for key in ("warnings", "pmi_error", "pmi_units_note"):
        if step.get(key):
            out[key] = step[key]
    return out
