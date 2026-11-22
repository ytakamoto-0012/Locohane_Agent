"""STEP（ISO 10303-21、P21）のテキストから、ヘッダーと製品の属性を読み書きする。標準ライブラリだけで動く。

OCCT（XCAF）は形・名前・色・レイヤー・GD&T は読み書きできるが、ユーザー定義属性（品番・材質等）は Python から
一覧できず、書き出しもしない。そこで属性はここで P21 を直接読み、保存時は書き出した STEP に足す。

読むもの:

- ヘッダー: FILE_DESCRIPTION・FILE_NAME（作成者・組織・作成システム等）・FILE_SCHEMA（AP203 / AP214 / AP242 と版）。
- 製品（PRODUCT）ごとの: 品番（id）・名前・説明、PRODUCT_DEFINITION に付いた PROPERTY_DEFINITION の属性
  （CAx-IF の User Defined Attributes の書き方。DESCRIPTIVE_REPRESENTATION_ITEM は文字列、
  VALUE_REPRESENTATION_ITEM / MEASURE_REPRESENTATION_ITEM は数値）。材料（'material property'）・
  検証プロパティ（'geometric validation property' の体積・面積・重心）もここに入る。

文字列は ISO 10303-21 の符号（\\X2\\…\\X0\\ の UTF-16、\\X4\\…\\X0\\ の UTF-32、\\X\\hh の ISO 8859-1、\\S\\c、''）を解く
（日本語の名前・属性は \\X2\\ で書かれる）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# FILE_SCHEMA の名前 → アプリケーションプロトコル。
_SCHEMAS = (
    ("AP242_MANAGED_MODEL_BASED_3D_ENGINEERING", "AP242"),
    ("AP203_CONFIGURATION_CONTROLLED_3D_DESIGN", "AP203e2"),
    ("CONFIG_CONTROL_DESIGN", "AP203"),
    ("AUTOMOTIVE_DESIGN", "AP214"),
    ("AP209", "AP209"),
    ("AP210", "AP210"),
    ("STRUCTURAL_FRAME_SCHEMA", "CIS/2"),
    ("IFC", "IFC"),
)
# AP242 の版（FILE_SCHEMA の { 1 0 10303 442 <ed> ... } の数字）。
# 2 は第 1 版の改訂（DIS 等）で使われたことがある。第 4 版（2025）の番号はまだ確かめていないので、知らない番号は番号のまま出す。
_AP242_EDITIONS = {"1": "AP242 ed1 (2014)", "2": "AP242 ed1", "3": "AP242 ed2 (2020)", "4": "AP242 ed3 (2022)"}
# 属性として読む PROPERTY_DEFINITION の名前のうち、意味が決まっているもの。
_VALIDATION_NAMES = ("geometric validation property",)
_MATERIAL_NAMES = ("material property",)
# 1 つの製品から読む属性の上限（巨大なファイルで LLM へ返すものを抑える）。
MAX_ATTRIBUTES_PER_PRODUCT = 200


# ---------------------------------------------------------------------------
# 文字列の符号
# ---------------------------------------------------------------------------


def decode_string(raw: str) -> str:
    """P21 の文字列（外側の ' は除いたもの）を Python の文字列にする。"""
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "'" and raw.startswith("''", i):
            out.append("'")
            i += 2
            continue
        if c == "\\" and i + 1 < n:
            if raw.startswith("\\X2\\", i) or raw.startswith("\\X4\\", i):
                width = 4 if raw[i + 2] == "2" else 8
                end = raw.find("\\X0\\", i + 4)
                if end < 0:
                    end = n
                hexs = raw[i + 4 : end]
                for k in range(0, len(hexs) - width + 1, width):
                    try:
                        out.append(chr(int(hexs[k : k + width], 16)))
                    except ValueError:
                        pass
                # UTF-16 のサロゲートの組を 1 文字にする。
                i = end + 4
                continue
            if raw.startswith("\\X\\", i) and i + 5 <= n:
                try:
                    out.append(chr(int(raw[i + 3 : i + 5], 16)))
                    i += 5
                    continue
                except ValueError:
                    pass
            if raw.startswith("\\S\\", i) and i + 3 < n:
                out.append(chr(ord(raw[i + 3]) + 128))
                i += 4
                continue
            if raw.startswith("\\\\", i):
                out.append("\\")
                i += 2
                continue
            if raw.startswith("\\P", i) and i + 3 < n and raw[i + 3] == "\\":
                i += 4  # コードページの切り替え（\PA\ 等）は読み飛ばす
                continue
        out.append(c)
        i += 1
    text = "".join(out)
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return text


