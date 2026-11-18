"""永続的ファイルベースメモリー（ロジック層）。

ClaudeCode 相当の「スレッド／セッションをまたいで共有される永続メモリー」を
実装する。役割はここまで（それだけ）:

1. memory_dir 配下に User / Feedback / Project / Reference の4種類の
   メモリーを YAML frontmatter 付き Markdown ファイルとして保存する。
2. メモリーはユーザー別に持つ（memory_dir/users/<ユーザー>/<type>/<name>.md）。
   ユーザー別になる前からある memory_dir/<type>/<name>.md は「共通」のメモリーとして
   全ユーザーが読み書きできる。ユーザーのメモリーには公開範囲（private / shared）があり、
   他のユーザーに見せるのは shared のものだけ（見せる・見せないはこのモジュールで機械的に
   決め、LLM の判断には任せない）。種類ごとの公開範囲・強制的に private にする目印は
   config.ini [memory] で変えられる（MemoryPolicy。load_config() が memory_dir ごとに登録する）。
3. 索引ファイル MEMORY.md を、保存されているメモリー群から都度再構築する
   （索引を差分更新せず常に全再構築することで整合ズレを防ぐ。共通の分は memory_dir 直下、
   ユーザーの分は users/<ユーザー>/ 直下）。
4. システムプロンプトへ差し込むための索引テキストを、会話のユーザーごとに組み立てる
   （{{memory}} プレースホルダー用、200行超は切り詰め。自分 → 共通 → 他のユーザーの順）。
5. ファイルから読んだ内容を記録するメモリーには、元ファイルの sha256 を
   frontmatter の sources に残し、読むときに今の sha256 と照合する
   （一致すれば元ファイルを開き直さずに内容を使える）。

Chainlit には依存しない。ツール層（tools/memory_tools.py）の @tool 関数がこのモジュールの
関数を呼び、例外を "Error: ..." 形式の文字列へ変換して LLM に返す。

賢い仕掛け（動的import・全文検索エンジン等）は入れない。ファイルを読み書き
して素朴なキーワード一致で検索する、それだけ。
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from pathlib import Path

import yaml

from .skills import _split_frontmatter_text

logger = logging.getLogger(__name__)

# 5種類のメモリータイプ（サブディレクトリ名でもある）。
MEMORY_TYPES = ("user", "feedback", "project", "reference", "glossary")

# ユーザー別のメモリーを置く memory_dir 直下のフォルダ名（type 名と重ならない）。
USERS_DIRNAME = "users"

# 公開範囲。private は所有者だけ、shared は全ユーザーが検索・読み取りできる（書き換えは所有者だけ）。
VISIBILITIES = ("private", "shared")
Visibility = Literal["private", "shared"]
# 種類ごとの公開範囲の既定（config.ini [memory] の <type>_visibility の既定と同じ）。
# private の種類は指定に関係なく常に個人用、shared の種類は LLM の指定に従う（省略時は shared）。
DEFAULT_TYPE_VISIBILITY: dict[str, Visibility] = {
    "user": "private",
    "feedback": "shared",
    "project": "shared",
    "reference": "shared",
    "glossary": "shared",
}

# name 検証ルール: ASCII の英数字・ハイフン・アンダースコアのみ、1〜64文字（ファイル名として安全にするため）。
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_NAME_MAX = 64
# 所有者（フォルダ名）の検証。呼び出し側で skill_drafts.draft_owner_name() により整えた名前を渡す前提で、
# ここではパスの区切り・親フォルダ参照だけを拒否する。
_OWNER_FORBIDDEN_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_OWNER_MAX = 128

_INDEX_FILENAME = "MEMORY.md"
# app.py が起動時のシステムプロンプトの {{memory}} に残し、セッションごとに render_memory_block() の結果へ
# 差し替える目印（索引は会話のユーザーごとに違うため）。
MEMORY_INDEX_MARKER = "<<MEMORY_INDEX>>"
_INDEX_MAX_LINES = 200
# {{memory}} に載せる用語（glossary）の最大行数（ほかの種類の行が少なければ、空いた分まで増やす）。
_INDEX_GLOSSARY_MAX_LINES = 50
_INDEX_ENTRY_MAX_CHARS = 150

# 元ファイル付きメモリーの上限。ファイルの本文をまるごと写すと read_memory のたびに
# コンテキストが膨らむため、関係する部分（値・表・該当ページの要点）だけに絞らせる。
SOURCED_CONTENT_MAX_CHARS = 4000
SOURCE_FILES_MAX = 10
_HASH_CHUNK_BYTES = 1024 * 1024

# 他のユーザーに見せてはいけない内容（個人情報・認証情報・機密の表示）の目印。(設定名, 英語のラベル, パターン)。
# 有効なものに1つでも当たれば、指定に関係なく個人用（private）にし、共有済みのものも他のユーザーには見せない。
# 取りこぼしより誤検知（共有されないだけ）の方が害が小さいため、広めに当てる。
# どれを有効にするかは config.ini [memory] force_private_checks で選ぶ（社内で共有したい情報に当たるものは外せる）。
_SENSITIVE_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("email", "email address", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    (
        "phone",
        "phone number",
        re.compile(r"(?<!\d)0\d{1,4}[-‐－−(（ ]\d{1,4}[-‐－−)） ]?\d{3,4}(?!\d)|(?<!\d)0[5789]0\d{8}(?!\d)"),
    ),
    ("card_id", "card or ID number", re.compile(r"(?<!\d)\d{4}[- ]?\d{4}[- ]?\d{4}(?:[- ]?\d{4})?(?!\d)")),
    (
        "credential",
        "credential or personal ID",
        re.compile(
            r"パスワード|暗証番号|口座番号|マイナンバー|個人番号|クレジットカード|\bpassword\b|\bpasswd\b|\bapi[_ -]?key\b|\bsecret\b",
            re.IGNORECASE,
        ),
    ),
    ("confidential_label", "confidentiality label", re.compile(r"社外秘|極秘|部外秘|関係者外秘|\bconfidential\b", re.IGNORECASE)),
    ("personal_path", "personal folder path", re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s]+", re.IGNORECASE)),
)
# force_private_checks に書ける値（表示順）。
PRIVATE_CHECKS: tuple[str, ...] = tuple(check for check, _, _ in _SENSITIVE_PATTERNS)
_CHECK_LABELS: dict[str, str] = {check: label for check, label, _ in _SENSITIVE_PATTERNS}
# 既定で有効な目印（config.ini の force_private_checks の既定と同じ）。社内のローカル運用が前提のため、
# 社内で共有して困らないもの（メール・電話・機密の表示・個人フォルダのパス）は既定では見ない。
DEFAULT_PRIVATE_CHECKS: frozenset[str] = frozenset({"card_id", "credential"})
# 目印を見ない種類（config.ini の force_private_exempt_types の既定と同じ）。用語集は社内用語の定義なので
# 「パスワード」「API Key」等が語として普通に出てくる。目印で private に倒すと、glossary_visibility = shared に
# しても用語が個人用になり、設定ダッシュボードで共有に直しても戻されていた。
DEFAULT_PRIVATE_CHECK_EXEMPT_TYPES: frozenset[str] = frozenset({"glossary"})

# LLM に渡す振り分け基準の既定（config.ini [memory] shared_criteria / private_criteria の既定と同じ）。
DEFAULT_SHARED_CRITERIA = "know-how that helps anyone (how a task was done, corrections, where shared documents are)"
DEFAULT_PRIVATE_CRITERIA = "anything about the user personally or their family"


# ランクを設定していないユーザー・共通のメモリーのランク（config.ini [memory] default_rank の既定と同じ）。
DEFAULT_RANK = 1


@dataclass(frozen=True)
class MemoryPolicy:
    """ユーザーのメモリーの公開範囲と優先順位の規則（config.ini [memory]）。

    Attributes:
        type_visibility: 種類ごとの公開範囲。LLM の保存・更新はこれに従う（private の種類は個人用、shared の種類は共有）。
            設定ダッシュボードで人が選んだ公開範囲は、private の種類でなければそのまま使う。
        checks: 有効な個人情報・機密の目印（PRIVATE_CHECKS のうち）。当たれば常に個人用。
        check_exempt_types: checks を見ない種類（公開範囲は type_visibility だけで決まる）。
        ranks: ユーザー（メモリーの所有者のフォルダ名）ごとのランク。数が大きいほど上位（上司）。
            内容が食い違うメモリーは、所有者のランクが高い方に従わせる（同じなら自分のもの）。
        default_rank: ranks に無いユーザーと、共通のメモリーのランク。
    """

    type_visibility: Mapping[str, Visibility] = field(default_factory=lambda: dict(DEFAULT_TYPE_VISIBILITY))
    checks: frozenset[str] = DEFAULT_PRIVATE_CHECKS
    check_exempt_types: frozenset[str] = DEFAULT_PRIVATE_CHECK_EXEMPT_TYPES
    ranks: Mapping[str, int] = field(default_factory=dict)
    default_rank: int = DEFAULT_RANK

    def private_types(self) -> tuple[str, ...]:
        # 書かれていない種類は既定（DEFAULT_TYPE_VISIBILITY）に従う（以前は private とみなしたため、
        # 種類を足したときに古い書き方の規則のままだと新しい種類が黙って個人用になった）。
        return tuple(
            t for t in MEMORY_TYPES if self.type_visibility.get(t, DEFAULT_TYPE_VISIBILITY.get(t, "private")) == "private"
        )

    def checks_for(self, memory_type: str) -> frozenset[str]:
        """memory_type の内容に当てる目印（check_exempt_types の種類なら空）。"""
        return frozenset() if memory_type in self.check_exempt_types else self.checks

    def rank_of(self, owner: str | None) -> int:
        """所有者（None は共通）のランク。"""
        return self.default_rank if owner is None else self.ranks.get(owner, self.default_rank)

    @property
    def uses_ranks(self) -> bool:
        """ランクを設定しているか（していなければ一覧にランクを出さない）。"""
        return bool(self.ranks)


DEFAULT_POLICY = MemoryPolicy()
# memory_root（解決済みパスの文字列）ごとの規則。load_config() が set_policy() で登録する
# （設定ダッシュボードは複数のインスタンスを扱うため、プロセス全体で1つにはしない）。未登録なら DEFAULT_POLICY。
_POLICIES: dict[str, MemoryPolicy] = {}


def _policy_key(memory_root: Path) -> str:
    return str(Path(memory_root).resolve())


def set_policy(memory_root: Path, policy: MemoryPolicy) -> None:
    """memory_root のメモリーに使う公開範囲の規則を登録する（load_config() から呼ぶ）。"""
    _POLICIES[_policy_key(memory_root)] = policy


def policy_for(memory_root: Path) -> MemoryPolicy:
    """memory_root に登録された公開範囲の規則（未登録なら DEFAULT_POLICY）。"""
    return _POLICIES.get(_policy_key(memory_root), DEFAULT_POLICY)


def render_visibility_block(shared_criteria: str, private_criteria: str, policy: MemoryPolicy) -> str:
    """system_prompt.md の {{memory_visibility}} へ差し込む、公開範囲の決め方の説明を組み立てる。"""
    # 公開範囲は LLM に選ばせない（ツールに visibility の引数は無い）。種類ごとの設定で決まることを伝え、
    # 管理者の振り分け基準は「どの種類で保存するか」の基準として渡す。
    private_types = policy.private_types()
    shared_types = [t for t in MEMORY_TYPES if t not in private_types]
    if not shared_types:
        return "All memories are saved as private (only the current user can find and read them)."

    def _indent(text: str) -> str:
        # 基準は管理者の自由記述（改行可・表示言語のまま）。見出しの下にまとまって見えるよう字下げする。
        return "\n".join(f"  {line}" for line in text.strip().splitlines() if line.strip())

    lines = ["Set automatically from the memory_type by the system settings (you do not choose it)."]
    if private_types:
        lines.append(f"- Private (only the current user can find and read them): {'/'.join(private_types)}")
    lines.append(f"- Shared (every user of this system can find and read them): {'/'.join(shared_types)}")
    checked_types = [t for t in MEMORY_TYPES if policy.checks_for(t)]
    if policy.checks and checked_types:
        labels = ", ".join(_CHECK_LABELS[c] for c in PRIVATE_CHECKS if c in policy.checks)
        exempt = [t for t in MEMORY_TYPES if t not in checked_types]
        note = f" (not for {'/'.join(exempt)})" if exempt else ""
        lines.append(f"- Content with any of these is saved as private automatically{note}: {labels}.")
    lines += ["So choose the memory_type with this in mind:", "- Save as a shared type:", _indent(shared_criteria)]
    if private_types:
        lines += [f"- Save as {'/'.join(private_types)} (private):", _indent(private_criteria)]
    else:
        lines += ["- Do not save these (every memory is shared):", _indent(private_criteria)]
    return "\n".join(lines)

SourceState = Literal["unchanged", "changed", "missing"]


@dataclass(frozen=True)
class SourceFile:
    """メモリーの内容の元になったファイルの、記録した時点の指紋。

    Attributes:
        path: 元ファイルの絶対パス。
        sha256: 記録した時点の内容の sha256（16進）。
        size: 記録した時点のサイズ（バイト）。照合でサイズが違えばハッシュを計算せずに「変わった」とする。
    """

    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class SourceCheck:
    """元ファイル1件の照合結果。

    Attributes:
        source: 記録してある指紋。
        state: "unchanged"（sha256 が一致）| "changed"（内容が違う）| "missing"（読めない・無い）。
    """

    source: SourceFile
    state: SourceState


@dataclass(frozen=True)
class Memory:
    """1件のメモリー（frontmatter のメタデータ＋本文）。

    Attributes:
        name: メモリーの名前（同じ所有者の中で4type横断でユニーク、拡張子を除くファイル名）。
        description: 一行の説明文（索引 MEMORY.md にもそのまま載る）。
        memory_type: "user" | "feedback" | "project" | "reference" | "glossary"。
        content: frontmatter を除いた本文（Markdown）。
        path: このメモリーの実ファイルパス。
        sources: 内容の元になったファイルの指紋（frontmatter の sources。無ければ空）。
        owner: 所有者（ユーザーのフォルダ名）。None なら共通のメモリー。
        visibility: "private" | "shared"。共通のメモリーは常に "shared"。
        author: 作ったユーザー（frontmatter の author。記録が無ければ None）。
        updated_by: 最後に本文を書き換えたユーザー（frontmatter の updated_by。記録が無ければ None）。
    """

    name: str
    description: str
    memory_type: str
    content: str
    path: Path
    sources: tuple[SourceFile, ...] = ()
    owner: str | None = None
    visibility: Visibility = "shared"
    author: str | None = None
    updated_by: str | None = None


def ensure_dirs(memory_root: Path, owner: str | None = None) -> None:
    """memory_root（owner を渡せばそのユーザーのフォルダ）と4種類のtypeサブディレクトリを作成する。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        owner: 所有者。None なら共通のフォルダ（memory_root 直下）。
    """
    root = scope_root(memory_root, owner)
    root.mkdir(parents=True, exist_ok=True)
    for memory_type in MEMORY_TYPES:
        (root / memory_type).mkdir(parents=True, exist_ok=True)


def _validate_name(name: str) -> None:
    """name がスラッグとして安全かを検証する。不正なら ValueError。"""
    if not name or len(name) > _NAME_MAX or not _NAME_RE.match(name):
        raise ValueError(
            f"name must be an ASCII slug: ASCII letters (a-z, A-Z), digits, hyphens and underscores, "
            f"up to {_NAME_MAX} characters (no Japanese or other non-ASCII letters; translate or romanize them, "
            f"e.g. 'monthly-report-format', and keep the Japanese words in description): {name}"
        )


def _validate_type(memory_type: str) -> None:
    """memory_type が既定の4種のいずれかかを検証する。不正なら ValueError。"""
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"memory_type must be one of {MEMORY_TYPES}: {memory_type}")


def _validate_owner(owner: str) -> None:
    """owner がフォルダ名として安全かを検証する。不正なら ValueError。"""
    if not owner or len(owner) > _OWNER_MAX or owner in (".", "..") or _OWNER_FORBIDDEN_RE.search(owner):
        raise ValueError(f"invalid memory owner: {owner!r}")


def scope_root(memory_root: Path, owner: str | None) -> Path:
    """owner のメモリーを置くフォルダを返す（None なら共通 = memory_root 自身）。

    Raises:
        ValueError: owner がフォルダ名として不正な場合。
    """
    if owner is None:
        return memory_root
    _validate_owner(owner)
    return memory_root / USERS_DIRNAME / owner


def _safe_memory_path(memory_root: Path, memory_type: str, name: str, owner: str | None = None) -> Path:
    """<owner のフォルダ>/<type>/<name>.md を返す。境界外・不正値なら ValueError。

    tools.py の _safe_path と同じ二重防御の思想: name・owner は正規表現で
    ディレクトリ区切り文字自体を拒否した上で、resolve() + is_relative_to()
    でも境界を検証する。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        memory_type: "user" | "feedback" | "project" | "reference" | "glossary"。
        name: メモリーの名前。
        owner: 所有者。None なら共通。

    Returns:
        検証済みの絶対パス。

    Raises:
        ValueError: memory_type・name・owner が不正、または解決後のパスが
            所有者のフォルダ配下に収まらない場合。
    """
    _validate_type(memory_type)
    _validate_name(name)
    root = scope_root(memory_root, owner).resolve()
    candidate = (root / memory_type / f"{name}.md").resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Access outside the memory directory is not allowed: {name}")
    return candidate


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    """先頭 `---` で囲まれた YAML frontmatter を (dict, 本文) で返す。

    区切りは skills._parse_frontmatter と同じ（行全体が `---` の行。src/skills.py の
    _split_frontmatter_text を共用）だが、本文（本体テキスト）も呼び出し元へ返す点が異なる
    （メモリーは content の読み書きが必須のため）。単純に "---" で3分割すると、説明文や値に
    "---" が含まれたとき（ダッシュボードの「AIで翻訳」で混入しうる）そこで切れ、メモリーが読めなくなる。

    Args:
        text: メモリーファイルの全文（UTF-8 でデコード済み）。

    Returns:
        (frontmatter の dict, frontmatter を除いた本文) のタプル。
        frontmatter が無い/壊れている/dict でない場合は (None, text)。
    """
    split = _split_frontmatter_text(text)
    if split is None:
        return None, text
    parts = ["", split[0], split[1]]
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        logger.warning("Failed to parse memory frontmatter YAML: %s", e)
        return None, text
    if not isinstance(data, dict):
        return None, text
    return data, parts[2].lstrip("\n")


def _now() -> str:
    """現在時刻を ISO8601（UTC）文字列で返す（frontmatter の created/updated 用）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _serialize(
    name: str,
    description: str,
    memory_type: str,
    content: str,
    *,
    created: str,
    updated: str,
    sources: tuple[SourceFile, ...] = (),
    visibility: str | None = None,
    author: str | None = None,
    updated_by: str | None = None,
) -> str:
    """メモリー1件を frontmatter 付き Markdown 文字列へ組み立てる（visibility は共通のメモリーでは書かない）。"""
    frontmatter: dict = {
        "name": name,
        "description": description,
        "type": memory_type,
    }
    if visibility is not None:
        frontmatter["visibility"] = visibility
    if author is not None:
        frontmatter["author"] = author
    if updated_by is not None:
        frontmatter["updated_by"] = updated_by
    frontmatter["created"] = created
    frontmatter["updated"] = updated
    if sources:
        frontmatter["sources"] = [{"path": s.path, "sha256": s.sha256, "size": s.size} for s in sources]
    header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    return f"---\n{header}---\n\n{content.rstrip()}\n"


