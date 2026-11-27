/**
 * 3D ビューアの表面の解析表示（CadScene から使う。three.js を使うので CadScene と同じく遅延読み込みされる側に置く）。
 *
 * - ゼブラ: 視線の反射の向きが、仮想の照明（ドーム状の縞・モデルを囲む筒に並ぶ蛍光灯・平らなパネルに並ぶ蛍光灯。
 *   円筒・平面は水平／垂直に並べ、照明全体を X・Y・Z 軸の周りに回せる）のどこに当たるかで白黒の縞を描く。
 *   面のつながりの滑らかさ（G0・G1・G2）が縞の切れ・折れ・曲がりで見える。
 *   ZebraLightGuide は、その照明の形（球・筒・パネル）と明るい縞の並び・軸の向きを 3D 空間に重ねて描く。
 * - スカラー（曲率・偏差）: 頂点ごとの値を色にする。曲率は青 → 赤の虹色、偏差は公差内を緑、外側を黄 → 赤・水色 → 青。
 *
 * 法線は src/cad/analysis.py の頂点ごとの法線（ビューア用データの analysis.normals）を使う（無ければ三角形から求めた法線）。
 * 選択・ホバーの強調は頂点ごとの値（attribute hl: 0 = なし、1 = ホバー、2 = 選択）で色を重ねる。
 */
import * as THREE from 'three';

/** 照明の形。dome = 無限に遠い球に描いた縞、cylinder = モデルを囲む筒に並ぶ蛍光灯、plane = 平らなパネルに並ぶ蛍光灯。 */
export type ZebraLight = 'dome' | 'cylinder' | 'plane';
/** 円筒・平面の蛍光灯の並べ方（回転前の照明の座標で、水平 = 右向きに伸びる、垂直 = 上向きに伸びる）。 */
export type ZebraOrientation = 'horizontal' | 'vertical';

export interface ZebraSettings {
  light: ZebraLight;
  /** 円筒・平面の蛍光灯の向き（ドームでは使わない）。 */
  orientation: ZebraOrientation;
  /** 縞（蛍光灯）の数。 */
  stripes: number;
  /** 明るい部分の割合（0.05〜0.95）。 */
  width: number;
  /**
   * 照明を X・Y・Z 軸の周りに回す角度（度。X → Y → Z の順に回す）。軸は視点に固定なら画面の右・上・手前、
   * モデルに固定ならワールドの X・Y・Z。ドーム・円筒はモデルの中心の周り、平面はパネルの中心の周りに回す。
   */
  rotXDeg: number;
  rotYDeg: number;
  rotZDeg: number;
  /**
   * 平面の光源（パネルの中心）の位置。モデルの中心から、回転前の照明の右・上・手前へのずれ（モデル半径の倍数）。
   * パネルは手前の向きを法線にしてモデルの方を向く。
   */
  planePos: [number, number, number];
  /** true なら照明を視点に固定（回すと縞が流れる）、false ならモデル（ワールド）に固定。 */
  viewFixed: boolean;
  /** true なら照明（ドーム・トンネル・筒と、その中の明るい縞）の形と向きを 3D 空間に重ねて描く。 */
  showLights: boolean;
  /** true なら塗装面のような艶（視点に固定したキーライトのハイライト・縁の映り込み・上下の明暗の映り込み）を重ねる。 */
  gloss: boolean;
}

/** 縞（蛍光灯）の数の上限。 */
export const MAX_ZEBRA_STRIPES = 1000;

export const DEFAULT_ZEBRA: ZebraSettings = {
  light: 'dome',
  orientation: 'horizontal',
  stripes: 12,
  width: 0.5,
  rotXDeg: 0,
  rotYDeg: 0,
  rotZDeg: 0,
  planePos: [0, 0, 3],
  viewFixed: true,
  showLights: false,
  gloss: true
};

/** 照明の形の大きさ（モデル半径の倍数）。トンネル・筒の半径はシェーダーの tunnel と同じ 3、筒の長さは軸に沿って ±LIGHT_GUIDE_HALF_LENGTH。 */
export const LIGHT_GUIDE_RADIUS = 3;
export const LIGHT_GUIDE_HALF_LENGTH = 4;
/** 平面の光源（正方形のパネル）の一辺の半分（モデル半径の倍数）。 */
export const LIGHT_PLANE_HALF_SIZE = 4;

