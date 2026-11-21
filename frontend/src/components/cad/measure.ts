/**
 * 3D ビューアの計測（R・⌀、2 点間の距離、平面どうしの距離、3 点を通る円弧の半径）の計算。
 * three.js を使わない純粋な関数にしている（CadScene はクリックした点・要素を渡し、描く線と文字をここから受け取る）。
 *
 * 半径・平面の向きはメッシュでなく B-rep の値（ビューア用データの faces / edgeList の radius・axisOrigin・
 * axisDirection・circleCenter・normal 等。src/cad/export_mesh.py）を使うので正確。点を使う計測（距離・3 点円弧）は、
 * 頂点・直線のエッジ・円のエッジ・平面・円筒面・球面の上の点なら形の上の正確な点に合わせ（CadScene.measurePointAt）、
 * それ以外の曲面の点は表示用の三角形の上の点になるので approximate（概算）として示す。
 */
import type { CadItemInfo, CadSelection } from './cadData';

export type Vec3 = [number, number, number];

/** 計測の種類。radius = 面・エッジの R と ⌀、distance = 2 点間、planes = 平面どうし、arc3 = 3 点を通る円弧。 */
export type MeasureTool = 'radius' | 'distance' | 'planes' | 'arc3';

export const MEASURE_TOOLS: MeasureTool[] = ['radius', 'distance', 'planes', 'arc3'];

/** 1 つの計測に要るクリックの数。 */
export const PICKS_NEEDED: Record<MeasureTool, number> = { radius: 1, distance: 2, planes: 2, arc3: 3 };

/** クリックで拾った点。item はその点が乗る要素（面・エッジ・頂点）、exact は形の上の正確な点か。 */
export interface MeasurePick {
  point: Vec3;
  item: CadSelection | null;
  exact: boolean;
}

/** 3D 表示に重ねるもの（線分は 2 点ずつ、点は印、文字は labelAt に出す）。 */
export interface MeasureDraw {
  segments: Vec3[];
  points: Vec3[];
  labelAt: Vec3;
  label: string;
}

/** 確定時に AI へ返す計測の結果（src/tools/cad_viewer.py の _valid_measurements が受ける形）。 */
export interface MeasureResult {
  type: 'radius' | 'distance' | 'plane_distance' | 'plane_angle' | 'arc3';
  [key: string]: unknown;
}

export interface CadMeasurement {
  tool: MeasureTool;
  result: MeasureResult;
  draw: MeasureDraw;
  /** 概算（表示用の三角形の上の点を使った）か。 */
  approximate: boolean;
}

/** 計測できなかった理由（文言は i18n の cad.measure.error.<key>）。 */
export type MeasureError = 'noRadius' | 'notPlane' | 'samePick' | 'collinear' | 'samePlane';

export type MeasureOutcome = { ok: CadMeasurement } | { error: MeasureError };

// 平面どうしを平行とみなす角度（度）。ビューア用データの法線は小数 6 桁に丸めてあるので、その誤差より大きくとる。
const PARALLEL_DEG = 0.01;
// 3 点円弧の円を描く分割数。
const CIRCLE_SEGMENTS = 96;

export const sub = (a: Vec3, b: Vec3): Vec3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
export const add = (a: Vec3, b: Vec3): Vec3 => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
export const scale = (a: Vec3, s: number): Vec3 => [a[0] * s, a[1] * s, a[2] * s];
export const dot = (a: Vec3, b: Vec3): number => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
export const cross = (a: Vec3, b: Vec3): Vec3 => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
export const norm = (a: Vec3): number => Math.hypot(a[0], a[1], a[2]);
export const unit = (a: Vec3): Vec3 | null => {
  const n = norm(a);
  return n > 1e-12 ? scale(a, 1 / n) : null;
};

/** 3 つの数の配列なら Vec3、そうでなければ null（ビューア用データの値は unknown なので確かめる）。 */
export function asVec3(v: unknown): Vec3 | null {
  if (!Array.isArray(v) || v.length !== 3 || !v.every((x) => typeof x === 'number' && Number.isFinite(x))) return null;
  return [v[0], v[1], v[2]];
}

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/** 表示用の数（小数 3 桁まで、末尾の 0 は付けない）。 */
export function fmtNum(n: number): string {
  const r = Math.round(n * 1000) / 1000;
  return String(Object.is(r, -0) ? 0 : r);
}

/** 軸（点 o・向き d）へ p から下ろした足。 */
function footOnAxis(p: Vec3, o: Vec3, d: Vec3): Vec3 {
  return add(o, scale(d, dot(sub(p, o), d)));
}

/** p を、中心 c・軸 d・半径 r の円（円筒なら p の高さの断面の円）の上へ寄せた点。p が軸の上なら null。 */
export function onCircle(p: Vec3, c: Vec3, d: Vec3, r: number): Vec3 | null {
  const foot = footOnAxis(p, c, d);
  const radial = unit(sub(p, foot));
  return radial ? add(foot, scale(radial, r)) : null;
}

