/**
 * 3D ビューアの表面の解析表示（CadScene から使う。three.js を使うので CadScene と同じく遅延読み込みされる側に置く）。
 *
 * - ゼブラ: 視線の反射の向きが、仮想の照明（ドーム状の縞・モデルを囲むトンネルに並ぶ水平な蛍光灯・モデルを囲む
 *   縦の蛍光灯）のどこに当たるかで白黒の縞を描く。面のつながりの滑らかさ（G0・G1・G2）が縞の切れ・折れ・曲がりで見える。
 * - スカラー（曲率・偏差）: 頂点ごとの値を色にする。曲率は青 → 赤の虹色、偏差は公差内を緑、外側を黄 → 赤・水色 → 青。
 *
 * 法線は src/cad/analysis.py の頂点ごとの法線（ビューア用データの analysis.normals）を使う（無ければ三角形から求めた法線）。
 * 選択・ホバーの強調は頂点ごとの値（attribute hl: 0 = なし、1 = ホバー、2 = 選択）で色を重ねる。
 */
import * as THREE from 'three';

export type ZebraLight = 'dome' | 'horizontal' | 'vertical';

export interface ZebraSettings {
  light: ZebraLight;
  /** 縞（蛍光灯）の数。 */
  stripes: number;
  /** 明るい部分の割合（0.05〜0.95）。 */
  width: number;
  /** 縞の向きを回す角度（度）。 */
  angleDeg: number;
  /** true なら照明を視点に固定（回すと縞が流れる）、false ならモデル（ワールド）に固定。 */
  viewFixed: boolean;
}

export const DEFAULT_ZEBRA: ZebraSettings = { light: 'dome', stripes: 12, width: 0.5, angleDeg: 0, viewFixed: true };

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