/** 曲率の種類。mean = 平均曲率 (k1+k2)/2、gauss = ガウス曲率 k1·k2、k1・k2 = 最大・最小主曲率、absmax = max(|k1|,|k2|)。 */
export type CurvatureKind = 'mean' | 'gauss' | 'k1' | 'k2' | 'absmax';
export const CURVATURE_KINDS: CurvatureKind[] = ['mean', 'gauss', 'k1', 'k2', 'absmax'];

/** スカラーの色の付け方。rainbow = 範囲を青 → 赤、deviation = ±公差を緑、外側を黄 → 赤（正）・水色 → 青（負）。 */
export type ScalarScheme = 'rainbow' | 'deviation';

export interface ScalarDisplay {
  values: Float32Array;
  min: number;
  max: number;
  /** deviation のときの公差（±）。 */
  tolerance: number;
  scheme: ScalarScheme;
}

export type SurfaceDisplay = { mode: 'normal' } | { mode: 'zebra'; zebra: ZebraSettings } | { mode: 'scalar'; scalar: ScalarDisplay };

/** 曲率の値（頂点ごと）。 */
export function curvatureValues(k1: Float32Array, k2: Float32Array, kind: CurvatureKind): Float32Array {
  const out = new Float32Array(k1.length);
  for (let i = 0; i < k1.length; i++) {
    const a = k1[i];
    const b = k2[i];
    out[i] = kind === 'mean' ? (a + b) / 2 : kind === 'gauss' ? a * b : kind === 'k1' ? a : kind === 'k2' ? b : Math.max(Math.abs(a), Math.abs(b));
  }
  return out;
}

/**
 * 色の範囲の目安（外れ値に引きずられないよう、ばらつきの lo〜hi 分位点）。symmetric なら 0 を中心にそろえる。
 * 値が多いときは間引いて並べる（数百万頂点でも速く）。
 */
export function autoRange(values: Float32Array, symmetric: boolean, lo = 0.02, hi = 0.98): [number, number] {
  const n = values.length;
  if (!n) return [-1, 1];
  const step = Math.max(1, Math.floor(n / 200000));
  const sample: number[] = [];
  for (let i = 0; i < n; i += step) {
    const v = values[i];
    if (Number.isFinite(v)) sample.push(v);
  }
  if (!sample.length) return [-1, 1];
  sample.sort((a, b) => a - b);
  const q = (p: number) => sample[Math.min(sample.length - 1, Math.max(0, Math.round(p * (sample.length - 1))))];
  let a = q(lo);
  let b = q(hi);
  if (symmetric) {
    const m = Math.max(Math.abs(a), Math.abs(b));
    a = -m;
    b = m;
  }
  if (!(b > a)) {
    const m = Math.max(Math.abs(a), 1e-6);
    return [a - m, a + m];
  }
  return [a, b];
}

/** 虹色（青 → 水色 → 緑 → 黄 → 赤）。t は 0〜1。シェーダーの colormap と同じ式。 */
export function rainbow(t: number): [number, number, number] {
  const c = Math.max(0, Math.min(1, t));
  const f = (x: number) => Math.max(0, Math.min(1, x));
  return [f(1.5 - Math.abs(4 * c - 3)), f(1.5 - Math.abs(4 * c - 2)), f(1.5 - Math.abs(4 * c - 1))];
}

/** 偏差の色（シェーダーと同じ）。 */
export function deviationColor(v: number, min: number, max: number, tol: number): [number, number, number] {
  if (Math.abs(v) <= tol) return [0.2, 0.74, 0.32];
  if (v > 0) return rainbow(0.62 + 0.38 * Math.max(0, Math.min(1, (v - tol) / Math.max(max - tol, 1e-12))));
  return rainbow(0.38 - 0.38 * Math.max(0, Math.min(1, (-v - tol) / Math.max(-min - tol, 1e-12))));
}