def _parse_sources(raw: object, path: Path) -> tuple[SourceFile, ...]:
    """frontmatter の sources を SourceFile のタプルへ変換する。壊れた要素は警告して読み飛ばす。"""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        logger.warning("Ignoring the memory sources because it is not a list: %s", path)
        return ()
    sources: list[SourceFile] = []
    for item in raw:
        if (
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("sha256"), str)
            and isinstance(item.get("size"), int)
        ):
            sources.append(SourceFile(item["path"], item["sha256"], item["size"]))
        else:
            logger.warning("Ignoring an invalid memory source entry: %s: %r", path, item)
    return tuple(sources)


def _load(path: Path, owner: str | None = None) -> Memory | None:
    """メモリーファイル1件を読み込み Memory へ変換する。壊れていれば None。

    ユーザーのメモリーで visibility が無い・不正なら、他のユーザーへ漏らさないよう private とみなす。
    """
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)
    if frontmatter is None:
        logger.warning("Skipping because the memory frontmatter cannot be read: %s", path)
        return None
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    memory_type = frontmatter.get("type")
    if not isinstance(name, str) or not isinstance(description, str) or memory_type not in MEMORY_TYPES:
        logger.warning("Skipping because the memory frontmatter is invalid: %s", path)
        return None
    if owner is None:
        visibility: Visibility = "shared"
    else:
        raw_visibility = frontmatter.get("visibility")
        visibility = raw_visibility if raw_visibility in VISIBILITIES else "private"
    author = frontmatter.get("author")
    updated_by = frontmatter.get("updated_by")
    return Memory(
        name=name,
        description=description,
        memory_type=memory_type,
        content=body.rstrip("\n"),
        path=path,
        sources=_parse_sources(frontmatter.get("sources"), path),
        owner=owner,
        visibility=visibility,
        # ユーザー名の記録が無い（ユーザー別になる前の）ユーザーのメモリーは、所有者が作ったものとみなす。
        author=author if isinstance(author, str) and author else owner,
        updated_by=updated_by if isinstance(updated_by, str) and updated_by else None,
    )