def repair_mojibake(text: str) -> str:
    """UTF-8 のバイト列を 1 バイト 1 文字として読んでしまった文字列（二重の UTF-8）を元に戻す。

    CadQuery（cq.Assembly.export）は日本語の名前をこの形で STEP に書く。全部の文字が U+00FF 以下で、
    ISO 8859-1 のバイト列に戻すと正しい UTF-8 になり、しかも ASCII 以外を含むときだけ直す（普通の文字列は変えない）。
    """
    if not text or text.isascii() or any(ord(c) > 0xFF for c in text):
        return text
    try:
        fixed = text.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return text
    return fixed if fixed != text else text


def escape_non_ascii(text: str) -> str:
    """STEP の本文の文字列の中の ASCII 以外の文字を \\X2\\…\\X0\\ にする（規格どおりの ASCII だけの本文にする）。

    OCCT は ASCII 以外を UTF-8 のまま書くが、規格（ISO 10303-21）の上では \\X2\\ で書くもので、受け取る CAD に
    よっては文字化けする。文字列の外（コメントは書かない）には ASCII 以外は無い前提。
    """
    if text.isascii():
        return text

    def fix(m: re.Match) -> str:
        body = m.group(0)[1:-1]
        return encode_string(decode_string(body)) if not body.isascii() else m.group(0)

    return re.sub(r"'(?:[^']|'')*'", fix, text)


def encode_string(text: str) -> str:
    """Python の文字列を P21 の文字列（外側の ' を含む）にする。ASCII 以外は \\X2\\ / \\X4\\ で書く。"""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        wide = any(ord(ch) > 0xFFFF for ch in buf)
        if wide:
            out.append("\\X4\\" + "".join(f"{ord(ch):08X}" for ch in buf) + "\\X0\\")
        else:
            out.append("\\X2\\" + "".join(f"{ord(ch):04X}" for ch in buf) + "\\X0\\")
        buf.clear()

    for ch in text:
        if 32 <= ord(ch) < 127:
            flush()
            out.append("''" if ch == "'" else "\\\\" if ch == "\\" else ch)
        else:
            buf.append(ch)
    flush()
    return "'" + "".join(out) + "'"


# ---------------------------------------------------------------------------
# 構文
# ---------------------------------------------------------------------------


@dataclass
class Ref:
    """実体の参照（#123）。"""

    id: int


@dataclass
class Typed:
    """型付きの値（LENGTH_MEASURE(1.5) 等）。"""

    type: str
    value: Any


@dataclass
class Enum:
    """列挙値（.T. / .UNSPECIFIED. 等）。"""

    name: str


_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<ref>#\d+)|(?P<enum>\.[A-Z_0-9]+\.)|(?P<num>[+-]?(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?))"
    r"|(?P<kw>[A-Z_][A-Z_0-9]*)|(?P<sym>[(),$*]))",
    re.S,
)


def parse_params(text: str) -> list[Any]:
    """実体の引数の並び（外側の括弧の中）を Python の値の list にする。"""
    pos = 0
    tokens: list[tuple[str, str]] = []
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            pos += 1
            continue
        pos = m.end()
        kind = m.lastgroup
        tokens.append((kind, m.group(kind)))
    it = iter(tokens + [("sym", ")")])

    def value(tok: tuple[str, str]) -> Any:
        kind, s = tok
        if kind == "str":
            return repair_mojibake(decode_string(s[1:-1]))
        if kind == "ref":
            return Ref(int(s[1:]))
        if kind == "enum":
            return Enum(s[1:-1])
        if kind == "num":
            return float(s) if any(c in s for c in ".eE") else int(s)
        if kind == "kw":
            nxt = next(it)
            if nxt == ("sym", "("):
                inner = seq()
                return Typed(s, inner[0] if len(inner) == 1 else inner)
            return Enum(s)
        if s == "(":
            return seq()
        if s in ("$", "*"):
            return None
        return None

    def seq() -> list[Any]:
        items: list[Any] = []
        for tok in it:
            if tok == ("sym", ")"):
                return items
            if tok == ("sym", ","):
                continue
            items.append(value(tok))
        return items

    return seq()


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _sections(text: str) -> tuple[str, str]:
    text = _strip_comments(text)
    h0 = text.find("HEADER;")
    d0 = text.find("DATA;")
    header = text[h0 : d0 if d0 > 0 else len(text)] if h0 >= 0 else ""
    data = text[d0 + 5 :] if d0 >= 0 else ""
    end = data.find("ENDSEC;")
    return header, data[:end] if end >= 0 else data