/** 凡例（CSS の linear-gradient。左 = min、右 = max）。 */
export function legendGradient(s: Pick<ScalarDisplay, 'min' | 'max' | 'tolerance' | 'scheme'>): string {
  const stops: string[] = [];
  const N = 24;
  for (let i = 0; i <= N; i++) {
    const t = i / N;
    const v = s.min + (s.max - s.min) * t;
    const [r, g, b] = s.scheme === 'deviation' ? deviationColor(v, s.min, s.max, s.tolerance) : rainbow(t);
    stops.push(`rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)}) ${(t * 100).toFixed(1)}%`);
  }
  return `linear-gradient(to right, ${stops.join(', ')})`;
}

const VERTEX = /* glsl */ `
attribute vec3 aNormal;
attribute float hl;
attribute float aScalar;
varying vec3 vNormalW;
varying vec3 vPosW;
varying float vHl;
varying float vScalar;
void main() {
  vNormalW = normalize(mat3(modelMatrix) * aNormal);
  vec4 pw = modelMatrix * vec4(position, 1.0);
  vPosW = pw.xyz;
  vHl = hl;
  vScalar = aScalar;
  gl_Position = projectionMatrix * viewMatrix * pw;
}
`;

// ゼブラの照明（モデルの縞を描く材質と、照明の形を描く材質で共通）。
const ZEBRA_LIGHTS_GLSL = /* glsl */ `
uniform int uLight;       // 0 = ドーム, 1 = 円筒, 2 = 平面
uniform int uVertical;    // 円筒・平面: 0 = 水平（蛍光灯が右向きに伸びる）, 1 = 垂直（上向きに伸びる）
uniform float uStripes;
uniform float uWidth;
uniform vec3 uUp;         // 照明の座標の上・手前（見る人の側）・右（ワールドの向き。X・Y・Z の回転を入れた後）
uniform vec3 uFront;
uniform vec3 uRight;
uniform vec3 uCenter;
uniform float uRadius;
uniform vec3 uPlanePos;   // 平面の光源（パネルの中心）の、モデルの中心からの位置（ワールド）

// 縞の値 s（整数ごとに 1 本）を、幅 uWidth の明るい帯にする（画面上で 1 ピクセル程度ぼかしてギザギザを抑える）。
// 縞が 1 ピクセルより細かく詰まるところ（曲がりの強い丸め・かすめる反射）は、ちらつかないよう平均の明るさに寄せる。
float band(float s) {
  float f = fract(s);
  float w = fwidth(s);
  float aa = clamp(w, 1e-4, 0.5);
  float rise = clamp(smoothstep(0.0, aa, f) - smoothstep(uWidth, uWidth + aa, f), 0.0, 1.0);
  return mix(rise, uWidth, smoothstep(0.35, 0.9, w));
}

// 縞の値 s の明るい帯の中心（fract(s) = uWidth / 2）に、画面上で 1〜2 ピクセルの線を引く（光源の表示用。帯の幅は描かない）。
// 線が詰まりすぎるところは消す。
float centerLine(float s) {
  float w = max(fwidth(s), 1e-5);
  float d = abs(fract(s - 0.5 * uWidth + 0.5) - 0.5);
  return (1.0 - smoothstep(0.6 * w, 1.4 * w, d)) * (1.0 - smoothstep(0.15, 0.4, w));
}

// 縞の値 s の明るさ。光源の表示（ZEBRA_LINES を定義したシェーダー）では帯の中心線だけ、モデルでは幅のある帯。
float stripe(float s) {
#ifdef ZEBRA_LINES
  return centerLine(s);
#else
  return band(s);
#endif
}

// ドーム（無限に遠い球）の向き d の明るさ。軸からの緯度で縞を決める。
float domeAt(vec3 d, vec3 axis) {
  return stripe(asin(clamp(dot(d, axis), -1.0, 1.0)) / 3.14159265 * uStripes);
}

// 筒の上の点 q（中心からの位置を、軸に垂直な面へ写したもの）の明るさ。軸の周りの角度で決め、
// floorDir が 0 でなければ、その向きから ±30 度の範囲（床）は暗い。
float tubeAt(vec3 q, vec3 axis, vec3 e1, vec3 floorDir) {
  if (dot(floorDir, floorDir) > 0.5 && dot(normalize(q), floorDir) > 0.866) return 0.0;
  vec3 e2 = cross(axis, e1);
  float ang = atan(dot(q, e2), dot(q, e1));
  return stripe(ang / 6.28318531 * uStripes + 0.5 * (1.0 - uWidth));
}

// モデルを囲む筒（軸 axis、モデルの中心を通る、半径 3R）の内側に、軸に沿った蛍光灯が周りに uStripes 本並ぶ。
// 反射の光線が筒に当たる位置の角度で明るさを決める（筒の中からの光線は必ず筒に当たる）。floorDir が 0 でなければ、
// その向きから ±30 度の範囲（床）には蛍光灯を置かない。
float tunnel(vec3 p, vec3 r, vec3 axis, vec3 e1, vec3 floorDir) {
  vec3 pp = p - dot(p, axis) * axis;
  vec3 rp = r - dot(r, axis) * axis;
  float a = dot(rp, rp);
  if (a < 1e-6) return 0.0;  // 軸と平行な反射（筒の端の方へ抜ける）
  float R = 3.0 * uRadius;
  float b = dot(pp, rp);
  float c = dot(pp, pp) - R * R;
  float t = (-b + sqrt(max(b * b - a * c, 0.0))) / a;
  return tubeAt(pp + t * rp, axis, e1, floorDir);
}

// 照明の軸（surfaceShading.ts の lightAxis と同じ式）。ドームは縞の極の向き（上）、円筒は筒の軸、平面はパネルの法線（手前）。
// 水平の円筒: 横向きのトンネル（軸は右）の天井と壁に並ぶ。床は暗い。垂直の円筒: モデルを囲む縦の筒（軸は上）に縦に並ぶ。
vec3 lightAxis() {
  if (uLight == 2) return uFront;
  if (uLight == 1 && uVertical == 0) return uRight;
  return uUp;
}

// 筒の周りの角度を測る基準の向き（水平のトンネルは天井、垂直の筒は手前）。
vec3 tubeRef(vec3 axis) {
  vec3 ref = uVertical == 0 ? uUp : uFront;
  return normalize(ref - dot(ref, axis) * axis);
}

// 平面のパネル（中心からの位置 q）の明るさ。一辺 2 × ${LIGHT_PLANE_HALF_SIZE.toFixed(1)}R の正方形の外は暗い。
// 水平なら上下の位置、垂直なら左右の位置で縞を決め、パネルの中心に明るい蛍光灯が 1 本来るようにそろえる。
float panelAt(vec3 q) {
  float h = ${LIGHT_PLANE_HALF_SIZE.toFixed(1)} * uRadius;
  float a = dot(q, uRight);
  float b = dot(q, uUp);
  if (abs(a) > h || abs(b) > h) return 0.0;
  float c = uVertical == 1 ? a : b;
  return stripe(c / (2.0 * h) * uStripes + 0.5 * uWidth);
}

// 点 p（中心からの位置）から向き r の光線が平面のパネルに当たるところの明るさ（パネルの裏側へ向かう光線も、前に当たれば明るい）。
float plane(vec3 p, vec3 r) {
  float d = dot(r, uFront);
  if (abs(d) < 1e-6) return 0.0;
  float t = dot(uPlanePos - p, uFront) / d;
  if (t <= 0.0) return 0.0;
  return panelAt(p + t * r - uPlanePos);
}
`;