def _sha256_of(path: Path) -> str:
    """ファイルの sha256 を、全体を読み込まずに少しずつ計算する。"""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_sources(paths: list[Path]) -> tuple[SourceFile, ...]:
    """元ファイルの今の指紋（sha256・サイズ）を取る。

    Args:
        paths: 元ファイルの絶対パス（呼び出し側で @N・相対パスを解決済みのもの）。

    Returns:
        paths の順の SourceFile（同じパスは1件にまとめる）。

    Raises:
        ValueError: 件数が SOURCE_FILES_MAX を超える、絶対パスでない、ファイルでない・読めない場合。
    """
    unique: list[Path] = []
    for p in paths:
        if p not in unique:
            unique.append(p)
    if len(unique) > SOURCE_FILES_MAX:
        raise ValueError(f"source_files can list at most {SOURCE_FILES_MAX} files: {len(unique)}")
    sources: list[SourceFile] = []
    for p in unique:
        if not p.is_absolute():
            raise ValueError(f"source_files must be absolute paths: {p}")
        if not p.is_file():
            raise ValueError(f"source file not found (or not a file): {p}")
        try:
            sources.append(SourceFile(str(p), _sha256_of(p), p.stat().st_size))
        except OSError as e:
            raise ValueError(f"cannot read the source file: {p}: {e}") from e
    return tuple(sources)


