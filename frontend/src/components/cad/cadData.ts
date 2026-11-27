/**
 * show_cad_viewer（src/tools/cad_viewer.py）が CustomElement "CadViewer" の props.data に入れて送る
 * ビューア用データの型と、base64 の配列の復号。形は src/cad/mesh_data.py の docstring と一致させる。
 */

/** hole は開いたサーフェース・メッシュの面の中の穴、outer はつながった面のまとまりの外周（src/cad/topology.py）。 */
export type CadKind = 'face' | 'edge' | 'vertex' | 'part' | 'hole' | 'outer';
/** 縁のループとして描く種類（穴・外周）。 */
export type CadLoopKind = 'hole' | 'outer';

export interface CadSelection {
  kind: CadKind;
  id: number;
}

/** 面・エッジ・頂点・部品の情報（LLM へ返す内容と同じ。ビューアはホバー時の表示に一部を使う）。 */
export interface CadItemInfo {
  id: number;
  kind: CadKind;
  geomType?: string;
  name?: string;
  path?: string;
  area?: number;
  length?: number;
  radius?: number;
  point?: number[];
  center?: number[];
  normal?: number[];
  color?: string | null;
  free?: boolean;
  part?: number;
  faces?: number[];
  edges?: number[];
  /** DXF のレイヤーの設定（部品 = レイヤーのときだけ）。 */
  layer?: CadLayerSettings;
  selectors?: string[];
  meshHints?: string[];
  [key: string]: unknown;
}

export interface CadLayerSettings {
  color: string | null;
  aci: number | null;
  linetype: string | null;
  off: boolean;
  frozen: boolean;
  locked: boolean;
}

/** 範囲選択のしかた。window = 枠の中に完全に入るもの、crossing = 枠の中に入るものと枠に掛かるもの。 */
export type BoxMode = 'off' | 'window' | 'crossing';

/** 表示の設定（種類ごとの表示と、隠す部品・要素）。 */
export interface CadVisibility {
  face: boolean;
  edge: boolean;
  vertex: boolean;
  hiddenParts: ReadonlySet<number>;
  /** 「非表示」で 1 つずつ隠した面・エッジ・頂点・穴・外周（selectionKey の集合）。省くと無し。 */
  hiddenItems?: ReadonlySet<string>;
  /** 非表示領域: true なら隠したものだけを映し、見えていたものを隠す（CAD の「表示/非表示空間の切り替え」）。 */
  swapped?: boolean;
  /** false なら表示するモデル（基準モデルと比べるときの ①）を丸ごと隠す（当たり判定・範囲選択の対象にもしない）。省くと表示。 */
  model?: boolean;
}

/**
 * 利用者がビューアで変えた色。キーは面の ID・部品の ID、値は #rrggbb か null（色なしに戻す。自動保存したデータを
 * 開き直して「元に戻す」で、色の無かった元の状態へ戻すときに使う）。
 */
export interface CadColorOverrides {
  faces: ReadonlyMap<number, string | null>;
  parts: ReadonlyMap<number, string | null>;
}

/** 選択を集合で引くときのキー（"種類:ID"）。 */
export function selectionKey(kind: CadKind, id: number): string {
  return `${kind}:${id}`;
}

/** 上書きの値。上書きが無ければ undefined（null は「色なし」の上書き）。 */
export function overrideOf(map: ReadonlyMap<number, string | null>, id: number | undefined): string | null | undefined {
  return id !== undefined && map.has(id) ? (map.get(id) ?? null) : undefined;
}

/** 要素の今の色（変えた色 → 部品の変えた色 → ファイルの色）。色が無ければ null（テーマの既定色で描く）。 */
export function effectiveColor(data: CadData, overrides: CadColorOverrides, s: CadSelection): string | null {
  if (s.kind === 'part') {
    const o = overrideOf(overrides.parts, s.id);
    return o !== undefined ? o : (data.parts?.[s.id]?.color ?? null);
  }
  if (s.kind !== 'face') return null;
  const info = data.faces[s.id];
  let o = overrideOf(overrides.faces, s.id);
  if (o === undefined) o = overrideOf(overrides.parts, info?.part);
  return o !== undefined ? o : (info?.color ?? null);
}

