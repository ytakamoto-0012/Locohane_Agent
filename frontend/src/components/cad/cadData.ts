/**
 * show_cad_viewer（src/tools/cad_viewer.py）が CustomElement "CadViewer" の props.data に入れて送る
 * ビューア用データの型と、base64 の配列の復号。形は src/cad/mesh_data.py の docstring と一致させる。
 */

/** hole は開いたサーフェース・メッシュの面の中の穴（src/cad/topology.py）。 */
export type CadKind = 'face' | 'edge' | 'vertex' | 'part' | 'hole';

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

/** 表示の設定（種類ごとの表示と、隠す部品）。 */
export interface CadVisibility {
  face: boolean;
  edge: boolean;
  vertex: boolean;
  hiddenParts: ReadonlySet<number>;
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
  holes?: { positions: CadBuffer; ranges: [number, number][]; tolerance?: number; outerLoops?: number; openChains?: number };
  holeList?: CadItemInfo[];
  /** モデルの中の直交する 3 本の線で決まるローカル座標系（src/cad/topology.py の find_local_axes）。 */
  localAxes?: CadLocalAxis[];
  /** 前の版との比較（show_cad_viewer の compare_with）。added / removed は増えた・削った部分の三角形。 */
  diff?: { added?: CadTriangles; removed?: CadTriangles; error?: string; [key: string]: unknown };
  /** 「保存」で書いたデータにある、保存したときの表示（隠した部品・種類ごとの表示）。 */
  viewState?: {
    hiddenParts?: number[];
    visible?: Partial<Record<'face' | 'edge' | 'vertex', boolean>>;
    /** 自動保存・保存したときの「元に戻す」「やり直す」の履歴（開き直しても続きから戻せる。CadViewerBar.tsx）。 */
    history?: CadSavedHistory;
  };
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

/**
 * 穴 id と同じ向き（法線のなす角が deg 度以内。裏向きは含めない）の穴の ID（id 自身を含む、昇順）。
 * 法線は面の表側を向く（src/cad/topology.py）。
 */
export function holeGroup(data: CadData, id: number, deg: number): number[] {
  const holes = data.holeList ?? [];
  const base = holes[id]?.normal;
  if (!base) return holes[id] ? [id] : [];
  const cos = Math.cos((Math.max(0, Math.min(180, deg)) * Math.PI) / 180);
  const out: number[] = [];
  holes.forEach((h, i) => {
    const n = h.normal;
    // 丸めた単位ベクトルなので、内積を 1 で切ってから比べる（0° のとき自身が外れないように少し甘くする）。
    if (i === id || (n && Math.min(1, dot3(base, n)) >= cos - 1e-9)) out.push(i);
  });
  return out;
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
