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
  selectors?: string[];
  meshHints?: string[];
  [key: string]: unknown;
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
}

export interface CadViewerProps {
  question?: string;
  select?: CadKind[];
  data?: CadData;
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