/**
 * 部品の色をファイルの色へ戻した上書きを外す。残すとビューアはその部品の面をすべてその色で描くが、
 * backend（cad_viewer._valid_colors）は元と同じ色の変更を捨てるので、面ごとに色の付いた部品では
 * 画面と保存したファイルの色が食い違う。外すと面は元の色に戻り、画面と保存が一致する。
 */
export function dropUnchangedPartColors(data: CadData, overrides: CadColorOverrides): CadColorOverrides {
  const unchanged = [...overrides.parts].filter(
    ([id, hex]) => (data.parts?.[id]?.color ?? '').toLowerCase() === (hex ?? '').toLowerCase()
  );
  if (!unchanged.length) return overrides;
  const parts = new Map(overrides.parts);
  for (const [id] of unchanged) parts.delete(id);
  return { faces: overrides.faces, parts };
}

/** 「同じ部品・レイヤー」でまとめて選ぶときの、要素が属する部品（DXF ではレイヤー）。無ければ null。 */
export function partOf(data: CadData, s: CadSelection): number | null {
  if (s.kind === 'part') return s.id;
  const part = itemsOf(data, s.kind)[s.id]?.part;
  return typeof part === 'number' ? part : null;
}

/**
 * 「同じ色」でまとめて選ぶときの要素の色（小文字の #rrggbb）。面・部品は今の色、エッジ・頂点は
 * 自身の色が無いので属する部品（レイヤー）の色。色が無ければ null。
 */
export function attributeColor(data: CadData, overrides: CadColorOverrides, s: CadSelection): string | null {
  let color: string | null;
  if (s.kind === 'face' || s.kind === 'part') color = effectiveColor(data, overrides, s);
  else {
    const part = partOf(data, s);
    color = part === null ? null : effectiveColor(data, overrides, { kind: 'part', id: part });
  }
  return color ? color.toLowerCase() : null;
}

export function hexToRgb(hex: string): [number, number, number] | null {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : null;
}

/** #rrggbb → 「RGB(r, g, b)」。 */
export function rgbText(hex: string | null): string {
  const rgb = hex ? hexToRgb(hex) : null;
  return rgb ? `RGB(${rgb.join(', ')})` : '';
}

export function rgbToHex(rgb: [number, number, number]): string {
  return '#' + rgb.map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('');
}

/** STEP の属性（品番・材質等。src/cad/step_p21.py）。 */
export interface CadAttribute {
  name: string;
  value: string | number | null;
  type?: string;
  group?: string;
}

/** STEP の製品（PRODUCT）。 */
export interface CadProduct {
  product_id?: string | null;
  name?: string | null;
  description?: string | null;
  attributes?: CadAttribute[];
  material?: CadAttribute[];
}

/** STEP の PMI（寸法・幾何公差・データム。src/cad/step_xcaf.py）。faces などはビューアの ID。 */
export interface CadPmiItem {
  id: string;
  kind: 'dimension' | 'tolerance' | 'datum';
  text: string;
  faces?: number[];
  faces_second?: number[];
  edges?: number[];
  edges_second?: number[];
  parts?: number[];
  [key: string]: unknown;
}

export interface CadStepInfo {
  header?: { ap?: string; edition?: string; originating_system?: string; schema?: string[]; [key: string]: unknown };
  products?: CadProduct[];
  pmi?: { dimensions?: CadPmiItem[]; tolerances?: CadPmiItem[]; datums?: CadPmiItem[] };
  layers?: { name: string; parts?: number[]; faces?: number[] }[];
  warnings?: string[];
}

/** 穴・ボス（src/cad/features.py）。 */
export interface CadFeature {
  id: string;
  type: 'hole' | 'boss';
  diameter: number;
  depth?: number;
  length?: number;
  through?: boolean;
  faces: number[];
}

/**
 * 数の配列。ビューア用データの JSON では base64 の文字列、ブラウザが受け取るパッケージ（parseCadPackage）では
 * 復号済みのバイト列（パッケージの ArrayBuffer をそのまま指す）。
 */