def _entities(data: str) -> dict[int, tuple[str, str]]:
    """DATA 節の実体（#id → (型, 引数の文字列)）。複合の実体（#1=(A()B());）は型を "" にする。

    文字列の中の ';' で切れないよう、文字列を読み飛ばしながら 1 つずつ切り出す。
    """
    out: dict[int, tuple[str, str]] = {}
    i, n = 0, len(data)
    while i < n:
        j = data.find("#", i)
        if j < 0:
            break
        # 文の終わり（文字列の外の ';'）を探す。
        k, in_str = j, False
        while k < n:
            ch = data[k]
            if ch == "'":
                in_str = not in_str
            elif ch == ";" and not in_str:
                break
            k += 1
        stmt = data[j : k + 1]
        i = k + 1
        m = re.match(r"#(\d+)\s*=\s*([A-Z_][A-Z_0-9]*)?\s*\((.*)\)\s*;\s*$", stmt, re.S)
        if m:
            out[int(m.group(1))] = (m.group(2) or "", m.group(3))
    return out


# ---------------------------------------------------------------------------
# ヘッダー
# ---------------------------------------------------------------------------


def schema_info(schemas: list[str]) -> dict[str, Any]:
    """FILE_SCHEMA の名前から AP と版を求める。"""
    out: dict[str, Any] = {"schema": schemas}
    for s in schemas:
        upper = s.upper()
        for key, ap in _SCHEMAS:
            if key in upper:
                out["ap"] = ap
                if ap == "AP242":
                    nums = re.findall(r"\d+", s.split("{", 1)[1]) if "{" in s else []
                    # { 1 0 10303 442 <edition> <...> }
                    if len(nums) >= 5 and nums[3] == "442":
                        out["edition"] = _AP242_EDITIONS.get(nums[4], f"AP242 (schema version {nums[4]})")
                return out
    return out


def read_header(text: str) -> dict[str, Any]:
    header, _data = _sections(text)
    out: dict[str, Any] = {}
    for name in ("FILE_DESCRIPTION", "FILE_NAME", "FILE_SCHEMA"):
        m = re.search(name + r"\s*\((.*?)\)\s*;", header, re.S)
        if not m:
            continue
        params = parse_params(m.group(1))
        if name == "FILE_DESCRIPTION":
            out["description"] = [d for d in (params[0] if params and isinstance(params[0], list) else []) if d]
            if len(params) > 1 and isinstance(params[1], str):
                out["implementation_level"] = params[1]
        elif name == "FILE_NAME":
            keys = ("name", "time_stamp", "author", "organization", "preprocessor_version", "originating_system", "authorization")
            for key, val in zip(keys, params):
                if isinstance(val, list):
                    val = [v for v in val if v]
                if val not in (None, "", []):
                    out[key] = val
        else:
            out |= schema_info([s for s in (params[0] if params and isinstance(params[0], list) else []) if isinstance(s, str)])
    return out


# ---------------------------------------------------------------------------
# 製品と属性
# ---------------------------------------------------------------------------


def _measure(v: Any) -> tuple[Any, str | None]:
    if isinstance(v, Typed):
        inner = v.value
        return (inner if isinstance(inner, (int, float, str)) else str(inner)), v.type.lower()
    return v, None


