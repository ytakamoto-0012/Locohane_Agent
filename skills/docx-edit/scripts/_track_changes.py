"""Track Changes（変更履歴）用のoxmlヘルパー。

python-docxにはTrack Changes（<w:ins>/<w:del>）用の高レベルAPIが無いため、
create_docx.py の _add_page_number_field と同じ「OxmlElement + qn() で
要素を直接組み立てる」イディオムで実装する。

run_script からは直接実行されない。_ops.py から import して使う。
docx-read/scripts/read_docx.py も count_revisions 用にこのファイルを
sys.path 経由で直接importする（複製ではなく実体を1箇所に集約する1-B方式）。
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

DEFAULT_AUTHOR = "AI Agent"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def count_revisions(doc) -> dict:
    """文書body内の w:ins / w:del の件数を返す（read_docx.py から利用）。"""
    from docx.oxml.ns import qn

    body = doc.element.body
    insertion_count = sum(1 for _ in body.iter(qn("w:ins")))
    deletion_count = sum(1 for _ in body.iter(qn("w:del")))
    return {
        "has_pending_revisions": bool(insertion_count or deletion_count),
        "insertion_count": insertion_count,
        "deletion_count": deletion_count,
    }


def _next_revision_id(doc) -> int:
    """文書内の既存 w:ins/w:del の w:id 最大値+1を返す。

    呼び出すたびに文書全体を再走査するが、1回の編集で発行するidは
    せいぜい数十件程度のため実用上問題にならない。直前に追加した
    要素を含めて正しく次のidを算出できるシンプルさを優先する。
    """
    from docx.oxml.ns import qn

    max_id = 0
    body = doc.element.body
    for tag in ("w:ins", "w:del"):
        for el in body.iter(qn(tag)):
            try:
                max_id = max(max_id, int(el.get(qn("w:id"), "0")))
            except ValueError:
                pass
    return max_id + 1


def _make_revision_wrapper(tag: str, revision_id: int, author: str, date: str):
    from docx.oxml.ns import qn
    from docx.oxml.shared import OxmlElement

    el = OxmlElement(tag)
    el.set(qn("w:id"), str(revision_id))
    el.set(qn("w:author"), author)
    el.set(qn("w:date"), date)
    return el


def _clone_run_shell(orig_r):
    """rPr（書式）だけをコピーした空の<w:r>を返す。"""
    from docx.oxml.ns import qn
    from docx.oxml.shared import OxmlElement

    new_r = OxmlElement("w:r")
    rpr = orig_r.find(qn("w:rPr"))
    if rpr is not None:
        new_r.append(copy.deepcopy(rpr))
    return new_r


def _append_text(run_el, text: str, *, as_delete: bool = False):
    from docx.oxml.ns import qn
    from docx.oxml.shared import OxmlElement

    t = OxmlElement("w:delText" if as_delete else "w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    run_el.append(t)
    return run_el


def run_text(r_element) -> str:
    from docx.oxml.ns import qn

    return "".join(t.text or "" for t in r_element.findall(qn("w:t")))


def is_plain_text_run(r_element) -> bool:
    """<w:t>とrPr以外の子（tab/br/field等）を持たないrunのみ対象とする。"""
    from docx.oxml.ns import qn

    allowed = {qn("w:rPr"), qn("w:t")}
    return all(child.tag in allowed for child in r_element)


def apply_tracked_replace_to_run(doc, r_element, old_text: str, new_text: str, occurrence: str, author: str, date: str) -> int:
    """r_element内のold_textをtrack changes付きでnew_textに置き換える。

    マッチ箇所を「前方テキストrun」+「w:delでラップした旧テキストrun」+
    「w:insでラップした新テキストrun（new_textが空でなければ）」+
    「後方テキストrun」に分割し、元runの位置に addprevious で差し込む。
    戻り値は置換件数。
    """
    text = run_text(r_element)
    if old_text not in text:
        return 0

    # w:idは呼び出し時点のdoc全体を1回だけ走査して開始値を決め、以降は
    # ローカルでインクリメントする（新規要素はこの時点でまだツリーに
    # 接続されていないため、要素を作るたびに再走査すると同じidが
    # 採番されてしまう）。
    next_id = _next_revision_id(doc)

    new_elements = []
    cursor = 0
    count = 0
    limit = 1 if occurrence == "first" else None

    while True:
        pos = text.find(old_text, cursor)
        if pos == -1 or (limit is not None and count >= limit):
            break
        if pos > cursor:
            r = _clone_run_shell(r_element)
            _append_text(r, text[cursor:pos])
            new_elements.append(r)

        del_wrap = _make_revision_wrapper("w:del", next_id, author, date)
        next_id += 1
        r = _clone_run_shell(r_element)
        _append_text(r, old_text, as_delete=True)
        del_wrap.append(r)
        new_elements.append(del_wrap)

        if new_text:
            ins_wrap = _make_revision_wrapper("w:ins", next_id, author, date)
            next_id += 1
            r = _clone_run_shell(r_element)
            _append_text(r, new_text)
            ins_wrap.append(r)
            new_elements.append(ins_wrap)

        cursor = pos + len(old_text)
        count += 1

    if count == 0:
        return 0

    if cursor < len(text):
        r = _clone_run_shell(r_element)
        _append_text(r, text[cursor:])
        new_elements.append(r)

    for el in new_elements:
        r_element.addprevious(el)
    r_element.getparent().remove(r_element)
    return count


def accept_all_changes(doc) -> dict:
    from docx.oxml.ns import qn

    body = doc.element.body
    accepted_ins = accepted_del = 0

    for ins in list(body.iter(qn("w:ins"))):
        for child in list(ins):
            ins.addprevious(child)
        ins.getparent().remove(ins)
        accepted_ins += 1

    for del_el in list(body.iter(qn("w:del"))):
        del_el.getparent().remove(del_el)
        accepted_del += 1

    return {"accepted_ins": accepted_ins, "accepted_del": accepted_del}


def reject_all_changes(doc) -> dict:
    from docx.oxml.ns import qn

    body = doc.element.body
    rejected_ins = rejected_del = 0

    for ins in list(body.iter(qn("w:ins"))):
        ins.getparent().remove(ins)
        rejected_ins += 1

    for del_el in list(body.iter(qn("w:del"))):
        for del_text in del_el.iter(qn("w:delText")):
            del_text.tag = qn("w:t")
        for child in list(del_el):
            del_el.addprevious(child)
        del_el.getparent().remove(del_el)
        rejected_del += 1

    return {"rejected_ins": rejected_ins, "rejected_del": rejected_del}