export type CadBuffer = string | Uint8Array;

/** 三角形の集まり（位置は float32、番号は uint32）。 */
export interface CadTriangles {
  positions: CadBuffer;
  indices: CadBuffer;
}

export interface CadData {
  format: string;
  source: { kind: string; format?: string; path?: string };
  units: string;
  bbox: { min: number[]; max: number[]; size: number[] };
  mesh: { positions: CadBuffer; indices: CadBuffer; colors: CadBuffer | null; faceRanges: [number, number][] };
  edges: { positions: CadBuffer; ranges: [number, number][] };
  vertices: { positions: CadBuffer };
  faces: CadItemInfo[];
  edgeList: CadItemInfo[];
  vertexList: CadItemInfo[];
  parts?: CadItemInfo[];
  stats: Record<string, number>;
  /** STEP のヘッダー・製品の属性・PMI・レイヤー（元が STEP のときだけ）。 */
  step?: CadStepInfo;
  /** 穴・ボスの一覧（B-rep のときだけ）。 */
  features?: CadFeature[];
  /** 開いた形の面の中の穴（holeList）の縁の線（線分ごとに 2 点。穴 i は ranges[i] の範囲）。 */
  holes?: {
    positions: CadBuffer;
    ranges: [number, number][];
    tolerance?: number;
    /** 縫合トレランス（0 なら縫合していない）と、縫い合わせた境界の辺の数。 */
    sewTolerance?: number;
    sewnEdges?: number;
    outerLoops?: number;
    openChains?: number;
  };
  holeList?: CadItemInfo[];
  /** つながった面のまとまりの外周（outerList）の線（holes と同じ形）。 */
  outers?: { positions: CadBuffer; ranges: [number, number][] };
  outerList?: CadItemInfo[];
  /** 面ごとの、つながった面のまとまりの番号（src/cad/topology.py。縫合・許容差での突き合わせを含む）。 */
  faceComponents?: number[];
  /** モデルの中の直交する 3 本の線で決まるローカル座標系（src/cad/topology.py の find_local_axes）。 */
  localAxes?: CadLocalAxis[];
  /** 前の版との比較（show_cad_viewer の compare_with）。added / removed は増えた・削った部分の三角形。 */
  diff?: { added?: CadTriangles; removed?: CadTriangles; error?: string; [key: string]: unknown };
  /**
   * ゼブラ・曲率の表示に使う頂点ごとの法線（int16 × 32767）と主曲率 k1 ≥ k2（float32。凸 = 正）。src/cad/analysis.py。
   * mesh の頂点と同じ並び。method は exact（B-rep の正確な値）・mesh（三角形から推定）・mixed。
   */
  analysis?: { normals: CadBuffer; k1: CadBuffer; k2: CadBuffer; method?: string; summary?: Record<string, unknown> };
  /** 基準モデル（show_cad_viewer の reference）の三角形（src/cad/reference.py）。 */
  reference?: CadTriangles & { source?: { format?: string; path?: string }; triangles?: number; chordTolerance?: number };
  /**
   * 基準モデルとの偏差。values は mesh の頂点ごとの符号付き距離（float32。正 = 基準の外側）、transform は
   * 対象 → 基準の 4×4（行ごと）。error があれば基準モデルを読めなかった。
   */
  deviation?: {
    values?: CadBuffer;
    transform?: number[][];
    alignment?: CadAlignmentInfo;
    tolerance?: number;
    stats?: CadDeviationStats;
    error?: string;
  };
  /** 「保存」で書いたデータにある、保存したときの表示（隠した部品・種類ごとの表示）。 */
  viewState?: {
    hiddenParts?: number[];
    visible?: Partial<Record<'face' | 'edge' | 'vertex', boolean>>;
    /** 自動保存・保存したときの「元に戻す」「やり直す」の履歴（開き直しても続きから戻せる。CadViewerBar.tsx）。 */
    history?: CadSavedHistory;
  };
}

/** 位置合わせの記録（src/cad/align.py の align の戻り値）。 */
export interface CadAlignmentInfo {
  method: string;
  rms?: number;
  inlier_ratio?: number;
  iterations?: number;
  points?: number;
  [key: string]: unknown;
}

