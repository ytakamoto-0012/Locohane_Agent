/**
 * 3D ビューアの「元に戻す」「やり直す」の履歴を、自動保存・保存するビューア用データ（viewState.history）に入れる形へ
 * 直す・そこから戻す。開き直したビューアでも、前に開いていたときの操作まで戻れるようにするため。
 *
 * 保存したデータは色・属性の変更を焼き込んである（src/cad/mesh_data.py の apply_view_edits）ので、履歴の各時点の
 * 「元のデータに対する上書き」は、そのままでは新しいデータに対する上書きにならない。そこで、どこかの時点で変えた
 * 面・部品の、元のデータでの色（null = 色なし）と属性を base に残し、開き直したときに各時点を「新しいデータに対する
 * 上書き」（変えていない面・部品も元の色を明示する）に直す。選択は保存しない（開き直すと選択は空なので）。
 */
import type { CadAttribute, CadColorOverrides, CadData, CadSavedEdit, CadSavedHistory, CadSelection } from './cadData';

/** 元に戻す・やり直すの対象（選択・変えた色・隠した部品・要素・種類ごとの表示・属性の編集）。 */
export interface EditSnapshot {
  selection: CadSelection[];
  colors: CadColorOverrides;
  hiddenParts: Set<number>;
  /** 「非表示」で 1 つずつ隠した要素（selectionKey）。ビューアを開いている間だけのもので、保存する履歴には入れない。 */
  hiddenItems: Set<string>;
  kindVisible: { face: boolean; edge: boolean; vertex: boolean };
  attributeEdits: Record<string, CadAttribute[]>;
}

// 保存する履歴の大きさの上限（JSON の文字数）。全選択して色を変える操作が続くと大きくなるので、超えたら古い方から捨てる。
const SAVED_HISTORY_MAX_CHARS = 2_000_000;

function toSaved(s: EditSnapshot): CadSavedEdit {
  const map = (m: ReadonlyMap<number, string | null>) => Object.fromEntries([...m].map(([k, v]) => [String(k), v]));
  return {
    colors: { faces: map(s.colors.faces), parts: map(s.colors.parts) },
    hiddenParts: [...s.hiddenParts].sort((a, b) => a - b),
    visible: { ...s.kindVisible },
    attributes: s.attributeEdits
  };
}

/** 選択を除いて同じ時点が続くものをまとめる（選択だけ変えた操作は保存しても意味が無い）。 */
function dedupe(items: CadSavedEdit[]): CadSavedEdit[] {
  const out: CadSavedEdit[] = [];
  let last = '';
  for (const item of items) {
    const key = JSON.stringify(item);
    if (key !== last) out.push(item);
    last = key;
  }
  return out;
}

/**
 * 保存する履歴。past は古い順、future は次にやり直す順の逆（useEditHistory の積み方と同じ、最後が次）。
 * 今の時点（current）と同じになる時点は前後から除く。戻れる時点が無ければ null。
 */
export function serializeHistory(data: CadData, past: EditSnapshot[], future: EditSnapshot[], current: EditSnapshot): CadSavedHistory | null {
  const cur = JSON.stringify(toSaved(current));
  let savedPast = dedupe(past.map(toSaved));
  while (savedPast.length && JSON.stringify(savedPast[savedPast.length - 1]) === cur) savedPast.pop();
  let savedFuture = dedupe(future.map(toSaved));
  while (savedFuture.length && JSON.stringify(savedFuture[savedFuture.length - 1]) === cur) savedFuture.pop();
  if (!savedPast.length && !savedFuture.length) return null;

  const all = [...savedPast, ...savedFuture, toSaved(current)];
  const partIds = new Set<number>();
  const faceIds = new Set<number>();
  const products = new Set<string>();
  for (const s of all) {
    Object.keys(s.colors.parts).forEach((k) => partIds.add(Number(k)));
    Object.keys(s.colors.faces).forEach((k) => faceIds.add(Number(k)));
    Object.keys(s.attributes).forEach((k) => products.add(k));
  }
  // 部品の色を変えるとその部品の面の色も変わるので、その面の元の色も残す。
  if (partIds.size) data.faces.forEach((f, id) => typeof f.part === 'number' && partIds.has(f.part) && faceIds.add(id));
  const base: CadSavedHistory['base'] = { faces: {}, parts: {}, attributes: {} };
  faceIds.forEach((id) => (base.faces[String(id)] = data.faces[id]?.color ?? null));
  partIds.forEach((id) => (base.parts[String(id)] = data.parts?.[id]?.color ?? null));
  for (const name of products) {
    const prod = data.step?.products?.find((p) => p.name === name);
    base.attributes[name] = prod?.attributes ?? [];
  }
  const history: CadSavedHistory = { past: savedPast, future: savedFuture, base };
  // 大きすぎれば、戻れる時点の古い方（past の先頭・future の先頭 = 最後にやり直す方）から捨てる。
  while (JSON.stringify(history).length > SAVED_HISTORY_MAX_CHARS && (history.past.length || history.future.length)) {
    if (history.past.length >= history.future.length) history.past.shift();
    else history.future.shift();
  }
  return history.past.length || history.future.length ? history : null;
}

/** 保存した 1 時点を、開き直したデータ（data）に対する上書きの時点に直す。 */
function fromSaved(data: CadData, saved: CadSavedEdit, base: CadSavedHistory['base']): EditSnapshot {
  const faces = new Map<number, string | null>();
  const parts = new Map<number, string | null>();
  const nFaces = data.faces.length;
  const nParts = data.parts?.length ?? 0;
  for (const [k, v] of Object.entries(base.parts)) {
    const id = Number(k);
    if (id >= 0 && id < nParts) parts.set(id, k in saved.colors.parts ? saved.colors.parts[k] : v);
  }
  for (const [k, v] of Object.entries(base.faces)) {
    const id = Number(k);
    if (!(id >= 0 && id < nFaces)) continue;
    if (k in saved.colors.faces) faces.set(id, saved.colors.faces[k]);
    else {
      // その時点で部品の色を変えていれば、面は部品の色に従う（上書きしない）。そうでなければ元の面の色。
      const part = data.faces[id]?.part;
      if (!(typeof part === 'number' && String(part) in saved.colors.parts)) faces.set(id, v);
    }
  }
  const attributeEdits: Record<string, CadAttribute[]> = {};
  for (const [name, attrs] of Object.entries(base.attributes)) attributeEdits[name] = saved.attributes[name] ?? attrs;
  return {
    selection: [],
    colors: { faces, parts },
    hiddenParts: new Set(saved.hiddenParts.filter((p) => p >= 0 && p < nParts)),
    hiddenItems: new Set(),
    kindVisible: { ...saved.visible },
    attributeEdits
  };
}

/** 保存した履歴（data.viewState.history）を、開き直したビューアの past / future に戻す。無ければ空。 */
export function restoreHistory(data: CadData): { past: EditSnapshot[]; future: EditSnapshot[] } {
  const h = data.viewState?.history;
  if (!h || !Array.isArray(h.past) || !Array.isArray(h.future) || !h.base) return { past: [], future: [] };
  const base = { faces: h.base.faces ?? {}, parts: h.base.parts ?? {}, attributes: h.base.attributes ?? {} };
  return { past: h.past.map((s) => fromSaved(data, s, base)), future: h.future.map((s) => fromSaved(data, s, base)) };
}
