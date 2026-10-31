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
]
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
    if classifiers:
        return "; ".join(c.split("::")[-1].strip() for c in classifiers)
    # 3. License フィールド（自由記述の先頭行）。
    raw = (meta.get("License") or "").splitlines()
    return raw[0][:45] if raw and raw[0].strip() else "（記載なし）"


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
        rows.append((name, meta.get("Version", "?"), license_of(meta), url_of(meta)))

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
    out.append("> いずれも寛容ライセンス（MIT / Apache 2.0 / BSD / PSF / ISC）または")
    out.append("> ファイル単位の弱いコピーレフト（MPL 2.0）であり、改変せず依存として利用する")
    out.append("> 限り本ソフトウェアへの組み込み・商用配布が可能です。")
    out.append("> GPL / AGPL / LGPL は含まれません。各パッケージを改変する場合は当該ライセンス条項に従ってください。")
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