/** 偏差の統計（src/cad/align.py の deviation_stats。面積で重み付け）。 */
export interface CadDeviationStats {
  min?: number;
  max?: number;
  mean?: number;
  std?: number;
  rms?: number;
  abs_p95?: number;
  tolerance?: number;
  within_tolerance_ratio?: number;
  above_ratio?: number;
  below_ratio?: number;
}

/** 保存した履歴の 1 時点（選択は含めない）。色・属性は history.base のデータに対する上書き。 */
export interface CadSavedEdit {
  colors: { faces: Record<string, string | null>; parts: Record<string, string | null> };
  hiddenParts: number[];
  visible: { face: boolean; edge: boolean; vertex: boolean };
  attributes: Record<string, CadAttribute[]>;
}

/**
 * 保存した履歴。base は履歴が基準にしたデータ（保存する前に開いていたデータ）の、どこかの時点で変えた面・部品の色
 * （null = 色なし）と属性。保存したデータは変更を焼き込んであるので、開き直したときはこれを使って各時点を
 * 新しいデータに対する上書きに直す。
 */
export interface CadSavedHistory {
  past: CadSavedEdit[];
  future: CadSavedEdit[];
  base: {
    faces: Record<string, string | null>;
    parts: Record<string, string | null>;
    attributes: Record<string, CadAttribute[]>;
  };
}

export interface CadViewerProps {
  question?: string;
  select?: CadKind[];
  /** ビューア用データの ID。本体は app.py の GET /locohane/cad/data/{data_id} で取りに行く（fetchCadPackage）。 */
  data_id?: string;
  /** 以前の形（props にビューア用データ全体を入れていた）。 */
  data?: CadData;
  /** 「保存」ボタンで app.py の action_callback "cad_viewer_save" に渡す ID。 */
  save_id?: string;
  /** 指示付きで確定したときに撮る画面の長辺（ピクセル、[cad_viewer].screenshot_long_side_pixels）。0 以下・無しなら撮らない。 */
  screenshot_long_side?: number;
  /** 穴にマウスを乗せたとき、同じ向きとしてまとめる法線の角度差の既定値（度、[cad_viewer].hole_normal_angle_deg）。 */
  hole_normal_angle_deg?: number;
  /** 穴をまとめるときの穴サイズ誤差の既定値（モデルの単位、[cad_viewer].hole_size_tolerance。0 以下なら初めは外す）。 */
  hole_size_tolerance?: number;
  /** 「縫合」の欄の初めの値（モデルの単位、[cad_viewer].sew_tolerance）。 */
  sew_tolerance?: number;
  /** backend が答えを待つ秒数（[cad_viewer].timeout_seconds。0 = 無期限）。残りが少なくなったら残り時間を出す。 */
  timeout_seconds?: number;
  /** 基準モデルとの位置合わせをビューアからやり直せるか（app.py の action_callback "cad_viewer_align"）。 */
  can_align?: boolean;
  /** 偏差の公差の既定値（±、[cad_viewer].deviation_tolerance）。 */
  deviation_tolerance?: number;
}

/** 「縫合」（app.py の action_callback "cad_viewer_sew"）が返す、求め直した穴・外周・面のまとまり。 */
export type CadTopology = Pick<CadData, 'holeList' | 'holes' | 'outerList' | 'outers' | 'faceComponents'> & {
  stats?: Record<string, number>;
};

/** data の穴・外周・面のまとまりを topology に置き換える（無い項目は消す）。data そのものを書き換える。 */
export function replaceTopology(data: CadData, topology: CadTopology): void {
  for (const key of ['holeList', 'holes', 'outerList', 'outers', 'faceComponents'] as const) {
    if (topology[key] === undefined) delete data[key];
    else (data as unknown as Record<string, unknown>)[key] = topology[key];
  }
  const stats = { ...data.stats };
  delete stats.holes;
  delete stats.outers;
  data.stats = { ...stats, ...(topology.stats ?? {}) };
  loopPointCache.delete(data);
}

