"""英検3級レベルの単語帳を模した合成画像PDFフィクスチャを生成するスクリプト。

evals/cases/system_prompt_scale/ の単語帳テスト作成ケースが参照する、
E:\\共有\\勉強-課題\\英検\\３級 にあるユーザー私物のスキャンPDF（市販の単語帳を
スマホで撮影したもの）を模した合成フィクスチャ。著作権のある実データは一切
コピーせず、PIL で単語・意味・例文をすべて自作テキストとして描画し、ページ画像を
PDFへ結合する。実データの特徴（テキスト層を持たない＝pypdf.extract_text()が空
になる、スキャン写真の見出し番号・品詞タグ・赤字の意味・例文レイアウト）を
縮小再現しているため、生成されるPDFは pdf-tools の read_pdf.py では本文が
取れず、render_pdf_pages.py + analyze_image で画像として読む必要がある
（実データと同じ制約をエージェント評価に課すための意図的な設計）。

**複数ファイルに分割する理由**: 実データを実際に開いて確認したところ、
E:\\共有\\勉強-課題\\英検\\３級 には6個のPDFがあったが、これは6冊の別内容の本
ではなく、同じ1冊（市販の「でる順パス単 英検3級」相当）をスマホで色々な
タイミングで撮影した写真を、見出し語番号がバラバラな順序のまま6個のPDFへ
書き出したもの（例: 1個のPDFにSection1の0001~0100の一部ページと、別の
PDFにもSection1の別ページが混在。ファイル名 excited.pdf / でる度.pdf 等も
中身のヒントにはならない連番でも通し番号でもない命名）。単語帳PDFが1個で
完結する保証はなく、対象語がどのファイルの何ページ目にあるかはファイルを
開いて中身を確認するまで分からない、というのが実際の使われ方である。
1ファイルに全300語をきれいに収めた最初のフィクスチャ（このスクリプトの旧版）
はこの「複数ファイルにまたがって散らばっている」実態を再現できておらず
不十分だったため、複数ファイル構成に変更した。

単語データは合計300語（見出し番号0001~0300）。1ページ8語 x 38ページ
（最終ページのみ4語）分を作った上で、2ページ単位のかたまりを
`NUM_FILES`（既定6）個のPDFファイルへ順繰りに割り振る（実際の6ファイル
構成に合わせた既定値）。そのため学習計画の「1回目のテスト（単語1~40）」
だけを見ても、対象語は複数ファイルに分散して収録される（既定パラメータでは
3ファイルにまたがる）。各ファイルの名前は、そのファイルの最初のページの
先頭単語（英語, 小文字）+ ".pdf"（実データの excited.pdf 等が本文中の一単語
らしき名前だったのを再現した命名規則）とし、通し番号やSection番号のような
中身を示唆する名前は使わない。エージェント側の作業ディレクトリには
これら複数のPDFのみを置き、正解データ（answer key、各語がどのファイルの
何ページ目にあるかを含む）はワークディレクトリの外（フィクスチャと同階層の
兄弟ファイル）に保存する。エージェントが誤って正解データを直接読んで
カンニングしないようにするため、また、judge（人間役のClaudeCode）が
生成された問題PDFの内容を正解と突き合わせて捏造の有無を確認できるようにする
ため。

使い方:
    python evals/fixtures/answer/system_prompt_scale/generate_eiken_wordlist_fixture.py
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "英検3級語彙"
ANSWER_KEY_PATH = Path(__file__).resolve().parent / "英検3級語彙_answer_key.json"

# 実データ（E:\共有\勉強-課題\英検\３級）が6個のPDFに分散していたことに合わせた既定値。
NUM_FILES = 6
# 何ページ連続で同じファイルに割り振ってからローテーションするか。
# 38ページ・6ファイルの既定値では、対象語1~40（先頭5ページ）が
# ちょうど3ファイルにまたがって分散する（多すぎず少なすぎない分散具合）。
PAGES_PER_FILE_CHUNK = 2

_JP_FONT_PATH = Path(r"C:\Windows\Fonts\meiryo.ttc")
_JP_BOLD_FONT_PATH = Path(r"C:\Windows\Fonts\meiryob.ttc")
_EN_FONT_PATH = Path(r"C:\Windows\Fonts\arial.ttf")
_EN_BOLD_FONT_PATH = Path(r"C:\Windows\Fonts\arialbd.ttf")


def _font(path: Path, fallback: Path, size: int) -> ImageFont.FreeTypeFont:
    p = path if path.exists() else fallback
    return ImageFont.truetype(str(p), size=size)


# --- 品詞ごとの単語バンク（word, 意味）--------------------------------------
# 意味は例文テンプレートにそのまま埋め込める形で用意する:
#   動詞: 辞書形（「〜ことができますか」等の名詞化に接続可能な形）
#   形容詞: 述語化しやすいよう、な形容詞は「な」を含めない形
#   名詞: そのまま名詞として使える形
#   副詞: そのまま副詞として使える形
_VERBS: list[tuple[str, str]] = [
    ("begin", "始める"), ("catch", "捕まえる"), ("invite", "招待する"), ("feel", "感じる"),
    ("choose", "選ぶ"), ("hike", "ハイキングする"), ("keep", "保つ"), ("borrow", "借りる"),
    ("lend", "貸す"), ("forget", "忘れる"), ("remember", "覚えている"), ("decide", "決める"),
    ("agree", "同意する"), ("arrive", "到着する"), ("leave", "出発する"), ("visit", "訪れる"),
    ("travel", "旅行する"), ("discover", "発見する"), ("explain", "説明する"), ("introduce", "紹介する"),
    ("improve", "改善する"), ("practice", "練習する"), ("collect", "集める"), ("throw", "投げる"),
    ("kick", "蹴る"), ("pass", "合格する"), ("win", "勝つ"), ("lose", "負ける"),
    ("join", "参加する"), ("meet", "会う"), ("wait", "待つ"), ("hurry", "急ぐ"),
    ("hope", "望む"), ("wish", "願う"), ("plan", "計画する"), ("prepare", "準備する"),
    ("clean", "掃除する"), ("wash", "洗う"), ("cook", "料理する"), ("bake", "焼く"),
    ("grow", "育てる"), ("plant", "植える"), ("water", "水をやる"), ("feed", "えさをやる"),
    ("save", "節約する"), ("spend", "使う"), ("return", "戻る"), ("repair", "修理する"),
    ("build", "建てる"), ("break", "壊す"), ("fall", "落ちる"), ("hurt", "傷つける"),
    ("cry", "泣く"), ("laugh", "笑う"), ("smile", "ほほえむ"), ("shout", "叫ぶ"),
    ("sing", "歌う"), ("dance", "踊る"), ("paint", "描く"), ("draw", "描く"),
    ("write", "書く"), ("listen", "聞く"), ("watch", "見る"), ("speak", "話す"),
    ("answer", "答える"), ("learn", "学ぶ"), ("teach", "教える"), ("study", "勉強する"),
    ("understand", "理解する"), ("believe", "信じる"), ("finish", "終える"), ("continue", "続ける"),
    ("enjoy", "楽しむ"), ("need", "必要とする"), ("buy", "買う"), ("sell", "売る"),
    ("carry", "運ぶ"), ("bring", "持ってくる"), ("send", "送る"), ("receive", "受け取る"),
    ("protect", "守る"), ("promise", "約束する"), ("solve", "解決する"), ("order", "注文する"),
    ("invent", "発明する"), ("exercise", "運動する"), ("count", "数える"), ("guess", "推測する"),
    ("share", "分け合う"), ("move", "引っ越す"),
]

_ADJECTIVES: list[tuple[str, str]] = [
    ("excited", "わくわくした"), ("sunny", "晴れた"), ("cute", "かわいい"), ("fine", "元気"),
    ("glad", "うれしい"), ("healthy", "健康"), ("heavy", "重い"), ("same", "同じ"),
    ("happy", "幸せ"), ("sad", "悲しい"), ("angry", "怒った"), ("surprised", "驚いた"),
    ("nervous", "緊張した"), ("worried", "心配"), ("tired", "疲れた"), ("sleepy", "眠い"),
    ("hungry", "お腹が空いた"), ("thirsty", "喉が渇いた"), ("busy", "忙しい"), ("free", "暇"),
    ("easy", "簡単"), ("difficult", "難しい"), ("simple", "単純"), ("dangerous", "危険"),
    ("safe", "安全"), ("careful", "注意深い"), ("useful", "役に立つ"), ("important", "重要"),
    ("necessary", "必要"), ("popular", "人気がある"), ("famous", "有名"), ("favorite", "お気に入り"),
    ("special", "特別"), ("different", "違う"), ("similar", "似ている"), ("strange", "奇妙"),
    ("interesting", "面白い"), ("boring", "退屈"), ("exciting", "わくわくする"), ("wonderful", "素晴らしい"),
    ("amazing", "驚くべき"), ("terrible", "ひどい"), ("beautiful", "美しい"), ("pretty", "かわいい"),
    ("ugly", "醜い"), ("dirty", "汚い"), ("quiet", "静か"), ("loud", "うるさい"),
    ("bright", "明るい"), ("dark", "暗い"), ("warm", "暖かい"), ("cool", "涼しい"),
    ("cold", "寒い"), ("hot", "暑い"), ("wet", "濡れた"), ("dry", "乾いた"),
    ("fresh", "新鮮"), ("delicious", "おいしい"), ("sweet", "甘い"), ("sour", "すっぱい"),
    ("bitter", "苦い"), ("salty", "しょっぱい"), ("spicy", "辛い"), ("soft", "柔らかい"),
    ("strong", "強い"), ("weak", "弱い"), ("fast", "速い"), ("slow", "遅い"),
    ("near", "近い"), ("far", "遠い"), ("high", "高い"), ("low", "低い"),
    ("deep", "深い"), ("wide", "広い"), ("narrow", "狭い"), ("thick", "厚い"),
    ("thin", "薄い"), ("full", "満杯"), ("empty", "空"), ("expensive", "高価"),
    ("cheap", "安い"), ("rich", "裕福"), ("poor", "貧しい"), ("young", "若い"),
    ("kind", "親切"), ("gentle", "優しい"), ("friendly", "友好的"), ("brave", "勇敢"),
    ("honest", "正直"), ("polite", "礼儀正しい"), ("rude", "失礼"), ("lazy", "怠惰"),
    ("hardworking", "勤勉"), ("smart", "賢い"), ("foolish", "愚か"), ("curious", "好奇心が強い"),
    ("proud", "誇りに思っている"), ("shy", "内気"), ("confident", "自信がある"), ("lonely", "寂しい"),
]

_NOUNS: list[tuple[str, str]] = [
    ("school", "学校"), ("teacher", "先生"), ("student", "生徒"), ("classroom", "教室"),
    ("homework", "宿題"), ("test", "テスト"), ("exam", "試験"), ("subject", "教科"),
    ("math", "数学"), ("science", "理科"), ("history", "歴史"), ("music", "音楽"),
    ("art", "美術"), ("gym", "体育館"), ("library", "図書館"), ("cafeteria", "食堂"),
    ("playground", "運動場"), ("uniform", "制服"), ("bag", "かばん"), ("pencil", "鉛筆"),
    ("notebook", "ノート"), ("textbook", "教科書"), ("dictionary", "辞書"), ("computer", "コンピューター"),
    ("internet", "インターネット"), ("phone", "電話"), ("camera", "カメラ"), ("ticket", "チケット"),
    ("money", "お金"), ("wallet", "財布"), ("key", "鍵"), ("umbrella", "傘"),
    ("bicycle", "自転車"), ("bus", "バス"), ("train", "電車"), ("airplane", "飛行機"),
    ("ship", "船"), ("station", "駅"), ("airport", "空港"), ("hospital", "病院"),
    ("restaurant", "レストラン"), ("hotel", "ホテル"), ("museum", "博物館"), ("park", "公園"),
    ("zoo", "動物園"), ("beach", "浜辺"), ("mountain", "山"), ("river", "川"),
    ("lake", "湖"), ("sea", "海"), ("sky", "空"), ("star", "星"),
    ("moon", "月"), ("sun", "太陽"), ("weather", "天気"), ("rain", "雨"),
    ("snow", "雪"), ("wind", "風"), ("cloud", "雲"), ("season", "季節"),
    ("spring", "春"), ("summer", "夏"), ("autumn", "秋"), ("winter", "冬"),
    ("holiday", "休日"), ("vacation", "休暇"), ("festival", "祭り"), ("birthday", "誕生日"),
    ("present", "プレゼント"), ("gift", "贈り物"), ("party", "パーティー"), ("guest", "お客"),
    ("friend", "友達"), ("family", "家族"), ("parent", "親"), ("brother", "兄弟"),
    ("sister", "姉妹"), ("cousin", "いとこ"), ("neighbor", "近所の人"), ("doctor", "医者"),
    ("nurse", "看護師"), ("farmer", "農家"), ("driver", "運転手"), ("artist", "芸術家"),
    ("singer", "歌手"), ("writer", "作家"), ("scientist", "科学者"), ("engineer", "技術者"),
    ("animal", "動物"), ("dog", "犬"), ("cat", "猫"), ("bird", "鳥"),
    ("fish", "魚"), ("horse", "馬"), ("rabbit", "うさぎ"), ("insect", "昆虫"),
    ("flower", "花"), ("tree", "木"), ("leaf", "葉"), ("fruit", "果物"),
    ("vegetable", "野菜"), ("rice", "米"), ("bread", "パン"), ("milk", "牛乳"),
    ("tea", "お茶"), ("coffee", "コーヒー"), ("juice", "ジュース"), ("breakfast", "朝食"),
    ("lunch", "昼食"), ("dinner", "夕食"), ("meal", "食事"), ("kitchen", "台所"),
    ("bedroom", "寝室"), ("bathroom", "浴室"), ("garden", "庭"), ("roof", "屋根"),
    ("wall", "壁"), ("door", "ドア"), ("window", "窓"), ("table", "テーブル"),
    ("chair", "いす"), ("bed", "ベッド"),
]

_ADVERBS: list[tuple[str, str]] = [
    ("quickly", "すぐに"), ("slowly", "ゆっくり"), ("carefully", "注意深く"), ("suddenly", "突然"),
    ("usually", "いつも"), ("sometimes", "時々"), ("often", "よく"), ("again", "再び"),
    ("already", "すでに"), ("still", "まだ"), ("soon", "もうすぐ"), ("later", "後で"),
    ("together", "一緒に"), ("alone", "一人で"), ("outside", "外で"), ("inside", "中で"),
    ("abroad", "海外へ"), ("easily", "簡単に"), ("especially", "特に"), ("finally", "ついに"),
    ("gently", "優しく"), ("happily", "幸せに"), ("loudly", "大声で"), ("quietly", "静かに"),
    ("really", "本当に"), ("recently", "最近"), ("safely", "安全に"), ("clearly", "はっきりと"),
]

_POS_TAG = {"verb": "動", "adjective": "形", "noun": "名", "adverb": "副"}


def _verb_example(word: str, meaning: str, idx: int) -> tuple[str, str]:
    templates = [
        (f"Can you {word} it?", f"それを{meaning}ことができますか？"),
        (f"I will {word} it tomorrow.", f"明日それを{meaning}つもりです。"),
        (f"We need to {word} it soon.", f"すぐにそれを{meaning}必要があります。"),
    ]
    return templates[idx % len(templates)]


def _adjective_example(word: str, meaning: str, idx: int) -> tuple[str, str]:
    subjects = [
        ("This bag", "このかばん"),
        ("That movie", "あの映画"),
        ("The weather today", "今日の天気"),
    ]
    en_subj, ja_subj = subjects[idx % len(subjects)]
    return (f"{en_subj} is very {word}.", f"{ja_subj}はとても{meaning}です。")


def _noun_example(word: str, meaning: str, idx: int) -> tuple[str, str]:
    templates = [
        (f"I have a {word}.", f"私は{meaning}を持っています。"),
        (f"This is my {word}.", f"これは私の{meaning}です。"),
        (f"I like the {word}.", f"私はその{meaning}が好きです。"),
    ]
    return templates[idx % len(templates)]


def _adverb_example(word: str, meaning: str, idx: int) -> tuple[str, str]:
    return (f"Please do it {word}.", f"それを{meaning}やってください。")


def _build_word_list(seed: int, total: int) -> list[dict]:
    """4品詞バンクを結合し、重複語を除去した上でシード固定でシャッフルし、先頭 total 件を返す。"""
    pool: list[dict] = []
    seen: set[str] = set()

    def _add(bank: list[tuple[str, str]], pos: str, example_fn) -> None:
        for i, (word, meaning) in enumerate(bank):
            if word in seen:
                continue
            seen.add(word)
            example_en, example_ja = example_fn(word, meaning, i)
            pool.append(
                {
                    "word": word,
                    "pos": pos,
                    "pos_tag": _POS_TAG[pos],
                    "meaning_ja": meaning,
                    "example_en": example_en,
                    "example_ja": example_ja,
                }
            )

    _add(_VERBS, "verb", _verb_example)
    _add(_ADJECTIVES, "adjective", _adjective_example)
    _add(_NOUNS, "noun", _noun_example)
    _add(_ADVERBS, "adverb", _adverb_example)

    if len(pool) < total:
        raise ValueError(f"単語バンクが不足しています（{len(pool)}語 < {total}語）。バンクを追加してください。")

    rng = random.Random(seed)
    rng.shuffle(pool)
    selected = pool[:total]
    for i, entry in enumerate(selected):
        entry["head_no"] = i + 1
    return selected


# --- ページ画像の描画 ---------------------------------------------------------
PAGE_W = 1300
PAGE_H = 1900
HEADER_H = 170
MARGIN_X = 60
FOOTER_MARGIN = 20
ENTRIES_PER_PAGE = 8

_COLOR_HEADER_BG = (232, 112, 58)
_COLOR_HEADER_TEXT = (255, 255, 255)
_COLOR_BADGE_BG = (90, 90, 90)
_COLOR_BADGE_TEXT = (255, 255, 255)
_COLOR_WORD = (20, 20, 20)
_COLOR_POS_TAG = (140, 140, 140)
_COLOR_MEANING = (216, 60, 30)
_COLOR_EXAMPLE_EN = (30, 30, 30)
_COLOR_EXAMPLE_JA = (40, 90, 190)
_COLOR_SEPARATOR = (210, 210, 210)


def _render_page(entries: list[dict], page_no: int, total_pages: int) -> Image.Image:
    img = Image.new("RGB", (PAGE_W, PAGE_H), color="white")
    draw = ImageDraw.Draw(img)

    f_title = _font(_JP_BOLD_FONT_PATH, _JP_FONT_PATH, 34)
    f_section = _font(_JP_BOLD_FONT_PATH, _JP_FONT_PATH, 24)
    f_badge = _font(_EN_FONT_PATH, _EN_FONT_PATH, 22)
    f_word = _font(_EN_BOLD_FONT_PATH, _EN_FONT_PATH, 42)
    f_pos = _font(_JP_FONT_PATH, _JP_FONT_PATH, 20)
    f_meaning = _font(_JP_BOLD_FONT_PATH, _JP_FONT_PATH, 28)
    f_example_en = _font(_EN_FONT_PATH, _EN_FONT_PATH, 22)
    f_example_ja = _font(_JP_FONT_PATH, _JP_FONT_PATH, 20)

    draw.rectangle([0, 0, PAGE_W, HEADER_H], fill=_COLOR_HEADER_BG)
    draw.text((MARGIN_X, 28), "英検3級 でる順 単語帳（サンプル）", font=f_title, fill=_COLOR_HEADER_TEXT)
    start_no = entries[0]["head_no"]
    end_no = entries[-1]["head_no"]
    draw.text(
        (MARGIN_X, 90),
        f"Section {page_no}/{total_pages}    見出し語番号 {start_no:04d}~{end_no:04d}",
        font=f_section,
        fill=_COLOR_HEADER_TEXT,
    )

    body_h = PAGE_H - HEADER_H - FOOTER_MARGIN
    entry_h = body_h // ENTRIES_PER_PAGE

    for i, entry in enumerate(entries):
        top = HEADER_H + i * entry_h
        badge_w, badge_h = 100, 42
        draw.rectangle([MARGIN_X, top + 8, MARGIN_X + badge_w, top + 8 + badge_h], fill=_COLOR_BADGE_BG)
        draw.text(
            (MARGIN_X + 14, top + 14),
            f"{entry['head_no']:04d}",
            font=f_badge,
            fill=_COLOR_BADGE_TEXT,
        )

        word_x = MARGIN_X + badge_w + 24
        draw.text((word_x, top), entry["word"], font=f_word, fill=_COLOR_WORD)
        word_w = draw.textlength(entry["word"], font=f_word)
        draw.text((word_x + word_w + 14, top + 14), f"[{entry['pos_tag']}]", font=f_pos, fill=_COLOR_POS_TAG)

        draw.text((word_x, top + 58), entry["meaning_ja"], font=f_meaning, fill=_COLOR_MEANING)
        draw.text((word_x, top + 96), f"\u25b8 {entry['example_en']}", font=f_example_en, fill=_COLOR_EXAMPLE_EN)
        draw.text((word_x, top + 124), f"   {entry['example_ja']}", font=f_example_ja, fill=_COLOR_EXAMPLE_JA)

        sep_y = top + entry_h - 6
        draw.line([MARGIN_X, sep_y, PAGE_W - MARGIN_X, sep_y], fill=_COLOR_SEPARATOR, width=2)

    return img


def _assign_pages_to_files(pages: list[list[dict]], num_files: int, chunk_size: int) -> list[list[list[dict]]]:
    """ページ（8語ずつのかたまり）を、chunk_size 連続ページごとに num_files 個の
    ファイルへ順繰りに割り振る。同じチャンク内は元の連続順を保つが、チャンク間は
    ファイルをまたいで飛び飛びになる（実データのスキャン写真が撮影タイミングごとに
    別ファイルへ散らばっていた状態を再現）。
    """
    buckets: list[list[list[dict]]] = [[] for _ in range(num_files)]
    for page_idx, page_entries in enumerate(pages):
        chunk_idx = page_idx // chunk_size
        file_idx = chunk_idx % num_files
        buckets[file_idx].append(page_entries)
    return [b for b in buckets if b]


def build_fixture(
    seed: int = 42,
    total: int = 300,
    num_files: int = NUM_FILES,
    chunk_size: int = PAGES_PER_FILE_CHUNK,
    out_dir: Path | None = None,
) -> list[Path]:
    words = _build_word_list(seed=seed, total=total)

    root = out_dir or FIXTURE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    for stale in root.glob("*.pdf"):
        stale.unlink()

    pages: list[list[dict]] = [words[i : i + ENTRIES_PER_PAGE] for i in range(0, len(words), ENTRIES_PER_PAGE)]
    file_page_groups = _assign_pages_to_files(pages, num_files=num_files, chunk_size=chunk_size)

    pdf_paths: list[Path] = []
    manifest: list[dict] = []
    used_names: set[str] = set()
    for file_pages in file_page_groups:
        local_total = len(file_pages)
        images = [_render_page(page_entries, i + 1, local_total) for i, page_entries in enumerate(file_pages)]

        first_word = file_pages[0][0]["word"]
        name = f"{first_word}.pdf"
        suffix = 2
        while name in used_names:
            name = f"{first_word}_{suffix}.pdf"
            suffix += 1
        used_names.add(name)

        pdf_path = root / name
        images[0].save(str(pdf_path), save_all=True, append_images=images[1:], format="PDF")
        pdf_paths.append(pdf_path)

        head_nos: list[int] = []
        for local_page_idx, page_entries in enumerate(file_pages):
            for entry in page_entries:
                entry["source_file"] = name
                entry["page_in_source_file"] = local_page_idx + 1
                head_nos.append(entry["head_no"])
        manifest.append({"file": name, "pages": local_total, "head_nos": sorted(head_nos)})

    ANSWER_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ANSWER_KEY_PATH.write_text(
        json.dumps({"files": manifest, "words": words}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return pdf_paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--total", type=int, default=300)
    parser.add_argument("--num-files", type=int, default=NUM_FILES)
    parser.add_argument("--chunk-size", type=int, default=PAGES_PER_FILE_CHUNK)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()

    pdf_paths = build_fixture(
        seed=args.seed,
        total=args.total,
        num_files=args.num_files,
        chunk_size=args.chunk_size,
        out_dir=args.out_dir,
    )
    print(f"生成完了: {len(pdf_paths)}個のPDF")
    for p in pdf_paths:
        print(f"  {p}")
    print(f"正解データ: {ANSWER_KEY_PATH}")


if __name__ == "__main__":
    main()