def check_sources(mem: Memory) -> list[SourceCheck]:
    """メモリーの元ファイルを今の内容と照合する。

    サイズが違えばハッシュを計算せずに "changed"。サイズが同じなら sha256 を計算して比べる
    （更新日時は使わない。コピーや展開で変わっても内容が同じなら信用してよいため）。

    Args:
        mem: 照合するメモリー。

    Returns:
        mem.sources の順の照合結果（sources が無ければ空リスト）。
    """
    results: list[SourceCheck] = []
    for source in mem.sources:
        p = Path(source.path)
        try:
            if not p.is_file():
                state: SourceState = "missing"
            elif p.stat().st_size != source.size:
                state = "changed"
            else:
                state = "unchanged" if _sha256_of(p) == source.sha256 else "changed"
        except OSError as e:
            logger.warning("Cannot check a memory source file: %s: %s", p, e)
            state = "missing"
        results.append(SourceCheck(source, state))
    return results


def _validate_sourced_content(content: str, sources: tuple[SourceFile, ...]) -> None:
    """元ファイル付きのメモリーの本文が上限を超えていれば ValueError。"""
    if sources and len(content) > SOURCED_CONTENT_MAX_CHARS:
        raise ValueError(
            f"content of a memory with source_files must be at most {SOURCED_CONTENT_MAX_CHARS} characters "
            f"({len(content)} given). Keep only the parts that answer the kind of question "
            "(values, table rows, key points with page numbers), not the whole file."
        )


def detect_sensitive(text: str, checks: frozenset[str] | None = None) -> list[str]:
    """他のユーザーに見せてはいけない内容の目印を探し、当たった種類（英語のラベル）を返す。

    Args:
        text: 調べる文字列。
        checks: 有効な目印（PRIVATE_CHECKS のうち）。None なら全部。
    """
    return [
        label
        for check, label, pattern in _SENSITIVE_PATTERNS
        if (checks is None or check in checks) and pattern.search(text)
    ]


def _sensitive_text(description: str, content: str, sources: tuple[SourceFile, ...]) -> str:
    return "\n".join([description, content, *(s.path for s in sources)])


def resolve_visibility(
    memory_type: str,
    requested: str | None,
    description: str,
    content: str,
    sources: tuple[SourceFile, ...] = (),
    policy: MemoryPolicy = DEFAULT_POLICY,
) -> tuple[Visibility, list[str]]:
    """ユーザーのメモリーの公開範囲を決める。

    requested が None なら種類の設定（policy.type_visibility）どおり。policy で private の種類、または
    有効な個人情報・機密の目印（check_exempt_types の種類では見ない）を含むなら、指定に関係なく private にする。
    LLM のツールは requested を渡さない（公開範囲は設定で決まる）。requested を渡すのは設定ダッシュボードだけ。

    Returns:
        (公開範囲, shared にできなかった理由（shared を求めて private にしたときだけ。英語のラベル）)。

    Raises:
        ValueError: requested が private / shared のどちらでもない場合。
    """
    if requested is not None and requested not in VISIBILITIES:
        raise ValueError(f"visibility must be one of {VISIBILITIES}: {requested}")
    if requested == "private":
        return "private", []
    reasons: list[str] = []
    if memory_type in policy.private_types():
        reasons.append(f"memory_type {memory_type} is always private")
    reasons += detect_sensitive(_sensitive_text(description, content, sources), policy.checks_for(memory_type))
    return ("private", reasons) if reasons else ("shared", [])


def _iter_memories(memory_root: Path, memory_type: str | None = None, owner: str | None = None) -> list[Memory]:
    """owner のフォルダ（None なら共通）の対象type配下（省略時は全type）の *.md を走査する。"""
    types = (memory_type,) if memory_type else MEMORY_TYPES
    root = scope_root(memory_root, owner)
    results: list[Memory] = []
    for t in types:
        type_dir = root / t
        if not type_dir.is_dir():
            continue
        for entry in sorted(type_dir.glob("*.md")):
            mem = _load(entry, owner)
            if mem is not None:
                results.append(mem)
    return results


def list_owners(memory_root: Path) -> list[str]:
    """メモリーのフォルダを持つユーザー（フォルダ名）の一覧（名前順）。"""
    users_dir = memory_root / USERS_DIRNAME
    if not users_dir.is_dir():
        return []
    return sorted(p.name for p in users_dir.iterdir() if p.is_dir() and not _OWNER_FORBIDDEN_RE.search(p.name))