/** ローカル座標系。origin と単位ベクトル x・y・z はワールド座標（右手系でなければ rightHanded が false）。 */
export interface CadLocalAxis {
  id: number;
  origin: number[];
  x: number[];
  y: number[];
  z: number[];
  edges?: number[];
  rightHanded?: boolean;
  orthogonalityErrorDeg?: number;
}

function dot3(a: ArrayLike<number>, b: ArrayLike<number>): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

/** ワールド座標の点をローカル座標にする（local = [x·(p−o), y·(p−o), z·(p−o)]）。axis が null ならそのまま。 */
export function toLocalPoint(axis: CadLocalAxis | null, p: ArrayLike<number>): number[] {
  if (!axis) return [p[0], p[1], p[2]];
  const d = [p[0] - axis.origin[0], p[1] - axis.origin[1], p[2] - axis.origin[2]];
  return [dot3(axis.x, d), dot3(axis.y, d), dot3(axis.z, d)];
}

/** ワールド座標の向き（法線・差）をローカル座標にする。axis が null ならそのまま。 */
export function toLocalVector(axis: CadLocalAxis | null, v: ArrayLike<number>): number[] {
  if (!axis) return [v[0], v[1], v[2]];
  return [dot3(axis.x, v), dot3(axis.y, v), dot3(axis.z, v)];
}

// 穴の縁の点（holes.positions の線分の始点。穴 i は ranges[i]）。データごとに 1 回だけ読む。
const loopPointCache = new WeakMap<CadData, Float32Array>();

function holeLoopPositions(data: CadData): Float32Array {
  let p = loopPointCache.get(data);
  if (!p) {
    p = decodeFloat32(data.holes?.positions);
    loopPointCache.set(data, p);
  }
  return p;
}

/**
 * 穴の縁を、基準の向き base（単位ベクトル）から見たときと、穴自身の法線の向きから見たときの、縁の線のずれの最大
 * （中心からの距離の差。傾いた穴ほど・大きい穴ほど見かけの縁が内側へずれる）。縁の点が無ければ null。
 */
export function holeOutlineDeviation(data: CadData, id: number, base: ArrayLike<number>): number | null {
  const hole = data.holeList?.[id];
  const range = data.holes?.ranges?.[id];
  const c = hole?.center;
  const n = hole?.normal;
  if (!range || !c || !n) return null;
  const pos = holeLoopPositions(data);
  const [start, count] = range;
  let worst = 0;
  for (let k = 0; k < count; k++) {
    // 線分ごとに 2 点並ぶ（src/cad/topology.py の _loop_items）。始点だけ見れば縁の点を 1 回ずつ見る。
    const o = (start + k) * 6;
    if (o + 2 >= pos.length) return null;
    const r = [pos[o] - c[0], pos[o + 1] - c[1], pos[o + 2] - c[2]];
    const rr = dot3(r, r);
    const along = dot3(r, n);
    const own = Math.sqrt(Math.max(0, rr - along * along));
    const across = dot3(r, base);
    const seen = Math.sqrt(Math.max(0, rr - across * across));
    worst = Math.max(worst, Math.abs(own - seen));
  }
  return count ? worst : null;
}

/**
 * 穴 id と同じ向き（法線のなす角が deg 度以内）の穴の ID（id 自身を含む、昇順）。
 * 法線は穴の縁の面の表側を向く（src/cad/topology.py）が、縫い合わせていない面は面ごとに表裏がそろっていないことが
 * あるので、向きは表裏を問わずに比べる（見た目は同じ向きの穴が、表裏が逆なだけで外れないように）。
 * sizeTol（モデルの単位。null で使わない）を渡すと、穴 id の法線の向きから見た縁が、その穴自身の法線の向きから
 * 見た縁と比べて ±sizeTol に収まる穴だけにする（holeOutlineDeviation）。
 */