def _rep_items(ents: dict[int, tuple[str, str]], rep_id: int, cache: dict[int, list[Any]]) -> list[dict[str, Any]]:
    if rep_id not in ents:
        return []
    params = _params(ents, rep_id, cache)
    items = params[1] if len(params) > 1 and isinstance(params[1], list) else []
    out = []
    for ref in items:
        if not isinstance(ref, Ref) or ref.id not in ents:
            continue
        etype = ents[ref.id][0]
        p = _params(ents, ref.id, cache)
        name = p[0] if p and isinstance(p[0], str) else ""
        if etype == "DESCRIPTIVE_REPRESENTATION_ITEM":
            out.append({"name": name, "value": p[1] if len(p) > 1 else None})
        elif etype in ("VALUE_REPRESENTATION_ITEM", "MEASURE_REPRESENTATION_ITEM"):
            value, mtype = _measure(p[1] if len(p) > 1 else None)
            item: dict[str, Any] = {"name": name, "value": value}
            if mtype:
                item["type"] = mtype
            out.append(item)
        elif etype == "CARTESIAN_POINT":
            coords = p[1] if len(p) > 1 else None
            out.append({"name": name, "value": coords})
        elif etype == "" and "MEASURE_REPRESENTATION_ITEM" in ents[ref.id][1]:
            # 複合の実体（(LENGTH_MEASURE_WITH_UNIT() MEASURE_REPRESENTATION_ITEM() ...)）。値と名前だけ拾う。
            m = re.search(r"MEASURE_WITH_UNIT\s*\(\s*([A-Z_]+)\s*\(\s*([^)]*)\)", ents[ref.id][1])
            n = re.search(r"REPRESENTATION_ITEM\s*\(\s*'((?:[^']|'')*)'", ents[ref.id][1])
            if m:
                try:
                    val: Any = float(m.group(2))
                except ValueError:
                    val = m.group(2)
                out.append({"name": decode_string(n.group(1)) if n else "", "value": val, "type": m.group(1).lower()})
    return out


def _params(ents: dict[int, tuple[str, str]], eid: int, cache: dict[int, list[Any]]) -> list[Any]:
    if eid not in cache:
        cache[eid] = parse_params(ents[eid][1]) if eid in ents else []
    return cache[eid]


def read_products(text: str) -> list[dict[str, Any]]:
    """製品（PRODUCT）ごとの品番・名前・説明と、その PRODUCT_DEFINITION に付いた属性。"""
    _header, data = _sections(text)
    ents = _entities(data)
    cache: dict[int, list[Any]] = {}
    products: dict[int, dict[str, Any]] = {}
    for eid, (etype, _args) in ents.items():
        if etype == "PRODUCT":
            p = _params(ents, eid, cache)
            products[eid] = {
                "product_id": p[0] if len(p) > 0 else None,
                "name": p[1] if len(p) > 1 else None,
                "description": p[2] if len(p) > 2 else None,
                "attributes": [],
            }
    # PRODUCT_DEFINITION → PRODUCT（PRODUCT_DEFINITION_FORMATION を経由）。
    pdf_to_product: dict[int, int] = {}
    pd_to_product: dict[int, int] = {}
    for eid, (etype, _args) in ents.items():
        if etype.startswith("PRODUCT_DEFINITION_FORMATION"):
            p = _params(ents, eid, cache)
            if len(p) > 2 and isinstance(p[2], Ref) and p[2].id in products:
                pdf_to_product[eid] = p[2].id
    for eid, (etype, _args) in ents.items():
        if etype in ("PRODUCT_DEFINITION", "PRODUCT_DEFINITION_WITH_ASSOCIATED_DOCUMENTS"):
            p = _params(ents, eid, cache)
            if len(p) > 2 and isinstance(p[2], Ref) and p[2].id in pdf_to_product:
                pd_to_product[eid] = pdf_to_product[p[2].id]
    # PROPERTY_DEFINITION（PD を指すもの）→ PROPERTY_DEFINITION_REPRESENTATION → REPRESENTATION の項目。
    prop_reps: dict[int, list[int]] = {}
    for eid, (etype, _args) in ents.items():
        if etype == "PROPERTY_DEFINITION_REPRESENTATION":
            p = _params(ents, eid, cache)
            if len(p) > 1 and isinstance(p[0], Ref) and isinstance(p[1], Ref):
                prop_reps.setdefault(p[0].id, []).append(p[1].id)
    for eid, (etype, _args) in ents.items():
        if etype != "PROPERTY_DEFINITION":
            continue
        p = _params(ents, eid, cache)
        target = p[2] if len(p) > 2 and isinstance(p[2], Ref) else None
        if target is None or target.id not in pd_to_product:
            continue
        prod = products[pd_to_product[target.id]]
        pname = (p[0] or "") if isinstance(p[0], str) else ""
        items = [it for rep in prop_reps.get(eid, []) for it in _rep_items(ents, rep, cache)]
        lname = pname.lower()
        if lname in _VALIDATION_NAMES:
            prod.setdefault("validation", {}).update({it["name"] or "value": it["value"] for it in items})
        elif lname in _MATERIAL_NAMES:
            prod.setdefault("material", []).extend(items)
        else:
            for it in items:
                if len(prod["attributes"]) >= MAX_ATTRIBUTES_PER_PRODUCT:
                    prod["attributes_truncated"] = True
                    break
                # 1 つの PROPERTY_DEFINITION に 1 つの項目（CAx-IF の書き方）なら、属性名は項目の名前。
                row = {"name": it["name"] or pname, "value": it["value"]}
                if it.get("type"):
                    row["type"] = it["type"]
                if pname and pname != row["name"] and pname.lower() != "user defined attributes":
                    row["group"] = pname
                prod["attributes"].append(row)
    return list(products.values())