def all_memories(memory_root: Path, memory_type: str | None = None) -> list[Memory]:
    """共通と全ユーザーのメモリーを、公開範囲に関係なくすべて返す（設定ダッシュボード・索引の再構築用）。

    Raises:
        ValueError: memory_type が不正な値の場合。
    """
    if memory_type is not None:
        _validate_type(memory_type)
    result = _iter_memories(memory_root, memory_type)
    for owner in list_owners(memory_root):
        result += _iter_memories(memory_root, memory_type, owner)
    return result


def is_visible_to(mem: Memory, viewer: str | None, policy: MemoryPolicy = DEFAULT_POLICY) -> bool:
    """viewer（会話のユーザー）がこのメモリーを検索・読み取りできるか。

    自分のもの・共通のものは常に見える。他のユーザーのものは shared で、しかも今の規則（policy）で
    private の種類でなく、今の内容に有効な個人情報・機密の目印が無いものだけ（保存後に手で書き換えられた
    場合や、あとから規則を厳しくした場合にも漏らさないため、毎回確かめる）。
    """
    if mem.owner is None or mem.owner == viewer:
        return True
    if mem.visibility != "shared" or mem.memory_type in policy.private_types():
        return False
    checks = policy.checks_for(mem.memory_type)
    return not checks or not detect_sensitive(_sensitive_text(mem.description, mem.content, mem.sources), checks)


def is_private_to_owner(mem: Memory, policy: MemoryPolicy = DEFAULT_POLICY) -> bool:
    """所有者本人にしか見せないメモリーか（共通のメモリーは False）。

    他のユーザーから見えるかの判定（is_visible_to）と同じ基準: private の印・規則で private の種類・
    有効な個人情報・機密の目印のどれかがあれば個人用。
    """
    return mem.owner is not None and not is_visible_to(mem, None, policy)


def _viewer_rank(mem: Memory, viewer: str | None) -> int:
    """同じランクの中の並べ順: 自分（0）→ 共通（1）→ 他のユーザー（2）。似た内容があれば自分のものが先に目に入るようにする。"""
    if mem.owner is not None and mem.owner == viewer:
        return 0
    return 1 if mem.owner is None else 2


def _sorted_for(memories: list[Memory], viewer: str | None, policy: MemoryPolicy = DEFAULT_POLICY) -> list[Memory]:
    """所有者のランクが高い順（上司のものが先）、同じランクなら 自分 → 共通 → 他のユーザー の順に並べる。"""
    return sorted(
        memories,
        key=lambda m: (-policy.rank_of(m.owner), _viewer_rank(m, viewer), m.memory_type, m.name, m.owner or ""),
    )


def _find_memory(memory_root: Path, name: str, owner: str | None = None) -> Memory | None:
    """owner のフォルダ（None なら共通）の中で、name だけを手がかりに4type横断でメモリーを探す。"""
    _validate_name(name)
    for memory_type in MEMORY_TYPES:
        candidate = _safe_memory_path(memory_root, memory_type, name, owner)
        if candidate.is_file():
            return _load(candidate, owner)
    return None


def find_memory(memory_root: Path, name: str, owner: str | None = None) -> Memory | None:
    """owner のフォルダ（None なら共通）にある name のメモリー。無ければ None（公開範囲は見ない）。"""
    return _find_memory(memory_root, name, owner)


