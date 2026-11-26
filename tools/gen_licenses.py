"""THIRD_PARTY_LICENSES.md を自動生成する。

requirements.txt の直接依存から到達する「実行時の推移的依存」を対象に、
各パッケージのバージョン・ライセンス・URL を収集し、Markdown の告知ファイルを出力する。

使い方（プロジェクトルートのコマンドプロンプトで実行）:
    call python_env.bat
    python tools/gen_licenses.py

ライセンス検出は次の優先順で行う（新旧のメタデータ規約に対応）:
    1. License-Expression（PEP 639 の SPDX 式）
    2. License 分類子（Classifier: License :: ...）
    3. License フィールド（自由記述の先頭行）
"""

from __future__ import annotations

import importlib.metadata as md
from collections import Counter
from datetime import date
from pathlib import Path

from packaging.requirements import Requirement

# 本プロジェクトの直接依存（requirements.txt のトップレベル）。
TOP = [
    "langgraph",
    "langgraph-checkpoint-sqlite",
    "langchain-core",
    "langchain-openai",
    "chainlit",
    "pyyaml",
    "jmespath",
    "pypdf",
    "pypdfium2",
    "reportlab",
    "openpyxl",
    "python-calamine",
    "xlrd",
    "python-docx",
    "python-pptx",
    "sqlite-vec",
    "onnxruntime",
    "tokenizers",
    "numpy",
    "cadquery",
    "ezdxf",
    "scipy",
]
# 改変せず別のライブラリとして使う（動的リンク）限り商用利用できる弱いコピーレフト。
WEAK_COPYLEFT = {"LGPL", "MPL 2.0"}
# メタデータにライセンスが書かれていないパッケージ（同梱の LICENSE ファイルを読んで確かめた値）。
KNOWN_LICENSES = {
    # LICENSE は BSD 2 条項（Anaconda）、同梱の LLVM は Apache 2.0 with LLVM Exceptions（LICENSE.thirdparty）。
    "llvmlite": "BSD-2-Clause; Apache-2.0 WITH LLVM-exception (bundled LLVM)",
}
# 告知に含めない実行環境ツール。
EXCLUDE = {"pip", "setuptools", "wheel"}

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "THIRD_PARTY_LICENSES.md"


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def dependency_closure() -> set[str]:
    """トップレベルから到達する実行時依存の推移閉包を返す（extra は除外）。"""
    seen: set[str] = set()
    stack = list(TOP)
    while stack:
        name = _norm(stack.pop())
        if name in seen:
            continue
        seen.add(name)
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        for req_str in dist.requires or []:
            try:
                req = Requirement(req_str)
            except Exception:
                continue
            # extra 付き（オプション依存）はスキップ。
            if req.marker and "extra" in str(req.marker):
                continue
            stack.append(req.name)
    return seen


def license_of(meta) -> str:
    # 1. PEP 639 License-Expression（SPDX）。
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip()
    # 2. License 分類子。
    classifiers = [v for k, v in meta.items() if k == "Classifier" and v.startswith("License")]
    names = [c.split("::")[-1].strip() for c in classifiers]
    # "License :: OSI Approved" だけ（種類が分からない）なら License フィールドを見る（casadi 等）。
    if names and names != ["OSI Approved"]:
        return "; ".join(names)
    # 3. License フィールド（自由記述の先頭行）。
    raw = (meta.get("License") or "").splitlines()
    if raw and raw[0].strip():
        return raw[0][:60]
    return "; ".join(names) if names else "（記載なし）"


def url_of(meta) -> str:
    url = meta.get("Home-page")
    if url:
        return url
    for k, v in meta.items():
        if k == "Project-URL" and ("ome" in v or "ository" in v or "ource" in v):
            return v.split(",")[-1].strip()
    return ""


def simplify(lic: str) -> str:
    up = lic.upper()
    table = [
        ("APACHE", "Apache 2.0"),
        ("MIT", "MIT"),
        ("BSD", "BSD"),
        ("MPL", "MPL 2.0"),
        ("MOZILLA", "MPL 2.0"),
        ("ISC", "ISC"),
        ("PYTHON SOFTWARE", "PSF"),
        ("PSF", "PSF"),
        ("UNLICENSE", "Unlicense"),
        ("AGPL", "AGPL"),
        ("LGPL", "LGPL"),
        ("GPL", "GPL"),
    ]
    for key, label in table:
        if key in up:
            return label
    return lic[:30] if lic else "（記載なし）"