const FRAGMENT = /* glsl */ `
uniform int uMode;        // 1 = ゼブラ, 2 = スカラー
uniform vec3 uToEye;      // 面からカメラへの向き（平行投影なので一定）
uniform int uScheme;      // 0 = 虹色, 1 = 偏差
uniform float uMin;
uniform float uMax;
uniform float uTol;
uniform vec3 uHover;
uniform vec3 uSelected;
uniform float uGloss;     // 1 = 艶あり
uniform vec3 uKeyLight;   // 艶のハイライトを作るキーライトの向き（視点に固定、左上手前）
uniform vec3 uCamUp;      // 画面の上向き（映り込みの上下の明暗に使う）
varying vec3 vNormalW;
varying vec3 vPosW;
varying float vHl;
varying float vScalar;
${ZEBRA_LIGHTS_GLSL}

vec3 rainbow(float t) {
  t = clamp(t, 0.0, 1.0);
  return clamp(vec3(1.5 - abs(4.0 * t - 3.0), 1.5 - abs(4.0 * t - 2.0), 1.5 - abs(4.0 * t - 1.0)), 0.0, 1.0);
}

void main() {
  vec3 n = normalize(vNormalW);
  if (!gl_FrontFacing) n = -n;
  float facing = abs(dot(n, uToEye));
  vec3 col;
  if (uMode == 1) {
    vec3 r = reflect(-uToEye, n);
    vec3 p = vPosW - uCenter;
    vec3 axis = lightAxis();
    float lit = 0.0;
    if (uLight == 0) {
      lit = domeAt(r, axis);
    } else if (uLight == 2) {
      lit = plane(p, r);
    } else {
      vec3 e1 = tubeRef(axis);
      lit = tunnel(p, r, axis, e1, uVertical == 0 ? -e1 : vec3(0.0));
    }
    if (uGloss > 0.5) {
      // 塗装面のような艶。暗い部分には上が明るく下が暗い周りの映り込み、明るい縞は中心ほど白く飛ぶ光にする。
      float sky = 0.5 + 0.5 * dot(r, uCamUp);
      vec3 dark = mix(vec3(0.012, 0.013, 0.016), vec3(0.10, 0.105, 0.115), sky * sky);
      vec3 light = mix(vec3(0.80, 0.81, 0.83), vec3(1.0), sky);
      col = mix(dark, light, lit);
      // 縁ほど映り込みが強くなる（Schlick のフレネル）。縞のコントラストを潰さないよう控えめにする。
      float fres = pow(1.0 - facing, 5.0);
      col = mix(col, vec3(0.75), 0.04 + 0.30 * fres);
      // キーライトの鋭いハイライトと、その周りのにじみ。
      vec3 h = normalize(uKeyLight + uToEye);
      float nh = max(dot(n, h), 0.0);
      col += vec3(0.9) * pow(nh, 220.0) + vec3(0.12) * pow(nh, 18.0);
      col = min(col, vec3(1.0));
    } else {
      vec3 dark = vec3(0.06);
      vec3 light = vec3(0.97);
      col = mix(dark, light, lit);
      // 形が読めるよう、少しだけ陰影を残す。
      col *= 0.82 + 0.18 * facing;
    }
  } else {
    float v = vScalar;
    if (v != v) {
      col = vec3(0.55);
    } else if (uScheme == 1) {
      if (abs(v) <= uTol) col = vec3(0.2, 0.74, 0.32);
      else if (v > 0.0) col = rainbow(0.62 + 0.38 * clamp((v - uTol) / max(uMax - uTol, 1e-12), 0.0, 1.0));
      else col = rainbow(0.38 - 0.38 * clamp((-v - uTol) / max(-uMin - uTol, 1e-12), 0.0, 1.0));
    } else {
      col = rainbow((v - uMin) / max(uMax - uMin, 1e-12));
    }
    col *= 0.5 + 0.5 * facing;
  }
  if (vHl > 1.5) col = mix(col, uSelected, 0.55);
  else if (vHl > 0.5) col = mix(col, uHover, 0.5);
  gl_FragColor = vec4(col, 1.0);
}
`;