def read_text(path: str | Path) -> tuple[str, str]:
    """STEP の本文と文字コード。規格の上は ASCII だが、ASCII 以外を UTF-8 のまま書くもの（OCCT 等）があるので
    UTF-8 を先に試し、だめなら ISO 8859-1 で読む（どのバイト列も読める）。"""
    raw = Path(path).read_bytes()
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1"


def read_step_info(path: str | Path) -> dict[str, Any]:
    """STEP ファイルのヘッダーと製品の属性。読めなければ空の dict。"""
    try:
        text, _enc = read_text(path)
    except OSError:
        return {}
    return {"header": read_header(text), "products": read_products(text)}


# ---------------------------------------------------------------------------
# 書き出し（OCCT が書いた STEP に、属性とヘッダーの情報を足す）
# ---------------------------------------------------------------------------


_TYPE_NAME = re.compile(r"[A-Z][A-Z_]*")


def _p21_real(value: float) -> str:
    """ISO 10303-21 の REAL として正しい形にする（小数点が必須。指数は 1.E-05 のように E 前に小数点）。

    Python の repr は 1e-05 のように出すので、厳格な STEP 読み取りが拒否する。
    """
    num = repr(float(value))
    if "e" in num.lower():
        mant, exp = num.split("e") if "e" in num else num.split("E")
        if "." not in mant:
            mant += "."
        return f"{mant.upper()}E{exp.upper()}"
    return num if "." in num else num + "."


def _value_text(value: Any, vtype: str | None) -> tuple[str, str]:
    """属性の値 → (実体の型, 引数)。文字列は DESCRIPTIVE、数値は VALUE_REPRESENTATION_ITEM。"""
    if isinstance(value, bool):
        value = "true" if value else "false"
    if isinstance(value, (int, float)) and vtype not in (None, "", "string"):
        kind = vtype.upper() if vtype else ("COUNT_MEASURE" if isinstance(value, int) else "REAL")
        # 型名はそのまま本文に書くので、英字と _ だけのもの（LENGTH_MEASURE 等）に限る（ほかは型なしの数として書く）。
        if _TYPE_NAME.fullmatch(kind) and (kind in ("REAL", "INTEGER") or kind.endswith("_MEASURE")):
            if kind in ("INTEGER", "COUNT_MEASURE"):
                num = str(int(value))
            else:
                num = _p21_real(value)
            return "VALUE_REPRESENTATION_ITEM", f"{kind}({num})"
    if isinstance(value, (int, float)):
        if isinstance(value, int):
            return "VALUE_REPRESENTATION_ITEM", f"COUNT_MEASURE({int(value)})"
        return "VALUE_REPRESENTATION_ITEM", f"REAL({_p21_real(value)})"
    return "DESCRIPTIVE_REPRESENTATION_ITEM", encode_string("" if value is None else str(value))


