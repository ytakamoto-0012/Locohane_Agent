"""Office系スキル共用のテーマ（THEMES）と resolve_theme 共有モジュール。

8つの配色テーマ（THEMES）とデフォルトテーマ（DEFAULT_THEME）、
およびテーマ名から辞書を取得する resolve_theme() 関数を提供する。

このモジュールは SKILL.md を持たない（非スキルディレクトリ）。
skills/ 直下のスキル外ファイルとして配置し、excel-read の既存パターン
（sys.path 経由）で各スキルから import する。
"""

from __future__ import annotations

DEFAULT_THEME = "charcoal"

# Anthropic公式pptxスキルのDesign Ideas（配色パレット）に準拠した8テーマ。
# pptx-create / excel-edit の THEMES と同じ名前・同じ色（クロススキルで
# 統一した見た目にできるよう意図的に揃えている）。
THEMES = {
    "charcoal": {"primary": "36454F", "secondary": "F2F2F2", "accent": "212121", "text_on_primary": "FFFFFF"},
    "navy": {"primary": "1E2761", "secondary": "CADCFC", "accent": "0B1440", "text_on_primary": "FFFFFF"},
    "forest": {"primary": "2C5F2D", "secondary": "97BC62", "accent": "1C3D1D", "text_on_primary": "FFFFFF"},
    "coral": {"primary": "2F3C7E", "secondary": "F9E795", "accent": "F96167", "text_on_primary": "FFFFFF"},
    "terracotta": {"primary": "B85042", "secondary": "E7E8D1", "accent": "A7BEAE", "text_on_primary": "FFFFFF"},
    "ocean": {"primary": "065A82", "secondary": "1C7293", "accent": "21295C", "text_on_primary": "FFFFFF"},
    "teal": {"primary": "028090", "secondary": "00A896", "accent": "02C39A", "text_on_primary": "FFFFFF"},
    "berry": {"primary": "6D2E46", "secondary": "A26769", "accent": "ECE2D0", "text_on_primary": "FFFFFF"},
}

# グラフの系列（4系列以上）用の固定順カテゴリカルパレット。dataviz skill
# （references/palette.md）が色覚多様性(CVD)シミュレーションで検証済みの
# 8色パレット（light面、隣接ΔE 9.1/正常視19.6）をそのまま採用する。
# THEMESのprimary/secondary/accentは3色循環だと4系列目以降で重複するため、
# グラフの系列色にはTHEMESではなく常にこちらを使う（テーマ名によらず固定）。
CHART_PALETTE = ["2A78D6", "EB6834", "1BAF7A", "EDA100", "E87BA4", "008300", "4A3AA7", "E34948"]


def resolve_theme(name: str | None) -> dict:
    """テーマ名から配色辞書を返す。

    Parameters
    ----------
    name : str | None
        テーマ名（大文字小文字不問）。None または空文字列・空白のみの場合は
        DEFAULT_THEME へフォールバックする。

    Returns
    -------
    dict
        {"primary": ..., "secondary": ..., "accent": ..., "text_on_primary": ...}

    Raises
    ------
    ValueError
        name が THEMES に存在しない場合。
    """
    key = (name or "").strip().lower()
    if not key:
        key = DEFAULT_THEME.lower()
    if key not in THEMES:
        supported = ", ".join(sorted(THEMES))
        raise ValueError(f"Unsupported theme: {name!r} (supported: {supported})")
    return THEMES[key]