def main() -> None:
    closure = dependency_closure()
    rows = []
    for name in sorted(closure):
        if name in EXCLUDE:
            continue
        try:
            meta = md.distribution(name).metadata
        except md.PackageNotFoundError:
            continue
        rows.append((name, meta.get("Version", "?"), KNOWN_LICENSES.get(name) or license_of(meta), url_of(meta)))

    summary = Counter(simplify(r[2]) for r in rows)

    out = []
    out.append("# 第三者ライセンス告知 (THIRD_PARTY_LICENSES)")
    out.append("")
    out.append("本ファイルは Locohane が実行時に利用する第三者オープンソース")
    out.append(f"パッケージとそのライセンスの一覧です（tools/gen_licenses.py で自動生成 / 生成日: {date.today()}）。")
    out.append("対象は requirements.txt の直接依存から到達する実行時の推移的依存集合です。")
    out.append("")
    out.append("再生成:")
    out.append("")
    out.append("```cmd")
    out.append("call python_env.bat")
    out.append("python tools/gen_licenses.py")
    out.append("```")
    out.append("")
    out.append("## ライセンス種別サマリ")
    out.append("")
    out.append("| ライセンス種別 | パッケージ数 |")
    out.append("|---|---:|")
    for k, c in summary.most_common():
        out.append(f"| {k} | {c} |")
    out.append(f"| **合計** | **{len(rows)}** |")
    out.append("")
    lgpl = [r[0] for r in rows if simplify(r[2]) == "LGPL"]
    strong = [r[0] for r in rows if simplify(r[2]) in ("GPL", "AGPL")]
    out.append("> 寛容ライセンス（MIT / Apache 2.0 / BSD / PSF / ISC）、ファイル単位の弱いコピーレフト（MPL 2.0）、")
    out.append("> ライブラリ単位の弱いコピーレフト（LGPL）だけであれば、改変せず依存として利用する")
    out.append("> 限り本ソフトウェアへの組み込み・商用配布が可能です。")
    if strong:
        out.append(f"> **GPL / AGPL が含まれています: {', '.join(strong)}。配布前に確認してください。**")
    else:
        out.append("> GPL / AGPL は含まれません。")
    if lgpl:
        out.append(f"> LGPL: {', '.join(lgpl)}（3D ビューア用の cadquery の依存）。改変せず、pip で入れた別のライブラリとして")
        out.append("> 使う限り商用利用できます。改変して配布する場合や、1 つの実行ファイルへ静的にまとめる場合は LGPL の条項に従ってください。")
    copyleft = [r[0] for r in rows if any(k in r[2].upper() for k in ("LGPL", "LESSER GENERAL", "MPL", "MOZILLA"))]
    out.append("> 各パッケージを改変する場合は当該ライセンス条項に従ってください。")
    if copyleft or lgpl:
        out.append(">")
        out.append(f"> **改変禁止**: {', '.join(copyleft)}、および cadquery-ocp に同梱の OpenCASCADE 本体（LGPL / MPL 2.0）。")
        out.append("> ファイルの書き換え・パッチ・フォーク版への差し替えをせず、LGPL のものは静的にリンクしないこと")
        out.append("> （README_DETAIL.md「依存パッケージ（商用利用可）」の「改変禁止のパッケージ」参照）。")
    out.append("")
    out.append("> `cadquery-ocp`（OCP）はラッパー自体は Apache 2.0 ですが、OpenCASCADE Technology（OCCT）の")
    out.append("> ビルド済みライブラリを同梱しており、OCCT は LGPL-2.1 + Open CASCADE Exception です（上の集計では")
    out.append("> パッケージのメタデータどおり Apache 2.0 に数えています）。")
    out.append("")
    out.append("> `pypdfium2` はビルド済みPDFiumバイナリに libpng / LibTIFF / FreeType（FTL） / zlib /")
    out.append("> libjpeg-turbo / ICU 等の第三者コードを同梱しており、パッケージの")
    out.append("> `LicenseRef-PdfiumThirdParty` にその全文が含まれます。確認の結果、いずれも寛容")
    out.append("> ライセンスでGPL等の混入はありません。")
    out.append("")
    out.append("## パッケージ一覧")
    out.append("")
    out.append("| パッケージ | バージョン | ライセンス | URL |")
    out.append("|---|---|---|---|")
    for name, ver, lic, url in rows:
        out.append(f"| {name} | {ver} | {lic} | {url} |")
    out.append("")

    OUTPUT.write_text("\n".join(out), encoding="utf-8")
    print(f"生成完了: {OUTPUT}  対象パッケージ数: {len(rows)}")
    print("サマリ:", dict(summary))


if __name__ == "__main__":
    main()