/** 解析表示の材質（ゼブラ・スカラー共通）。 */
export function createSurfaceMaterial(): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    vertexShader: VERTEX,
    fragmentShader: FRAGMENT,
    side: THREE.DoubleSide,
    polygonOffset: true,
    polygonOffsetFactor: 1,
    polygonOffsetUnits: 1,
    uniforms: {
      uMode: { value: 1 },
      uLight: { value: 0 },
      uVertical: { value: 0 },
      uStripes: { value: DEFAULT_ZEBRA.stripes },
      uWidth: { value: DEFAULT_ZEBRA.width },
      uUp: { value: new THREE.Vector3(0, 0, 1) },
      uFront: { value: new THREE.Vector3(0, -1, 0) },
      uRight: { value: new THREE.Vector3(1, 0, 0) },
      uToEye: { value: new THREE.Vector3(0, 0, 1) },
      uCenter: { value: new THREE.Vector3() },
      uRadius: { value: 1 },
      uPlanePos: { value: new THREE.Vector3(0, -3, 0) },
      uScheme: { value: 0 },
      uMin: { value: -1 },
      uMax: { value: 1 },
      uTol: { value: 0 },
      uHover: { value: new THREE.Color('#1fb6d4') },
      uSelected: { value: new THREE.Color('#ff7a00') },
      uGloss: { value: DEFAULT_ZEBRA.gloss ? 1 : 0 },
      uKeyLight: { value: new THREE.Vector3(0, 0, 1) },
      uCamUp: { value: new THREE.Vector3(0, 1, 0) }
    }
  });
}