export function holeGroup(data: CadData, id: number, deg: number, sizeTol: number | null = null): number[] {
  const holes = data.holeList ?? [];
  const base = holes[id]?.normal;
  if (!base) return holes[id] ? [id] : [];
  const cos = Math.cos((Math.max(0, Math.min(90, deg)) * Math.PI) / 180);
  const out: number[] = [];
  holes.forEach((h, i) => {
    if (i === id) {
      out.push(i);
      return;
    }
    const n = h.normal;
    // 丸めた単位ベクトルなので、内積を 1 で切ってから比べる（0° のとき同じ向きの穴が外れないように少し甘くする）。
    if (!n || Math.min(1, Math.abs(dot3(base, n))) < cos - 1e-9) return;
    if (sizeTol !== null && sizeTol >= 0) {
      const dev = holeOutlineDeviation(data, i, base);
      if (dev === null || dev > sizeTol + 1e-9) return;
    }
    out.push(i);
  });
  return out;
}

/**
 * 面 ids とつながった面（同じ面のまとまり。faceComponents）すべての ID（昇順）。まとまりが無いデータ（古い
 * ビューア用データ）では、エッジを共有する面をたどる。
 */
export function connectedFaces(data: CadData, ids: Iterable<number>): number[] {
  const comps = data.faceComponents;
  const start = [...ids].filter((i) => i >= 0 && i < data.faces.length);
  if (comps && comps.length === data.faces.length) {
    const want = new Set(start.map((i) => comps[i]));
    const out: number[] = [];
    comps.forEach((c, i) => {
      if (want.has(c)) out.push(i);
    });
    return out;
  }
  const byFace = new Map<number, number[]>();
  for (const e of data.edgeList) {
    const fs = Array.isArray(e.faces) ? (e.faces as number[]) : [];
    for (const f of fs) for (const g of fs) if (f !== g) (byFace.get(f) ?? byFace.set(f, []).get(f)!).push(g);
  }
  const seen = new Set(start);
  const stack = [...start];
  while (stack.length) {
    const f = stack.pop()!;
    for (const g of byFace.get(f) ?? []) {
      if (!seen.has(g)) {
        seen.add(g);
        stack.push(g);
      }
    }
  }
  return [...seen].sort((a, b) => a - b);
}

export const CAD_DATA_FORMAT = 'locohane-cad-viewer/1';

function decodeBytes(value: CadBuffer | null | undefined): Uint8Array {
  if (!value) return new Uint8Array(0);
  if (typeof value !== 'string') return value;
  const bin = atob(value);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// パッケージのバイト列は 8 の倍数の位置から始まる（src/cad/viewer_package.py）ので、コピーせずにそのまま見る。
// 位置がずれている（4 の倍数でない）ものだけコピーする。
function aligned(bytes: Uint8Array): Uint8Array {
  return bytes.byteOffset % 4 === 0 ? bytes : bytes.slice();
}

export function decodeFloat32(value: CadBuffer | null | undefined): Float32Array {
  const bytes = aligned(decodeBytes(value));
  return new Float32Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 4);
}

export function decodeUint32(value: CadBuffer | null | undefined): Uint32Array {
  const bytes = aligned(decodeBytes(value));
  return new Uint32Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 4);
}

export function decodeInt16(value: CadBuffer | null | undefined): Int16Array {
  const raw = decodeBytes(value);
  const bytes = raw.byteOffset % 2 === 0 ? raw : raw.slice();
  return new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
}

/** 4×4 の変換（行ごとの入れ子の配列）が使える形か。 */
export function isMatrix4(m: unknown): m is number[][] {
  return Array.isArray(m) && m.length === 4 && m.every((row) => Array.isArray(row) && row.length === 4 && row.every((v) => typeof v === 'number' && Number.isFinite(v)));
}

/** 位置合わせし直した偏差の値（float32 の並び。app.py の GET /locohane/cad/data/{id}）を取ってくる。 */
export async function fetchCadFloat32(backendUrl: string, id: string, signal?: AbortSignal): Promise<Float32Array> {
  const res = await fetch(`${backendUrl}/locohane/cad/data/${encodeURIComponent(id)}`, { credentials: 'include', signal });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return new Float32Array(await res.arrayBuffer());
}

/** 色（三角形の頂点ごとの RGB）。書き換えて使うことがあるので、パッケージのバイト列はコピーして返す。 */
export function decodeUint8(value: CadBuffer | null | undefined): Uint8Array | null {
  if (!value) return null;
  return typeof value === 'string' ? decodeBytes(value) : value.slice();
}