/** 球面（中心 c・半径 r）の上へ寄せた点。 */
export function onSphere(p: Vec3, c: Vec3, r: number): Vec3 | null {
  const dir = unit(sub(p, c));
  return dir ? add(c, scale(dir, r)) : null;
}

const vecOut = (v: Vec3): number[] => v.map((x) => Math.round(x * 1e6) / 1e6);
const ref = (s: CadSelection | null) => (s ? { kind: s.kind, id: s.id } : null);

/**
 * 面・エッジの R と ⌀。円筒面・球面・円錐面（クリックした点の高さの半径）・トーラス面（管の半径 = 丸めの R）・
 * 円のエッジに対応する。線は中心（軸の足）からクリックした点への半径の線。
 */
export function measureRadius(info: CadItemInfo | undefined, pick: MeasurePick): MeasureOutcome {
  if (!info || !pick.item) return { error: 'noRadius' };
  const p = pick.point;
  const g = info.geomType;
  let r: number | null = null;
  let center: Vec3 | null = null;
  let axis: Vec3 | null = null;
  const extra: Record<string, unknown> = {};
  if (info.kind === 'face' && g === 'CYLINDER') {
    const o = asVec3(info.axisOrigin);
    axis = unit(asVec3(info.axisDirection) ?? [0, 0, 0]);
    r = num(info.radius);
    if (o && axis) center = footOnAxis(p, o, axis);
  } else if (info.kind === 'face' && g === 'SPHERE') {
    r = num(info.radius);
    center = asVec3(info.sphereCenter);
  } else if (info.kind === 'face' && g === 'CONE') {
    const apex = asVec3(info.apex);
    axis = unit(asVec3(info.axisDirection) ?? [0, 0, 0]);
    if (apex && axis) {
      center = footOnAxis(p, apex, axis);
      r = norm(sub(p, center));
      extra.radius_at = 'clicked point';
      if (num(info.semiAngleDeg) !== null) extra.semi_angle_deg = info.semiAngleDeg;
    }
  } else if (info.kind === 'face' && g === 'TORUS') {
    const o = asVec3(info.axisOrigin);
    axis = unit(asVec3(info.axisDirection) ?? [0, 0, 0]);
    const major = num(info.majorRadius);
    r = num(info.minorRadius);
    if (o && axis && major !== null) {
      // 管の中心の円（半径 major）の上で、クリックした点に一番近い点。
      const radial = unit(sub(sub(p, o), scale(axis, dot(sub(p, o), axis))));
      if (radial) center = add(o, scale(radial, major));
      extra.major_radius = major;
    }
  } else if (info.kind === 'edge' && g === 'CIRCLE') {
    r = num(info.radius);
    center = asVec3(info.circleCenter);
    axis = unit(asVec3(info.axisDirection) ?? [0, 0, 0]);
  }
  if (r === null || r <= 0 || !center) return { error: 'noRadius' };
  // 半径の線の外側の端（円・円筒の上の点。クリックした点が三角形の上でも形の上に寄せる）。
  const rim = (axis && (g === 'CYLINDER' || g === 'CIRCLE') ? onCircle(p, center, axis, r) : null) ?? p;
  const label = `R${fmtNum(r)}  ⌀${fmtNum(r * 2)}`;
  return {
    ok: {
      tool: 'radius',
      approximate: false,
      draw: { segments: [center, rim], points: [center], labelAt: rim, label },
      result: {
        type: 'radius',
        radius: r,
        diameter: r * 2,
        geomType: g,
        center: vecOut(center),
        ...(axis ? { axis: vecOut(axis) } : {}),
        item: ref(pick.item),
        ...extra
      }
    }
  };
}

/** 2 点間の距離と、各軸の成分（ΔX・ΔY・ΔZ）。 */
export function measureDistance(a: MeasurePick, b: MeasurePick): MeasureOutcome {
  const d = sub(b.point, a.point);
  const dist = norm(d);
  if (dist < 1e-12) return { error: 'samePick' };
  const approximate = !a.exact || !b.exact;
  const mid = scale(add(a.point, b.point), 0.5);
  return {
    ok: {
      tool: 'distance',
      approximate,
      draw: { segments: [a.point, b.point], points: [a.point, b.point], labelAt: mid, label: `${approximate ? '≈' : ''}${fmtNum(dist)}` },
      result: {
        type: 'distance',
        distance: dist,
        delta: vecOut(d),
        points: [vecOut(a.point), vecOut(b.point)],
        items: [ref(a.item), ref(b.item)],
        approximate
      }
    }
  };
}

/** 平面の面か（PLANE で法線と中心を持つ）。 */
export function isPlane(info: CadItemInfo | undefined): boolean {
  return !!info && info.kind === 'face' && info.geomType === 'PLANE' && !!asVec3(info.normal) && !!asVec3(info.center);
}

/**
 * 平面どうし。平行なら距離（1 つめの面の法線の向きに測る）、平行でなければ 2 つの平面のなす角（0〜90°）。
 * 線は、平行なら 1 つめの面の中心から 2 つめの平面へ下ろした垂線。
 */