const _euler = new THREE.Euler();
const _rot = new THREE.Quaternion();
const _base = new THREE.Quaternion();
const _v = new THREE.Vector3();
const DEG = Math.PI / 180;

/**
 * 材質の照明の向きを今のカメラに合わせる（描くたびに呼ぶ）。視点に固定する照明は、カメラの上・手前・右を照明の
 * 上・手前・右にする。モデルに固定する照明は、ワールドの +Z を上、−Y を手前（正面から +Y の向きに見る）、+X を右にする。
 * そのうえで X・Y・Z 軸（視点に固定なら画面の右・上・手前、モデルに固定ならワールドの X・Y・Z）の周りに X → Y → Z の順に回す。
 * 平面の光源の位置は、回す前の照明の右・上・手前で測る（パネルはその位置を中心に回る）。
 */
export function updateSurfaceUniforms(material: THREE.ShaderMaterial, camera: THREE.Camera, zebra: ZebraSettings | null): void {
  const u = material.uniforms;
  const q = camera.quaternion;
  (u.uToEye.value as THREE.Vector3).set(0, 0, 1).applyQuaternion(q);
  (u.uKeyLight.value as THREE.Vector3).set(-0.45, 0.6, 0.66).normalize().applyQuaternion(q);
  (u.uCamUp.value as THREE.Vector3).set(0, 1, 0).applyQuaternion(q);
  if (!zebra) return;
  if (zebra.viewFixed) _base.copy(q);
  else _base.identity();
  _euler.set((zebra.rotXDeg || 0) * DEG, (zebra.rotYDeg || 0) * DEG, (zebra.rotZDeg || 0) * DEG, 'XYZ');
  _rot.setFromEuler(_euler).premultiply(_base);
  // 回す前の照明の上・手前（X・Y・Z の軸の座標で）。右はどちらも +X。
  const upZ = zebra.viewFixed ? 0 : 1;
  const upY = zebra.viewFixed ? 1 : 0;
  const frontY = zebra.viewFixed ? 0 : -1;
  const frontZ = zebra.viewFixed ? 1 : 0;
  (u.uUp.value as THREE.Vector3).set(0, upY, upZ).applyQuaternion(_rot);
  (u.uFront.value as THREE.Vector3).set(0, frontY, frontZ).applyQuaternion(_rot);
  (u.uRight.value as THREE.Vector3).set(1, 0, 0).applyQuaternion(_rot);
  const [px, py, pz] = zebra.planePos ?? DEFAULT_ZEBRA.planePos;
  _v.set(px || 0, (py || 0) * upY + (pz || 0) * frontY, (py || 0) * upZ + (pz || 0) * frontZ).applyQuaternion(_base);
  (u.uPlanePos.value as THREE.Vector3).copy(_v).multiplyScalar(u.uRadius.value as number);
}