def _rebuild_scope_index(memory_root: Path, owner: str | None) -> None:
    root = scope_root(memory_root, owner)
    memories = sorted(_iter_memories(memory_root, owner=owner), key=lambda m: (m.memory_type, m.name))
    lines = [
        "# MEMORY.md",
        "",
        "Index of persistent memories. Rebuilt automatically by create_memory/update_memory/delete_memory.",
        "",
    ]
    if not memories:
        lines.append("(no memories saved)")
    else:
        for mem in memories:
            flag = " (private)" if owner is not None and mem.visibility == "private" else ""
            entry = f"- [{mem.memory_type}] {mem.name}{flag}: {mem.description}"
            lines.append(entry[:_INDEX_ENTRY_MAX_CHARS])
    root.mkdir(parents=True, exist_ok=True)
    (root / _INDEX_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def rebuild_index(memory_root: Path, owner: str | None = None) -> None:
    """MEMORY.md を再生成する（owner を渡せばそのユーザーの分、None なら共通の分）。

    索引は差分更新せず、create_memory/update_memory/delete_memory の
    末尾で毎回このように全再構築することで、ファイル群と索引の
    整合ズレを構造的に防ぐ。共通の MEMORY.md には共通のメモリーだけを載せ、
    ユーザーの MEMORY.md はそのユーザーのフォルダに置く（他のユーザーの個人用メモリーの説明を
    共通の場所に書き出さないため）。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        owner: 所有者。None なら共通。
    """
    _rebuild_scope_index(memory_root, owner)


def rebuild_all_indexes(memory_root: Path) -> None:
    """共通と全ユーザーの MEMORY.md を再生成する（手でファイルを動かした後の整え直し用）。"""
    _rebuild_scope_index(memory_root, None)
    for owner in list_owners(memory_root):
        _rebuild_scope_index(memory_root, owner)


def create_memory(
    memory_root: Path,
    name: str,
    description: str,
    memory_type: str,
    content: str,
    source_files: list[Path] | None = None,
    *,
    owner: str | None = None,
    visibility: str | None = None,
    author: str | None = None,
) -> Path:
    """新しいメモリーを保存し、MEMORY.md 索引を再構築する。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        name: 所有者の中で一意な名前（英数字・ハイフン・アンダースコアのみ）。
        description: 一行の説明文。
        memory_type: "user" | "feedback" | "project" | "reference" | "glossary"。
        content: メモリー本文。
        source_files: 本文の元になったファイルの絶対パス。指定すると今の sha256 を記録する
            （本文は SOURCED_CONTENT_MAX_CHARS 文字まで）。
        owner: 所有者。None なら共通のメモリーとして保存する。
        visibility: ユーザーのメモリーの公開範囲（None なら種類の設定どおり。LLM のツールは常に None）。
            memory_root に登録された規則（policy_for）に沿って resolve_visibility() が private に倒すことがある。
            共通のメモリーでは無視する。
        author: 作ったユーザー（frontmatter の author に記録する）。None なら owner。

    Returns:
        作成したファイルの絶対パス。

    Raises:
        ValueError: name/memory_type/owner/visibility が不正、description/content が空、
            同じ所有者に同名のメモリーが既に存在する、source_files が読めない・多すぎる、
            または元ファイル付きの本文が長すぎる場合。
    """
    if not description.strip():
        raise ValueError("description is empty")
    if not content.strip():
        raise ValueError("content is empty")
    path = _safe_memory_path(memory_root, memory_type, name, owner)
    if _find_memory(memory_root, name, owner) is not None:
        raise ValueError(f"A memory with the same name already exists: {name} (use update_memory to update it)")
    sources = fingerprint_sources(source_files) if source_files else ()
    _validate_sourced_content(content, sources)
    final_visibility = (
        None
        if owner is None
        else resolve_visibility(memory_type, visibility, description, content, sources, policy_for(memory_root))[0]
    )
    ensure_dirs(memory_root, owner)
    now = _now()
    path.write_text(
        _serialize(
            name,
            description.strip(),
            memory_type,
            content,
            created=now,
            updated=now,
            sources=sources,
            visibility=final_visibility,
            author=author if author is not None else owner,
        ),
        encoding="utf-8",
    )
    rebuild_index(memory_root, owner)
    logger.info("create_memory: name=%s type=%s owner=%s visibility=%s", name, memory_type, owner, final_visibility)
    return path


def update_memory(
    memory_root: Path,
    name: str,
    content: str,
    source_files: list[Path] | None = None,
    *,
    owner: str | None = None,
    updated_by: str | None = None,
) -> Path:
    """既存メモリーの本文を更新し、MEMORY.md 索引を再構築する（name/description/type は不変）。

    LLM の update_memory ツール用。ユーザーのメモリーの公開範囲は、今の値を引き継がずに種類の設定
    （config.ini [memory] <type>_visibility）と新しい本文の目印で決め直す（設定を変えた後や、以前 LLM が
    private を選んでいたメモリーも、次の更新から設定どおりになる）。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        name: 更新対象メモリーの名前。
        content: 新しい本文。
        source_files: None なら記録済みの元ファイルの指紋をそのまま残す。リストを渡すと
            その今の sha256 で置き換える（空リストなら元ファイルの記録を外す）。
        owner: 所有者。None なら共通のメモリーを更新する。
        updated_by: 書き換えたユーザー（frontmatter の updated_by に記録する）。None なら記録済みの値のまま。

    Returns:
        更新したファイルの絶対パス。

    Raises:
        ValueError: content が空、owner に name のメモリーが存在しない、
            source_files が読めない・多すぎる、または元ファイル付きの本文が長すぎる場合。
    """
    if not content.strip():
        raise ValueError("content is empty")
    mem = _find_memory(memory_root, name, owner)
    if mem is None:
        raise ValueError(f"Memory not found: {name}")
    return _rewrite(
        memory_root,
        mem,
        description=mem.description,
        memory_type=mem.memory_type,
        content=content,
        sources=mem.sources if source_files is None else fingerprint_sources(source_files),
        visibility=None,
        updated_by=updated_by,
    )


def edit_memory(
    memory_root: Path,
    name: str,
    *,
    description: str,
    memory_type: str,
    content: str,
    owner: str | None = None,
    visibility: str | None = None,
    updated_by: str | None = None,
    source_files: list[Path] | None = None,
    fingerprints: tuple[SourceFile, ...] | None = None,
    follow_policy: bool = False,
) -> Path:
    """既存メモリーの説明・種類・本文（ユーザーのものは公開範囲も）をまとめて書き換え、索引を再構築する。

    設定ダッシュボード（admin/memories.py）から人が直す用途と、用語集（src/glossary.py）の書き直し用。
    LLM 向けの update_memory と違い description と type も変えられる（type を変えるとファイルを移す）。
    created は残す。元ファイルの指紋（sources）は source_files を渡さなければそのまま残す。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        name: 書き換えるメモリーの名前（name 自体・所有者は変えない）。
        description: 新しい一行の説明文。
        memory_type: 新しい type。
        content: 新しい本文。
        owner: 所有者。None なら共通のメモリー。
        visibility: ユーザーのメモリーの新しい公開範囲（None なら今の値のまま）。
        updated_by: 書き換えた人（frontmatter の updated_by に記録する）。None なら記録済みの値のまま。
        source_files: 渡すとその今の sha256 で元ファイルの指紋を置き換える（None なら記録済みのまま）。
        fingerprints: 記録する元ファイルの指紋そのもの（記録済みの指紋を取り直さずに残したいとき。用語集の出典の追加用）。
            source_files と同時には渡せない。件数は SOURCE_FILES_MAX まで。
        follow_policy: True なら visibility を無視し、公開範囲を種類の設定と目印で決め直す（LLM が書き換える
            用語集用。今の値を引き継ぐと、以前 private になった用語が設定を shared にしても戻らなかった）。

    Returns:
        書き換え後のファイルの絶対パス。

    Raises:
        ValueError: memory_type/visibility が不正、description/content が空、name のメモリーが存在しない、
            source_files が読めない・多すぎる、source_files と fingerprints を両方渡した、
            または元ファイル付きの本文が長すぎる場合。
    """
    if not description.strip():
        raise ValueError("description is empty")
    if not content.strip():
        raise ValueError("content is empty")
    if source_files is not None and fingerprints is not None:
        raise ValueError("pass either source_files or fingerprints, not both")
    if fingerprints is not None and len(fingerprints) > SOURCE_FILES_MAX:
        raise ValueError(f"source_files can list at most {SOURCE_FILES_MAX} files: {len(fingerprints)}")
    _validate_type(memory_type)
    mem = _find_memory(memory_root, name, owner)
    if mem is None:
        raise ValueError(f"Memory not found: {name}")
    if fingerprints is not None:
        sources = tuple(fingerprints)
    else:
        sources = mem.sources if source_files is None else fingerprint_sources(source_files)
    return _rewrite(
        memory_root,
        mem,
        description=description.strip(),
        memory_type=memory_type,
        content=content,
        sources=sources,
        visibility=None if follow_policy else (visibility if visibility is not None else mem.visibility),
        updated_by=updated_by,
    )


def _rewrite(
    memory_root: Path,
    mem: Memory,
    *,
    description: str,
    memory_type: str,
    content: str,
    sources: tuple[SourceFile, ...],
    visibility: str | None,
    updated_by: str | None = None,
    rebuild: bool = True,
) -> Path:
    """mem を新しい内容で書き直す（created・author は残す。type が変われば移す）。

    visibility が None なら種類の設定どおり（resolve_visibility）。rebuild=False なら索引を作り直さない
    （まとめて書き換える呼び出し側が最後に1回だけ作り直す）。
    """
    _validate_sourced_content(content, sources)
    final_visibility = (
        None
        if mem.owner is None
        else resolve_visibility(memory_type, visibility, description, content, sources, policy_for(memory_root))[0]
    )
    frontmatter, _ = _split_frontmatter(mem.path.read_text(encoding="utf-8"))
    created = frontmatter.get("created") if frontmatter else None
    path = _safe_memory_path(memory_root, memory_type, mem.name, mem.owner)
    ensure_dirs(memory_root, mem.owner)
    path.write_text(
        _serialize(
            mem.name,
            description,
            memory_type,
            content,
            created=created or _now(),
            updated=_now(),
            sources=sources,
            visibility=final_visibility,
            author=mem.author,
            updated_by=updated_by if updated_by is not None else mem.updated_by,
        ),
        encoding="utf-8",
    )
    if path != mem.path:
        mem.path.unlink()
    if rebuild:
        rebuild_index(memory_root, mem.owner)
    logger.info("rewrite memory: name=%s type=%s owner=%s visibility=%s", mem.name, memory_type, mem.owner, final_visibility)
    return path


@dataclass(frozen=True)
class BulkResult:
    """まとめて操作した結果。

    Attributes:
        done: 操作したメモリー（操作前の Memory。削除なら消したもの）。
        failed: 操作できなかったもの（(所有者, 名前, 理由)）。
    """

    done: tuple[Memory, ...]
    failed: tuple[tuple[str | None, str, str], ...] = ()


def delete_memories(memory_root: Path, mems: list[Memory]) -> BulkResult:
    """メモリーをまとめて削除する（索引は所有者ごとに最後に1回だけ作り直す）。

    1件ずつ delete_memory すると、そのたびに所有者の全メモリーを読み直して索引を作るため、
    数百件で O(n²) になり設定ダッシュボードが固まっていた。
    """
    done: list[Memory] = []
    failed: list[tuple[str | None, str, str]] = []
    owners: set[str | None] = set()
    for mem in mems:
        try:
            mem.path.unlink()
        except FileNotFoundError:
            failed.append((mem.owner, mem.name, "not found"))
            continue
        except OSError as e:
            failed.append((mem.owner, mem.name, str(e)))
            continue
        done.append(mem)
        owners.add(mem.owner)
    for owner in owners:
        rebuild_index(memory_root, owner)
    logger.info("delete_memories: deleted=%d failed=%d", len(done), len(failed))
    return BulkResult(tuple(done), tuple(failed))


def change_memories(
    memory_root: Path,
    mems: list[Memory],
    *,
    memory_type: str | None = None,
    visibility: str | None = None,
    updated_by: str | None = None,
) -> BulkResult:
    """メモリーの種類・公開範囲をまとめて変える（説明・本文・元ファイルの指紋はそのまま）。

    memory_type / visibility は None なら今の値のまま。公開範囲は共通のメモリーには効かず、ユーザーの
    メモリーでも private の種類・目印に当たる内容は private になる（edit_memory と同じ規則）。
    索引は所有者ごとに最後に1回だけ作り直す。

    Raises:
        ValueError: memory_type / visibility が不正、または両方とも None の場合。
    """
    if memory_type is None and visibility is None:
        raise ValueError("pass memory_type or visibility")
    if memory_type is not None:
        _validate_type(memory_type)
    if visibility is not None and visibility not in VISIBILITIES:
        raise ValueError(f"visibility must be one of {VISIBILITIES}: {visibility}")
    done: list[Memory] = []
    failed: list[tuple[str | None, str, str]] = []
    owners: set[str | None] = set()
    for mem in mems:
        new_type = memory_type or mem.memory_type
        try:
            if new_type != mem.memory_type and _find_memory_other_type(memory_root, mem, new_type):
                raise ValueError(f"a memory named {mem.name} already exists in {new_type}")
            _rewrite(
                memory_root,
                mem,
                description=mem.description,
                memory_type=new_type,
                content=mem.content,
                sources=mem.sources,
                visibility=visibility if visibility is not None else mem.visibility,
                updated_by=updated_by,
                rebuild=False,
            )
        except (ValueError, OSError) as e:
            failed.append((mem.owner, mem.name, str(e)))
            continue
        done.append(mem)
        owners.add(mem.owner)
    for owner in owners:
        rebuild_index(memory_root, owner)
    logger.info("change_memories: type=%s visibility=%s changed=%d failed=%d", memory_type, visibility, len(done), len(failed))
    return BulkResult(tuple(done), tuple(failed))


def _find_memory_other_type(memory_root: Path, mem: Memory, memory_type: str) -> bool:
    """mem を memory_type へ移す先に、同じ名前の別のファイルがあるか（壊れた重複を上書きしないため）。"""
    target = _safe_memory_path(memory_root, memory_type, mem.name, mem.owner)
    return target != mem.path and target.exists()


def copy_memory(
    memory_root: Path,
    mem: Memory,
    *,
    owner: str | None,
    copied_by: str | None = None,
    source_policy: MemoryPolicy | None = None,
    rebuild: bool = True,
) -> Literal["copied", "skipped"]:
    """別のメモリーストア（別インスタンス等）の mem を、memory_root の owner のメモリーとして複製する。

    説明・種類・本文・元ファイルの指紋（sources）・作成者（author）はそのまま引き継ぎ、公開範囲は
    memory_root 側の規則（policy_for）で決め直す（共通のメモリーは書かない）。同じ名前のメモリーが
    コピー先に既にあれば置き換えずに飛ばす（置き換えたいときは、先にコピー先のものを削除する）。

    Args:
        memory_root: コピー先のメモリーストアのルート。
        mem: コピー元のメモリー。
        owner: コピー先の所有者。None なら共通のメモリー。
        copied_by: 最後に書き換えたユーザー（updated_by）として残す名前。
        source_policy: コピー元の公開範囲の規則（個人用かの判定に使う）。None なら DEFAULT_POLICY。
        rebuild: False なら索引を作り直さない（まとめてコピーする呼び出し側が最後に rebuild_index する）。

    Returns:
        "copied"（新規）| "skipped"（同じ名前のメモリーがコピー先にある。種類は問わない）。

    Raises:
        ValueError: name/memory_type/owner が不正な場合、または他のユーザーの個人用のメモリーを
            別の所有者（共通を含む）へコピーしようとした場合。
    """
    if owner != mem.owner and is_private_to_owner(mem, source_policy or DEFAULT_POLICY):
        raise ValueError("a private memory can be copied only to the same owner")
    path = _safe_memory_path(memory_root, mem.memory_type, mem.name, owner)
    if _find_memory(memory_root, mem.name, owner) is not None:
        return "skipped"
    final_visibility = (
        None
        if owner is None
        else resolve_visibility(mem.memory_type, mem.visibility, mem.description, mem.content, mem.sources, policy_for(memory_root))[0]
    )
    ensure_dirs(memory_root, owner)
    now = _now()
    path.write_text(
        _serialize(
            mem.name,
            mem.description,
            mem.memory_type,
            mem.content,
            created=now,
            updated=now,
            sources=mem.sources,
            visibility=final_visibility,
            author=mem.author if mem.author is not None else mem.owner,
            updated_by=copied_by,
        ),
        encoding="utf-8",
    )
    if rebuild:
        rebuild_index(memory_root, owner)
    logger.info("copy_memory: name=%s type=%s owner=%s", mem.name, mem.memory_type, owner)
    return "copied"


def delete_memory(memory_root: Path, name: str, owner: str | None = None) -> Path:
    """メモリーを削除し、MEMORY.md 索引を再構築する。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        name: 削除対象メモリーの名前。
        owner: 所有者。None なら共通のメモリー。

    Returns:
        削除したファイルの絶対パス（削除後はもう存在しない）。

    Raises:
        ValueError: owner に name のメモリーが存在しない場合。
    """
    mem = _find_memory(memory_root, name, owner)
    if mem is None:
        raise ValueError(f"Memory not found: {name}")
    mem.path.unlink()
    rebuild_index(memory_root, owner)
    logger.info("delete_memory: name=%s owner=%s", name, owner)
    return mem.path


def read_memory(memory_root: Path, name: str, owner: str | None = None) -> Memory:
    """owner のフォルダ（None なら共通）のメモリー1件を全文（本文込み）で読み込む（公開範囲は見ない）。

    Raises:
        ValueError: name のメモリーが存在しない場合。
    """
    mem = _find_memory(memory_root, name, owner)
    if mem is None:
        raise ValueError(f"Memory not found: {name}")
    return mem


def read_visible_memory(memory_root: Path, name: str, viewer: str | None, owner: str | None = None, *, auto: bool = True) -> Memory:
    """viewer が読めるメモリーを name で探す。

    auto=True なら 自分 → 共通 → 他のユーザー（見えるものだけ）の順に探し、最初に見つかったものを返す
    （同じ名前なら自分のものが優先される）。他のユーザーに同じ名前が複数あれば、owner を指定させる。
    auto=False なら owner（None は共通）のフォルダだけを見る。

    Raises:
        ValueError: 見つからない（他のユーザーの個人用メモリーも「見つからない」として扱い、在ることを明かさない）、
            または他のユーザーに同じ名前が複数ある場合。
    """
    _validate_name(name)
    policy = policy_for(memory_root)
    if not auto:
        mem = _find_memory(memory_root, name, owner)
        if mem is None or not is_visible_to(mem, viewer, policy):
            raise ValueError(f"Memory not found: {name}")
        return mem
    for scope in ([viewer] if viewer is not None else []) + [None]:
        mem = _find_memory(memory_root, name, scope)
        if mem is not None:
            return mem
    others = [
        m
        for o in list_owners(memory_root)
        if o != viewer and (m := _find_memory(memory_root, name, o)) is not None and is_visible_to(m, viewer, policy)
    ]
    if not others:
        raise ValueError(f"Memory not found: {name}")
    if len(others) > 1:
        raise ValueError(
            f"Several users share a memory named {name} ({', '.join(m.owner or '' for m in others)}): pass owner to choose one"
        )
    return others[0]


def search_memories(
    memory_root: Path, query: str, memory_type: str | None = None, *, viewer: str | None = None
) -> list[Memory]:
    """name/description/content に対する大文字小文字を区別しない部分一致検索（viewer が見えるものだけ）。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        query: 検索キーワード。
        memory_type: 指定すればそのtypeのみを検索対象にする。
        viewer: 会話のユーザー。自分のもの・共通のもの・他のユーザーの共有のものが対象になる。

    Returns:
        ヒットした Memory のリスト（自分 → 共通 → 他のユーザー、その中は memory_type→name の順）。

    Raises:
        ValueError: query が空、または memory_type が不正な値の場合。
    """
    if not query.strip():
        raise ValueError("query is empty")
    needle = query.lower()
    hits = [
        mem
        for mem in list_memories(memory_root, memory_type, viewer=viewer)
        if needle in f"{mem.name}\n{mem.description}\n{mem.content}".lower()
    ]
    return hits


def list_memories(memory_root: Path, memory_type: str | None = None, *, viewer: str | None = None) -> list[Memory]:
    """viewer が見えるメモリー（または指定typeのみ）を列挙する。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        memory_type: 指定すればそのtypeのみを列挙する。
        viewer: 会話のユーザー（None ならどのユーザーでもない立場 = 共通と他のユーザーの共有のもの）。

    Returns:
        Memory のリスト（所有者のランクが高い順。同じランクなら 自分 → 共通 → 他のユーザー、
        その中は memory_type→name の順）。

    Raises:
        ValueError: memory_type が不正な値の場合。
    """
    policy = policy_for(memory_root)
    visible = [m for m in all_memories(memory_root, memory_type) if is_visible_to(m, viewer, policy)]
    return _sorted_for(visible, viewer, policy)


def label_for(mem: Memory, viewer: str | None, policy: MemoryPolicy = DEFAULT_POLICY) -> str:
    """一覧・検索結果で name の後ろに付ける持ち主の表示（例: " (mine, private)"・" (common)"・" (user: alice)"）。

    ランクを設定していれば、所有者のランクも付ける（例: " (user: alice, rank 3)"）。
    """
    if mem.owner is None:
        parts = ["common"]
    elif mem.owner == viewer:
        parts = ["mine", mem.visibility]
    else:
        parts = [f"user: {mem.owner}"]
    if policy.uses_ranks:
        parts.append(f"rank {policy.rank_of(mem.owner)}")
    return f" ({', '.join(parts)})"


def render_memory_block(memory_root: Path, viewer: str | None = None, max_lines: int = _INDEX_MAX_LINES) -> str:
    """system_prompt.md の {{memory}} プレースホルダーへ差し込むテキストを、viewer 向けに組み立てる。

    viewer が見えるメモリー（所有者のランクが高い順。同じランクなら 自分 → 共通 → 他のユーザーの共有のもの）を
    1行ずつ並べ、max_lines を超える場合は切り詰めて注記を付す（先に並ぶものが残る）。用語（glossary）は
    ほかの種類の後ろに別枠で並べ、_INDEX_GLOSSARY_MAX_LINES 件まで（ほかの種類が少なければ空いた分まで）にする。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        viewer: 会話のユーザー。
        max_lines: 差し込む最大行数。

    Returns:
        差し込み用テキスト。見えるメモリーが無ければ「(no memories saved)」を返す。
    """
    memories = list_memories(memory_root, viewer=viewer) if memory_root.is_dir() else []
    if not memories:
        return "(no memories saved)"
    policy = policy_for(memory_root)

    def line(m: Memory) -> str:
        return f"- [{m.memory_type}] {m.name}{label_for(m, viewer, policy)}: {m.description}"[:_INDEX_ENTRY_MAX_CHARS]

    # 用語（glossary）は辞書ファイル1つで数百件になるため別枠にする（同じ枠だと、ほかの種類と共通のメモリーが
    # 用語に押し出されて索引から消える）。用語は _INDEX_GLOSSARY_MAX_LINES 件まで（ほかが少なければ空いた分まで）。
    terms = [m for m in memories if m.memory_type == "glossary"]
    rest = [m for m in memories if m.memory_type != "glossary"]
    term_budget = min(len(terms), _INDEX_GLOSSARY_MAX_LINES, max_lines)
    rest_budget = max_lines - term_budget
    if len(rest) < rest_budget:
        term_budget = min(len(terms), max_lines - len(rest))
    lines = [line(m) for m in rest[:rest_budget]]
    if len(rest) > rest_budget:
        lines += ["", f"... (truncated after {rest_budget} lines; check details with list_memories / search_memory)"]
    if terms:
        lines += [line(m) for m in terms[:term_budget]]
        if len(terms) > term_budget:
            lines += [
                "",
                f"... ({len(terms) - term_budget} more glossary terms not shown; find a term with "
                "search_memory(query, memory_type=\"glossary\"))",
            ]
    return "\n".join(lines)