const PACKAGE_MAGIC = 'LCVD';
const PACKAGE_VERSION = 1;

interface PackageBlob {
  path: string[];
  type: 'bytes' | 'json';
  offset: number;
  length: number;
}

/**
 * ブラウザへ送る形のビューア用データ（src/cad/viewer_package.py の write_package）を読む。
 * 一覧（faces 等）は塊ごとに文字列にして読むので、全体が大きくても 1 本の巨大な文字列は作らない。
 */
export function parseCadPackage(buffer: ArrayBuffer): CadData {
  const view = new DataView(buffer);
  const magic = String.fromCharCode(...new Uint8Array(buffer, 0, Math.min(4, buffer.byteLength)));
  if (buffer.byteLength < 12 || magic !== PACKAGE_MAGIC) throw new Error('not a 3D viewer package');
  const version = view.getUint32(4, true);
  if (version !== PACKAGE_VERSION) throw new Error(`unsupported 3D viewer package version ${version}`);
  const headerLength = view.getUint32(8, true);
  const decoder = new TextDecoder();
  const header = JSON.parse(decoder.decode(new Uint8Array(buffer, 12, headerLength))) as {
    data: Record<string, unknown>;
    blobs: PackageBlob[];
  };
  const data = header.data;
  for (const blob of header.blobs) {
    if (blob.offset + blob.length > buffer.byteLength) throw new Error('the 3D viewer package is truncated');
    const bytes = new Uint8Array(buffer, blob.offset, blob.length);
    let node = data;
    for (const key of blob.path.slice(0, -1)) {
      if (typeof node[key] !== 'object' || node[key] === null) node[key] = {};
      node = node[key] as Record<string, unknown>;
    }
    const last = blob.path[blob.path.length - 1];
    if (blob.type === 'bytes') {
      node[last] = bytes;
    } else {
      const items = JSON.parse(decoder.decode(bytes)) as unknown[];
      const list = Array.isArray(node[last]) ? (node[last] as unknown[]) : [];
      for (const item of items) list.push(item);
      node[last] = list;
    }
  }
  return data as unknown as CadData;
}

/** ビューア用データを app.py の GET /locohane/cad/data/{dataId} から取ってきて読む。 */
export async function fetchCadPackage(backendUrl: string, dataId: string, signal?: AbortSignal): Promise<CadData> {
  const res = await fetch(`${backendUrl}/locohane/cad/data/${encodeURIComponent(dataId)}`, {
    credentials: 'include',
    signal
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return parseCadPackage(await res.arrayBuffer());
}

export function itemsOf(data: CadData, kind: CadKind): CadItemInfo[] {
  switch (kind) {
    case 'face':
      return data.faces;
    case 'edge':
      return data.edgeList;
    case 'vertex':
      return data.vertexList;
    case 'part':
      return data.parts ?? [];
    case 'hole':
      return data.holeList ?? [];
    case 'outer':
      return data.outerList ?? [];
  }
}

/** PMI の ID → 項目。 */
export function pmiById(data: CadData): Map<string, CadPmiItem> {
  const out = new Map<string, CadPmiItem>();
  const pmi = data.step?.pmi;
  for (const items of [pmi?.dimensions, pmi?.tolerances, pmi?.datums]) for (const item of items ?? []) out.set(item.id, item);
  return out;
}

/** PMI が指す要素（面・エッジ・部品）。 */
export function pmiTargets(item: CadPmiItem): CadSelection[] {
  const ids = (xs?: number[]) => xs ?? [];
  return [
    ...[...ids(item.faces), ...ids(item.faces_second)].map((id) => ({ kind: 'face' as const, id })),
    ...[...ids(item.edges), ...ids(item.edges_second)].map((id) => ({ kind: 'edge' as const, id })),
    ...ids(item.parts).map((id) => ({ kind: 'part' as const, id }))
  ];
}

export function sameSelection(a: CadSelection, b: CadSelection): boolean {
  return a.kind === b.kind && a.id === b.id;
}