/** 表示の設定を材質に入れる。 */
export function applySurfaceDisplay(material: THREE.ShaderMaterial, display: SurfaceDisplay): void {
  const u = material.uniforms;
  if (display.mode === 'zebra') {
    const z = display.zebra;
    u.uMode.value = 1;
    u.uLight.value = z.light === 'cylinder' ? 1 : z.light === 'plane' ? 2 : 0;
    u.uVertical.value = z.orientation === 'vertical' ? 1 : 0;
    u.uStripes.value = Math.max(1, Math.min(MAX_ZEBRA_STRIPES, z.stripes));
    u.uWidth.value = Math.max(0.05, Math.min(0.95, z.width));
    u.uGloss.value = z.gloss ? 1 : 0;
  } else if (display.mode === 'scalar') {
    const s = display.scalar;
    u.uMode.value = 2;
    u.uScheme.value = s.scheme === 'deviation' ? 1 : 0;
    u.uMin.value = s.min;
    u.uMax.value = s.max;
    u.uTol.value = Math.max(0, s.tolerance);
  }
}

const GUIDE_VERTEX = /* glsl */ `
varying vec3 vPosW;
void main() {
  vec4 pw = modelMatrix * vec4(position, 1.0);
  vPosW = pw.xyz;
  gl_Position = projectionMatrix * viewMatrix * pw;
}
`;

// 照明の形の上に、蛍光灯（明るい縞）の中心線だけを、モデルの縞と同じ式（ZEBRA_LIGHTS_GLSL）で描く（幅のある帯は
// モデルを隠して邪魔なので描かない）。ドームは中心から見た向き、トンネル・筒はその点の軸の周りの角度、パネルはその上の
// 位置で決まる。照明の形そのものはごく薄く描く。手前側の壁（表から見える面）の線は、モデルの縞が見えるよう薄くする。
const GUIDE_FRAGMENT = /* glsl */ `
#define ZEBRA_LINES
uniform vec3 uLitColor;
uniform vec3 uDarkColor;
varying vec3 vPosW;
${ZEBRA_LIGHTS_GLSL}
void main() {
  vec3 p = vPosW - uCenter;
  vec3 axis = lightAxis();
  float lit;
  if (uLight == 0) {
    lit = domeAt(normalize(p), axis);
  } else if (uLight == 2) {
    lit = panelAt(p - uPlanePos);
  } else {
    vec3 e1 = tubeRef(axis);
    lit = tubeAt(p - dot(p, axis) * axis, axis, e1, uVertical == 0 ? -e1 : vec3(0.0));
  }
  float side = uLight == 2 ? 0.8 : (gl_FrontFacing ? 0.35 : 0.9);
  gl_FragColor = vec4(mix(uDarkColor, uLitColor, lit), mix(0.025, 1.0, lit) * side);
}
`;

/** 照明の軸（シェーダーの lightAxis と同じ式）。up・front・right は updateSurfaceUniforms で入れた照明の座標。 */
function lightAxis(zebra: ZebraSettings, up: THREE.Vector3, front: THREE.Vector3, right: THREE.Vector3): THREE.Vector3 {
  if (zebra.light === 'plane') return front.clone();
  if (zebra.light === 'cylinder' && zebra.orientation !== 'vertical') return right.clone();
  return up.clone();
}

const Y_AXIS = new THREE.Vector3(0, 1, 0);
const GUIDE_AXIS_COLOR = 0xff9f1c;
const _basis = new THREE.Matrix4();

/**
 * ゼブラの照明の形を 3D 空間に描く（ドームは半径 3R の球、円筒は半径 3R・長さ ±4R の筒、平面は一辺 8R の正方形のパネル）。
 * 明るい縞（蛍光灯）は解析表示の材質と同じ uniform を使って同じ式で描くので、本数・幅・回転・位置・向きの変更がそのまま映る。
 * 照明の軸（ドームは縞の極、円筒は筒の軸、平面はパネルからモデルへ向かう法線）を矢印で示す。選択・当たり判定の対象にはしない。
 */
export class ZebraLightGuide {
  readonly group = new THREE.Group();
  private readonly material: THREE.ShaderMaterial;
  private readonly dome: THREE.Mesh;
  private readonly tube: THREE.Mesh;
  private readonly panel: THREE.Mesh;
  private readonly arrow: THREE.ArrowHelper;