export function measurePlanes(f1: CadItemInfo | undefined, f2: CadItemInfo | undefined, a: MeasurePick, b: MeasurePick): MeasureOutcome {
  if (!isPlane(f1) || !isPlane(f2)) return { error: 'notPlane' };
  if (a.item && b.item && a.item.kind === b.item.kind && a.item.id === b.item.id) return { error: 'samePick' };
  const n1 = unit(asVec3(f1!.normal)!)!;
  const n2 = unit(asVec3(f2!.normal)!)!;
  const c1 = asVec3(f1!.center)!;
  const c2 = asVec3(f2!.center)!;
  const cos = Math.min(1, Math.abs(dot(n1, n2)));
  const angle = (Math.acos(cos) * 180) / Math.PI;
  const items = [ref(a.item), ref(b.item)];
  if (angle <= PARALLEL_DEG) {
    const along = dot(sub(c2, c1), n1);
    const dist = Math.abs(along);
    if (dist < 1e-9) return { error: 'samePlane' };
    const foot = add(c1, scale(n1, along));
    return {
      ok: {
        tool: 'planes',
        approximate: false,
        draw: { segments: [c1, foot], points: [c1, foot], labelAt: scale(add(c1, foot), 0.5), label: fmtNum(dist) },
        result: { type: 'plane_distance', distance: dist, direction: vecOut(scale(n1, Math.sign(along))), items }
      }
    };
  }
  return {
    ok: {
      tool: 'planes',
      approximate: false,
      draw: { segments: [c1, c2], points: [c1, c2], labelAt: scale(add(c1, c2), 0.5), label: `∠${fmtNum(angle)}°` },
      result: { type: 'plane_angle', angle_deg: angle, items }
    }
  };
}

/** 3 点を通る円（外接円）の中心・半径・法線。3 点が一直線上（か重なる）なら null。 */
export function circleThrough(a: Vec3, b: Vec3, c: Vec3): { center: Vec3; radius: number; normal: Vec3 } | null {
  const ab = sub(b, a);
  const ac = sub(c, a);
  const n = cross(ab, ac);
  const n2 = dot(n, n);
  const lab = norm(ab);
  const lac = norm(ac);
  // 一直線上かを、辺の長さに対する平行四辺形の面積の割合（= 角の sin）で判定する（大きさによらない）。
  if (lab < 1e-12 || lac < 1e-12 || Math.sqrt(n2) < 1e-9 * lab * lac) return null;
  // 中心 = a + (|ac|² (n × ab) + |ab|² (ac × n)) / (2 |n|²)
  const off = scale(add(scale(cross(n, ab), dot(ac, ac)), scale(cross(ac, n), dot(ab, ab))), 1 / (2 * n2));
  return { center: add(a, off), radius: norm(off), normal: unit(n)! };
}

/** 3 点を通る円弧の半径。円全体を描き、3 点と中心に印を付ける。 */
export function measureArc3(a: MeasurePick, b: MeasurePick, c: MeasurePick): MeasureOutcome {
  const circle = circleThrough(a.point, b.point, c.point);
  if (!circle) return { error: 'collinear' };
  const { center, radius, normal } = circle;
  const u = unit(sub(a.point, center))!;
  const v = cross(normal, u);
  const segments: Vec3[] = [];
  let prev = a.point;
  for (let i = 1; i <= CIRCLE_SEGMENTS; i++) {
    const t = (i / CIRCLE_SEGMENTS) * Math.PI * 2;
    const q = add(center, add(scale(u, radius * Math.cos(t)), scale(v, radius * Math.sin(t))));
    segments.push(prev, q);
    prev = q;
  }
  segments.push(center, b.point);
  const approximate = !a.exact || !b.exact || !c.exact;
  return {
    ok: {
      tool: 'arc3',
      approximate,
      draw: {
        segments,
        points: [a.point, b.point, c.point, center],
        labelAt: scale(add(center, b.point), 0.5),
        label: `${approximate ? '≈' : ''}R${fmtNum(radius)}  ⌀${fmtNum(radius * 2)}`
      },
      result: {
        type: 'arc3',
        radius,
        diameter: radius * 2,
        center: vecOut(center),
        normal: vecOut(normal),
        points: [a.point, b.point, c.point].map(vecOut),
        items: [ref(a.item), ref(b.item), ref(c.item)],
        approximate
      }
    }
  };
}

/** 集めたクリック（PICKS_NEEDED[tool] 個）から計測する。info は要素の情報を引く関数。 */
export function measure(tool: MeasureTool, picks: MeasurePick[], info: (s: CadSelection | null) => CadItemInfo | undefined): MeasureOutcome {
  switch (tool) {
    case 'radius':
      return measureRadius(info(picks[0].item), picks[0]);
    case 'distance':
      return measureDistance(picks[0], picks[1]);
    case 'planes':
      return measurePlanes(info(picks[0].item), info(picks[1].item), picks[0], picks[1]);
    case 'arc3':
      return measureArc3(picks[0], picks[1], picks[2]);
  }
}
