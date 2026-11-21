/**
 * show_cad_viewer（src/tools/cad_viewer.py）が CustomElement "CadViewer" の props.data に入れて送る
 * ビューア用データの型と、base64 の配列の復号。形は src/cad/mesh_data.py の docstring と一致させる。
 */

export type CadKind = 'face' | 'edge' | 'vertex' | 'part';

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

/** 利用者がビューアで変えた色（#rrggbb）。キーは面の ID・部品の ID。 */
export interface CadColorOverrides {
  faces: ReadonlyMap<number, string>;
  parts: ReadonlyMap<number, string>;
}

/** 要素の今の色（変えた色 → 部品の変えた色 → ファイルの色）。色が無ければ null（テーマの既定色で描く）。 */
export function effectiveColor(data: CadData, overrides: CadColorOverrides, s: CadSelection): string | null {
  if (s.kind === 'part') return overrides.parts.get(s.id) ?? data.parts?.[s.id]?.color ?? null;
  if (s.kind !== 'face') return null;
  const info = data.faces[s.id];
  const part = info?.part;
  return overrides.faces.get(s.id) ?? (part !== undefined ? overrides.parts.get(part) : undefined) ?? info?.color ?? null;
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

export interface CadData {
  format: string;
  source: { kind: string; format?: string; path?: string };
  units: string;
  bbox: { min: number[]; max: number[]; size: number[] };
  mesh: { positions: string; indices: string; colors: string | null; faceRanges: [number, number][] };
  edges: { positions: string; ranges: [number, number][] };
  vertices: { positions: string };
  faces: CadItemInfo[];
  edgeList: CadItemInfo[];
  vertexList: CadItemInfo[];
  parts?: CadItemInfo[];
  stats: Record<string, number>;
  /** 「保存」で書いたデータにある、保存したときの表示（隠した部品・種類ごとの表示）。 */
  viewState?: { hiddenParts?: number[]; visible?: Partial<Record<'face' | 'edge' | 'vertex', boolean>> };
}

export interface CadViewerProps {
  question?: string;
  select?: CadKind[];
  data?: CadData;
  /** 「保存」ボタンで app.py の action_callback "cad_viewer_save" に渡す ID。 */
  save_id?: string;
  /** 指示付きで確定したときに撮る画面の長辺（ピクセル、[cad_viewer].screenshot_long_side_pixels）。0 以下・無しなら撮らない。 */
  screenshot_long_side?: number;
}

export const CAD_DATA_FORMAT = 'locohane-cad-viewer/1';

function decodeBytes(b64: string | null | undefined): Uint8Array {
  if (!b64) return new Uint8Array(0);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function decodeFloat32(b64: string | null | undefined): Float32Array {
  const bytes = decodeBytes(b64);
  return new Float32Array(bytes.buffer, 0, bytes.byteLength / 4);
}

export function decodeUint32(b64: string | null | undefined): Uint32Array {
  const bytes = decodeBytes(b64);
  return new Uint32Array(bytes.buffer, 0, bytes.byteLength / 4);
}

export function decodeUint8(b64: string | null | undefined): Uint8Array | null {
  return b64 ? decodeBytes(b64) : null;
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
  }
}

export function sameSelection(a: CadSelection, b: CadSelection): boolean {
  return a.kind === b.kind && a.id === b.id;
}