  constructor(surface: THREE.ShaderMaterial) {
    const su = surface.uniforms;
    this.material = new THREE.ShaderMaterial({
      vertexShader: GUIDE_VERTEX,
      fragmentShader: GUIDE_FRAGMENT,
      side: THREE.DoubleSide,
      transparent: true,
      depthWrite: false,
      // 照明の値はモデルの材質と同じ uniform（同じ物）を共有する。
      uniforms: {
        uLight: su.uLight,
        uVertical: su.uVertical,
        uStripes: su.uStripes,
        uWidth: su.uWidth,
        uUp: su.uUp,
        uFront: su.uFront,
        uRight: su.uRight,
        uCenter: su.uCenter,
        uRadius: su.uRadius,
        uPlanePos: su.uPlanePos,
        uLitColor: { value: new THREE.Color('#ffe9a8') },
        uDarkColor: { value: new THREE.Color('#3a4252') }
      }
    });
    this.dome = new THREE.Mesh(new THREE.SphereGeometry(1, 128, 64), this.material);
    this.tube = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, 2, 256, 1, true), this.material);
    this.panel = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), this.material);
    this.arrow = new THREE.ArrowHelper(Y_AXIS, new THREE.Vector3(), 1, GUIDE_AXIS_COLOR);
    for (const o of [this.dome, this.tube, this.panel]) {
      o.renderOrder = 2;
      o.frustumCulled = false;
    }
    this.group.add(this.dome, this.tube, this.panel, this.arrow);
    this.group.visible = false;
  }

  /** 形と向きを今の設定・照明の座標に合わせる（updateSurfaceUniforms の後、描くたびに呼ぶ）。 */
  update(zebra: ZebraSettings, center: THREE.Vector3, radius: number): void {
    const u = this.material.uniforms;
    const up = u.uUp.value as THREE.Vector3;
    const front = u.uFront.value as THREE.Vector3;
    const right = u.uRight.value as THREE.Vector3;
    const axis = lightAxis(zebra, up, front, right);
    const R = LIGHT_GUIDE_RADIUS * radius;
    this.dome.visible = zebra.light === 'dome';
    this.tube.visible = zebra.light === 'cylinder';
    this.panel.visible = zebra.light === 'plane';
    if (zebra.light === 'plane') {
      // パネルの面の右・上を照明の右・上、法線を手前にする（右 × 上 = 手前なので右手系のまま）。
      const h = LIGHT_PLANE_HALF_SIZE * radius;
      this.panel.position.copy(center).add(u.uPlanePos.value as THREE.Vector3);
      this.panel.quaternion.setFromRotationMatrix(_basis.makeBasis(right, up, front));
      this.panel.scale.set(h, h, 1);
      // 矢印はパネルの中心からモデルの側（−手前）へ、パネルとモデルの中心の距離ほど伸ばす。
      const dir = front.clone().negate();
      const len = Math.max(Math.abs((u.uPlanePos.value as THREE.Vector3).dot(front)), radius);
      this.arrow.position.copy(this.panel.position);
      this.arrow.setDirection(dir);
      this.arrow.setLength(len, 0.12 * len, 0.05 * len);
      return;
    }
    if (zebra.light === 'dome') {
      this.dome.position.copy(center);
      this.dome.scale.setScalar(R);
    } else {
      this.tube.position.copy(center);
      this.tube.quaternion.setFromUnitVectors(Y_AXIS, axis);
      this.tube.scale.set(R, LIGHT_GUIDE_HALF_LENGTH * radius, R);
    }
    // 軸の矢印は形を貫いて少し外まで伸ばす。
    const half = (zebra.light === 'dome' ? LIGHT_GUIDE_RADIUS : LIGHT_GUIDE_HALF_LENGTH) * radius * 1.15;
    this.arrow.position.copy(center).addScaledVector(axis, -half);
    this.arrow.setDirection(axis);
    this.arrow.setLength(2 * half, 0.12 * half, 0.05 * half);
  }

  dispose(): void {
    this.dome.geometry.dispose();
    this.tube.geometry.dispose();
    this.panel.geometry.dispose();
    this.material.dispose();
    this.arrow.dispose();
  }
}
