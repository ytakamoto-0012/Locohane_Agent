"""大量ファイル探索シナリオの再現用テストデータを生成するスクリプト。

evals/cases/system_prompt/018番台のケースが参照する、子供会の活動記録を
模したフィクスチャ（evals/fixtures/annual_schedule/）を生成する。実データ
（ユーザー提供の写真・スキャン画像）は個人情報配慮のため一切コピーせず、
PIL でテキストのみのダミー画像を合成する。

実データの特徴（2019/2020/2025年の年別フォルダ、各年に写真、
一部の年だけ ocr_md/ サブフォルダに一部ページのみOCR済みmarkdownがある、
という「画像だけの年」と「一部OCR済みの年」が混在する構成）を縮小再現する:
- 2019/: 写真3枚 + ocr_md/ に一部だけOCR済みmd（1枚分のみ）
- 2020/: 写真2枚（OCR済みmdなし、全て未OCR）
- 2025/: 写真2枚 + ocr_md/ に1枚分のみOCR済みmd

--preset large では、上記の縮小フィクスチャでは再現できなかった規模差
（過去に「evalではpassしたが実データでは動かなかった」原因の一つ、
evals/tuning_log.md 参照）を埋めるため、年数・年あたり件数を指定して
実データ相当（数十件規模）のフィクスチャを決定論的に生成する。

使い方:
    python evals/fixtures/generate_annual_schedule_fixture.py
    python evals/fixtures/generate_annual_schedule_fixture.py --preset large
    python evals/fixtures/generate_annual_schedule_fixture.py --preset large --years 8 --events-per-year 15
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from PIL import Image, ImageDraw

FIXTURE_ROOT = Path(__file__).resolve().parent / "annual_schedule"
LARGE_FIXTURE_ROOT = Path(__file__).resolve().parent / "annual_schedule_large"

# 年 -> [(ファイル名, 描画テキスト, OCR済みmdを別途作るか), ...]
_EVENTS: dict[str, list[tuple[str, str, bool]]] = {
    "2019": [
        ("photo_01.png", "2019年3月10日\n花見会\n参加者25名", True),
        ("photo_02.png", "2019年7月20日\n夏祭り\n参加者40名", False),
        ("photo_03.png", "2019年12月15日\nもちつき大会\n参加者30名", False),
    ],
    "2020": [
        ("photo_01.png", "2020年2月2日\n節分祭\n参加者15名", False),
        ("photo_02.png", "2020年8月8日\n川遊びイベント\n参加者20名", False),
    ],
    "2025": [
        ("photo_01.png", "2025年4月12日\n春のハイキング\n参加者18名", True),
        ("photo_02.png", "2025年11月3日\n収穫祭\n参加者22名", False),
    ],
}

# --preset large 用の行事名テンプレート（月・参加人数はシードから決定論的に生成する）。
_EVENT_NAMES = [
    "花見会", "夏祭り", "もちつき大会", "節分祭", "川遊びイベント",
    "春のハイキング", "収穫祭", "秋の遠足", "クリスマス会", "新年会",
    "運動会", "お楽しみ会", "納涼祭", "文化祭", "餅つき交流会",
    "防災訓練", "ラジオ体操会", "七夕まつり", "餅つき大会", "登山会",
]


def _make_image(path: Path, text: str) -> None:
    img = Image.new("RGB", (480, 320), color="white")
    draw = ImageDraw.Draw(img)
    draw.multiline_text((20, 20), text, fill="black", spacing=12)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _make_ocr_md(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.replace("\n", " / ") + "\n", encoding="utf-8")


def _build_small_fixture() -> Path:
    """既定の縮小フィクスチャ（3年分・計9ファイル）を生成する。"""
    for year, events in _EVENTS.items():
        year_dir = FIXTURE_ROOT / year
        ocr_dir = year_dir / "ocr_md"
        for filename, text, has_ocr in events:
            _make_image(year_dir / filename, text)
            if has_ocr:
                stem = Path(filename).stem
                _make_ocr_md(ocr_dir / f"md_{stem}.md", text)
    return FIXTURE_ROOT


def _build_large_fixture(
    root: Path, years: int, events_per_year: int, ocr_ratio: float, seed: int
) -> Path:
    """実データ相当の規模（年数×年あたり件数）のフィクスチャを決定論的に生成する。

    Args:
        root: 出力先ルートディレクトリ。
        years: 生成する年数（直近年から遡って連番の年フォルダを作る）。
        events_per_year: 1年あたりの行事件数（=写真ファイル数）。
        ocr_ratio: 各年内でOCR済みmdを作る行事の割合（0.0〜1.0）。
        seed: 乱数シード（同じ値なら毎回同じフィクスチャが再現される）。

    Returns:
        生成したルートディレクトリ。
    """
    rng = random.Random(seed)
    base_year = 2026 - years
    for year_offset in range(years):
        year = str(base_year + year_offset)
        year_dir = root / year
        ocr_dir = year_dir / "ocr_md"
        for event_index in range(events_per_year):
            name = rng.choice(_EVENT_NAMES)
            month = rng.randint(1, 12)
            day = rng.randint(1, 28)
            attendees = rng.randint(10, 50)
            text = f"{year}年{month}月{day}日\n{name}\n参加者{attendees}名"
            filename = f"photo_{event_index + 1:02d}.png"
            _make_image(year_dir / filename, text)
            if rng.random() < ocr_ratio:
                _make_ocr_md(ocr_dir / f"md_{Path(filename).stem}.md", text)
    return root


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", choices=["small", "large"], default="small")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--events-per-year", type=int, default=8)
    parser.add_argument("--ocr-ratio", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()

    if args.preset == "small":
        out = _build_small_fixture()
    else:
        out = _build_large_fixture(
            root=args.out_dir or LARGE_FIXTURE_ROOT,
            years=args.years,
            events_per_year=args.events_per_year,
            ocr_ratio=args.ocr_ratio,
            seed=args.seed,
        )
    print(f"生成完了: {out}")


if __name__ == "__main__":
    main()