def material_rows(material: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """read_products の材料（[{name, value}]）を add_attributes に渡す行にする（CAx-IF の材料の書き方）。

    文字列の項目は材料名（PROPERTY_DEFINITION('material property','material name',…)・
    DESCRIPTIVE_REPRESENTATION_ITEM(材料名, 説明)）、数値の項目は密度等（'material property', 項目名）。
    """
    rows = []
    for m in material:
        if isinstance(m.get("value"), (int, float)):
            rows.append({"name": m.get("name") or "density", "value": m["value"], "type": m.get("type") or "real",
                         "group": "material property", "description": m.get("name") or "density"})
        else:
            rows.append({"name": m.get("name") or "material", "value": m.get("value"), "group": "material property", "description": "material name"})
    return rows


def add_attributes(text: str, attributes_by_product: dict[str, list[dict[str, Any]]]) -> tuple[str, int]:
    """STEP の本文に、製品名ごとの属性を CAx-IF の User Defined Attributes の書き方で足す。

    Returns:
        (書き換えた本文, 足した属性の数)。製品名が見つからない属性は足さない。
    """
    if not any(attributes_by_product.values()):
        return text, 0
    _header, data = _sections(text)
    ents = _entities(data)
    cache: dict[int, list[Any]] = {}
    name_to_pds: dict[str, list[int]] = {}
    prod_name: dict[int, str] = {}
    for eid, (etype, _a) in ents.items():
        if etype == "PRODUCT":
            p = _params(ents, eid, cache)
            if len(p) > 1 and isinstance(p[1], str):
                prod_name[eid] = p[1]
    pdf_prod: dict[int, int] = {}
    for eid, (etype, _a) in ents.items():
        if etype.startswith("PRODUCT_DEFINITION_FORMATION"):
            p = _params(ents, eid, cache)
            if len(p) > 2 and isinstance(p[2], Ref) and p[2].id in prod_name:
                pdf_prod[eid] = p[2].id
    for eid, (etype, _a) in ents.items():
        if etype in ("PRODUCT_DEFINITION", "PRODUCT_DEFINITION_WITH_ASSOCIATED_DOCUMENTS"):
            p = _params(ents, eid, cache)
            if len(p) > 2 and isinstance(p[2], Ref) and p[2].id in pdf_prod:
                name_to_pds.setdefault(prod_name[pdf_prod[p[2].id]], []).append(eid)
    next_id = (max(ents) if ents else 0) + 1
    lines: list[str] = []
    ctx = None
    added = 0
    for name, attrs in attributes_by_product.items():
        for pd in name_to_pds.get(name, [])[:1]:
            for attr in attrs:
                key = str(attr.get("name") or "").strip()
                if not key:
                    continue
                if ctx is None:
                    ctx = next_id
                    lines.append(f"#{ctx}=REPRESENTATION_CONTEXT('','');")
                    next_id += 1
                etype, arg = _value_text(attr.get("value"), attr.get("type"))
                prop, rep, item, pdr = next_id, next_id + 1, next_id + 2, next_id + 3
                next_id += 4
                group = encode_string(str(attr.get("group") or "user defined attributes"))
                desc = encode_string(str(attr.get("description") or ""))
                lines += [
                    f"#{prop}=PROPERTY_DEFINITION({group},{desc},#{pd});",
                    f"#{item}={etype}({encode_string(key)},{arg});",
                    f"#{rep}=REPRESENTATION('',(#{item}),#{ctx});",
                    f"#{pdr}=PROPERTY_DEFINITION_REPRESENTATION(#{prop},#{rep});",
                ]
                if attr.get("group") != "material property":
                    added += 1
    if not lines:
        return text, 0
    # DATA 節の終わり（最後の ENDSEC;）の前に入れる。
    pos = text.rfind("ENDSEC;")
    if pos < 0:
        return text, 0
    return text[:pos] + "\n".join(lines) + "\n" + text[pos:], added


def set_header(text: str, original: dict[str, Any], preprocessor: str) -> str:
    """書き出した STEP の FILE_NAME の作成者・組織・作成システム・承認を元のファイルのものにする。"""
    m = re.search(r"FILE_NAME\s*\((.*?)\)\s*;", text, re.S)
    if not m or not original:
        return text
    params = parse_params(m.group(1))
    if len(params) < 7:
        return text

    def strs(v: Any) -> str:
        vals = [x for x in (v if isinstance(v, list) else [v]) if x not in (None, "")]
        return "(" + ",".join(encode_string(str(x)) for x in vals) + ")" if vals else "('')"

    name = params[0] if isinstance(params[0], str) else ""
    stamp = params[1] if isinstance(params[1], str) else ""
    author = original.get("author", params[2])
    org = original.get("organization", params[3])
    system = original.get("originating_system", params[5] or "")
    auth = original.get("authorization", params[6] or "")
    new = (
        f"FILE_NAME({encode_string(name)},{encode_string(stamp)},{strs(author)},{strs(org)},"
        f"{encode_string(preprocessor)},{encode_string(str(system))},{encode_string(str(auth))});"
    )
    return text[: m.start()] + new + text[m.end() :]