const FRAGMENT = /* glsl */ `
uniform int uMode;        // 1 = ゼブラ, 2 = スカラー
uniform int uLight;       // 0 = ドーム, 1 = 水平の蛍光灯（横向きのトンネル）, 2 = 垂直の蛍光灯（縦の筒）
uniform float uStripes;
uniform float uWidth;
uniform float uAngle;
uniform vec3 uUp;         // 照明の座標の上・手前（見る人の側）・右（ワールドの向き）
uniform vec3 uFront;
uniform vec3 uRight;
uniform vec3 uToEye;      // 面からカメラへの向き（平行投影なので一定）
uniform vec3 uCenter;
uniform float uRadius;
uniform int uScheme;      // 0 = 虹色, 1 = 偏差
uniform float uMin;
uniform float uMax;
uniform float uTol;
uniform vec3 uHover;
uniform vec3 uSelected;
varying vec3 vNormalW;
varying vec3 vPosW;
varying float vHl;
varying float vScalar;

vec3 rainbow(float t) {
  t = clamp(t, 0.0, 1.0);
  return clamp(vec3(1.5 - abs(4.0 * t - 3.0), 1.5 - abs(4.0 * t - 2.0), 1.5 - abs(4.0 * t - 1.0)), 0.0, 1.0);
}

// 縞の値 s（整数ごとに 1 本）を、幅 uWidth の明るい帯にする（画面上で 1 ピクセル程度ぼかしてギザギザを抑える）。
// 縞が 1 ピクセルより細かく詰まるところ（曲がりの強い丸め・かすめる反射）は、ちらつかないよう平均の明るさに寄せる。
float band(float s) {
  float f = fract(s);
  float w = fwidth(s);
  float aa = clamp(w, 1e-4, 0.5);
  float rise = clamp(smoothstep(0.0, aa, f) - smoothstep(uWidth, uWidth + aa, f), 0.0, 1.0);
  return mix(rise, uWidth, smoothstep(0.35, 0.9, w));
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
  vec3 q = pp + t * rp;
  if (dot(floorDir, floorDir) > 0.5 && dot(normalize(q), floorDir) > 0.866) return 0.0;
  vec3 e2 = cross(axis, e1);
  float ang = atan(dot(q, e2), dot(q, e1));
  return band(ang / 6.28318531 * uStripes + 0.5 * (1.0 - uWidth));
}

void main() {
  vec3 n = normalize(vNormalW);
  if (!gl_FrontFacing) n = -n;
  float facing = abs(dot(n, uToEye));
  vec3 col;
  if (uMode == 1) {
    vec3 r = reflect(-uToEye, n);
    vec3 p = vPosW - uCenter;
    float lit = 0.0;
    if (uLight == 0) {
      vec3 axis = cos(uAngle) * uUp + sin(uAngle) * uRight;
      float lat = asin(clamp(dot(r, axis), -1.0, 1.0));
      lit = band(lat / 3.14159265 * uStripes);
    } else if (uLight == 1) {
      // 水平な蛍光灯: 横向きのトンネル（軸は右向きを uAngle だけ奥へ回したもの）の天井と壁に並ぶ。床は暗い。
      vec3 axis = normalize(cos(uAngle) * uRight + sin(uAngle) * uFront);
      vec3 e1 = normalize(uUp - dot(uUp, axis) * axis);
      lit = tunnel(p, r, axis, e1, -e1);
    } else {
      // 垂直な蛍光灯: モデルを囲む縦の筒（軸は上向きを uAngle だけ右へ傾けたもの）に縦に並ぶ。
      vec3 axis = normalize(cos(uAngle) * uUp + sin(uAngle) * uRight);
      vec3 e1 = normalize(uFront - dot(uFront, axis) * axis);
      lit = tunnel(p, r, axis, e1, vec3(0.0));
    }
    vec3 dark = vec3(0.06);
    vec3 light = vec3(0.97);
    col = mix(dark, light, lit);
    // 形が読めるよう、少しだけ陰影を残す。
    col *= 0.82 + 0.18 * facing;
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
      uStripes: { value: DEFAULT_ZEBRA.stripes },
      uWidth: { value: DEFAULT_ZEBRA.width },
      uAngle: { value: 0 },
      uUp: { value: new THREE.Vector3(0, 0, 1) },
      uFront: { value: new THREE.Vector3(0, -1, 0) },
      uRight: { value: new THREE.Vector3(1, 0, 0) },
      uToEye: { value: new THREE.Vector3(0, 0, 1) },
      uCenter: { value: new THREE.Vector3() },
      uRadius: { value: 1 },
      uScheme: { value: 0 },
      uMin: { value: -1 },
      uMax: { value: 1 },
      uTol: { value: 0 },
      uHover: { value: new THREE.Color('#1fb6d4') },
      uSelected: { value: new THREE.Color('#ff7a00') }
    }
  });
}

/**
 * 材質の照明の向きを今のカメラに合わせる（描くたびに呼ぶ）。視点に固定する照明は、カメラの上・手前・右を照明の
 * 上・手前・右にする。モデルに固定する照明は、ワールドの +Z を上、−Y を手前（正面から +Y の向きに見る）、+X を右にする。
 */
export function updateSurfaceUniforms(material: THREE.ShaderMaterial, camera: THREE.Camera, zebra: ZebraSettings | null): void {
  const u = material.uniforms;
  const q = camera.quaternion;
  (u.uToEye.value as THREE.Vector3).set(0, 0, 1).applyQuaternion(q);
  if (!zebra) return;
  if (zebra.viewFixed) {
    (u.uUp.value as THREE.Vector3).set(0, 1, 0).applyQuaternion(q);
    (u.uFront.value as THREE.Vector3).set(0, 0, 1).applyQuaternion(q);
    (u.uRight.value as THREE.Vector3).set(1, 0, 0).applyQuaternion(q);
  } else {
    (u.uUp.value as THREE.Vector3).set(0, 0, 1);
    (u.uFront.value as THREE.Vector3).set(0, -1, 0);
    (u.uRight.value as THREE.Vector3).set(1, 0, 0);
  }
}

/** 表示の設定を材質に入れる。 */
export function applySurfaceDisplay(material: THREE.ShaderMaterial, display: SurfaceDisplay): void {
  const u = material.uniforms;
  if (display.mode === 'zebra') {
    const z = display.zebra;
    u.uMode.value = 1;
    u.uLight.value = z.light === 'dome' ? 0 : z.light === 'horizontal' ? 1 : 2;
    u.uStripes.value = Math.max(1, z.stripes);
    u.uWidth.value = Math.max(0.05, Math.min(0.95, z.width));
    u.uAngle.value = (z.angleDeg * Math.PI) / 180;
  } else if (display.mode === 'scalar') {
    const s = display.scalar;
    u.uMode.value = 2;
    u.uScheme.value = s.scheme === 'deviation' ? 1 : 0;
    u.uMin.value = s.min;
    u.uMax.value = s.max;
    u.uTol.value = Math.max(0, s.tolerance);
  }
}
