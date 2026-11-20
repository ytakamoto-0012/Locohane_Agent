/**
 * 3D ビューア（CadViewerBar）の three.js 部分。CadViewerBar から import() で遅延読み込みする
 * （three.js をチャット画面の本体バンドルに入れないため）。
 *
 * 面・エッジ・頂点はそれぞれ 1 つのジオメトリ（1 回の描画）にまとめ、ハイライトは頂点色の書き換えで行う
 * （面ごとに別のオブジェクトにすると、面の多い STL で描画回数が増えて重くなるため）。
 * 面 i の三角形は mesh の faceRanges[i]、エッジ j の線分は edges の ranges[j] の範囲に並ぶ
 * （src/cad/mesh_data.py）。描画は変化があったときだけ行う（常時のアニメーションループは回さない）。
 */
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js';
import { LineSegments2 } from 'three/examples/jsm/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/examples/jsm/lines/LineSegmentsGeometry.js';
import { decodeFloat32, decodeUint32, decodeUint8, type CadData, type CadKind, type CadSelection } from './cadData';

export interface CadSceneColors {
  background: string;
  face: string;
  edge: string;
  vertex: string;
  hover: string;
  selected: string;
}

export interface CadSceneCallbacks {
  onHover: (item: CadSelection | null) => void;
  onPick: (item: CadSelection) => void;
}

export type ViewName = 'iso' | 'front' | 'top' | 'right';

// 当たり判定の許容（画面上のピクセル）。
const PICK_PIXELS_LINE = 6;
const PICK_PIXELS_POINT = 9;
// クリックとドラッグ（回転）を分けるしきい値（ピクセル）。
const CLICK_MOVE_PIXELS = 4;
// 選択・ホバー中のエッジを重ねて描く太線の幅（ピクセル。WebGL の通常の線は 1px 固定のため）。
const HIGHLIGHT_LINE_WIDTH = 3.5;
// 視点を合わせるとき、形がちょうど画面に収まる距離に掛ける余白。
const FIT_MARGIN = 1.12;

type Range = [number, number];

export class CadScene {
  private readonly container: HTMLElement;
  private readonly data: CadData;
  private readonly callbacks: CadSceneCallbacks;
  private colors: CadSceneColors;
  private readonly renderer: THREE.WebGLRenderer;
  private readonly scene = new THREE.Scene();
  // 左下に出す XYZ 軸（本体のカメラの向きに合わせて回す）。
  private readonly gizmoScene = new THREE.Scene();
  private readonly gizmoCamera = new THREE.OrthographicCamera(-1.6, 1.6, 1.6, -1.6, 0.1, 10);
  private readonly camera: THREE.PerspectiveCamera;
  private readonly controls: OrbitControls;
  private readonly raycaster = new THREE.Raycaster();
  private readonly resizeObserver: ResizeObserver;
  private readonly center = new THREE.Vector3();
  private readonly radius: number;

  private mesh: THREE.Mesh | null = null;
  private lines: THREE.LineSegments | null = null;
  private edgeHighlight: LineSegments2 | null = null;
  private edgePositions: Float32Array | null = null;
  private points: THREE.Points | null = null;
  private faceVertexRanges: Range[] = [];
  private faceTriStarts: number[] = [];
  private edgeSegStarts: number[] = [];
  private baseFaceColors: Float32Array | null = null;

  private mode: CadKind = 'face';
  private selection: CadSelection[] = [];
  private hover: CadSelection | null = null;
  private downAt: { x: number; y: number } | null = null;
  private renderQueued = false;

  constructor(container: HTMLElement, data: CadData, callbacks: CadSceneCallbacks, colors: CadSceneColors) {
    this.container = container;
    this.data = data;
    this.callbacks = callbacks;
    this.colors = colors;

    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(window.devicePixelRatio || 1);
    this.renderer.domElement.className = 'cad-viewer-canvas';
    container.appendChild(this.renderer.domElement);

    const min = new THREE.Vector3(...data.bbox.min);
    const max = new THREE.Vector3(...data.bbox.max);
    this.center.addVectors(min, max).multiplyScalar(0.5);
    this.radius = Math.max(max.distanceTo(min) / 2, 1e-6);

    this.camera = new THREE.PerspectiveCamera(40, 1, this.radius / 1000, this.radius * 1000);
    this.scene.add(this.camera);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8a96, 1.6));
    const light = new THREE.DirectionalLight(0xffffff, 1.4);
    light.position.set(1, 1.5, 2);
    this.camera.add(light);

    this.buildMesh();
    this.buildLines();
    this.buildPoints();
    this.addAxes();

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.copy(this.center);
    this.controls.addEventListener('change', () => this.requestRender());
    this.resize();
    this.setView('iso');

    const el = this.renderer.domElement;
    el.addEventListener('pointerdown', this.handlePointerDown);
    el.addEventListener('pointerup', this.handlePointerUp);
    el.addEventListener('pointermove', this.handlePointerMove);
    el.addEventListener('pointerleave', this.handlePointerLeave);

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.applyTheme(colors);
    this.resize();
  }

  // ---------------------------------------------------------------------------
  // 組み立て
  // ---------------------------------------------------------------------------

  private buildMesh(): void {
    const positions = decodeFloat32(this.data.mesh.positions);
    const indices = decodeUint32(this.data.mesh.indices);
    if (!indices.length) return;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setIndex(new THREE.BufferAttribute(indices, 1));
    geometry.computeVertexNormals();

    // 面ごとの頂点の範囲（面の頂点は連続して並ぶ。src/cad/export_mesh.py・mesh_data.py）。
    this.faceTriStarts = this.data.mesh.faceRanges.map(([s]) => s);
    this.faceVertexRanges = this.data.mesh.faceRanges.map(([s, c]) => {
      if (c === 0) return [0, 0];
      let lo = Infinity;
      let hi = -Infinity;
      for (let k = s * 3; k < (s + c) * 3; k++) {
        const v = indices[k];
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
      return [lo, hi + 1];
    });

    const vertexCount = positions.length / 3;
    const colors = new Float32Array(vertexCount * 3);
    const fileColors = decodeUint8(this.data.mesh.colors);
    if (fileColors) {
      for (let i = 0; i < colors.length; i++) colors[i] = srgbToLinear(fileColors[i] / 255);
      this.baseFaceColors = colors.slice();
    }
    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));

    const material = new THREE.MeshStandardMaterial({
      vertexColors: true,
      roughness: 0.55,
      metalness: 0.05,
      side: THREE.DoubleSide,
      // エッジの線が面に埋もれないよう、面を少し奥へずらす。
      polygonOffset: true,
      polygonOffsetFactor: 1,
      polygonOffsetUnits: 1,
    });
    this.mesh = new THREE.Mesh(geometry, material);
    this.scene.add(this.mesh);
  }

  private buildLines(): void {
    const positions = decodeFloat32(this.data.edges.positions);
    if (!positions.length) return;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute('color', new THREE.BufferAttribute(new Float32Array(positions.length), 3));
    this.edgeSegStarts = this.data.edges.ranges.map(([s]) => s);
    this.edgePositions = positions;
    this.lines = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ vertexColors: true }));
    this.scene.add(this.lines);
    const material = new LineMaterial({ linewidth: HIGHLIGHT_LINE_WIDTH, vertexColors: true, depthTest: false });
    this.edgeHighlight = new LineSegments2(new LineSegmentsGeometry(), material);
    this.edgeHighlight.renderOrder = 3;
    this.edgeHighlight.visible = false;
    this.scene.add(this.edgeHighlight);
  }

  private buildPoints(): void {
    const positions = decodeFloat32(this.data.vertices.positions);
    if (!positions.length) return;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute('color', new THREE.BufferAttribute(new Float32Array((positions.length / 3) * 4), 4));
    const material = new THREE.PointsMaterial({ size: 8, sizeAttenuation: false, vertexColors: true, transparent: true, depthWrite: false });
    this.points = new THREE.Points(geometry, material);
    this.points.renderOrder = 1;
    this.scene.add(this.points);
  }

  /** 左下の XYZ 軸（X 赤・Y 緑・Z 青、ラベル付き）を作る。 */
  private addAxes(): void {
    this.gizmoScene.add(new THREE.AxesHelper(1));
    const labels: [string, string, THREE.Vector3][] = [
      ['X', '#e5484d', new THREE.Vector3(1.3, 0, 0)],
      ['Y', '#30a46c', new THREE.Vector3(0, 1.3, 0)],
      ['Z', '#3e63dd', new THREE.Vector3(0, 0, 1.3)],
    ];
    for (const [text, color, pos] of labels) {
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 64;
      const ctx = canvas.getContext('2d');
      if (!ctx) continue;
      ctx.fillStyle = color;
      ctx.font = 'bold 44px sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(text, 32, 34);
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(canvas), depthTest: false }));
      sprite.position.copy(pos);
      sprite.scale.set(0.55, 0.55, 1);
      this.gizmoScene.add(sprite);
    }
  }

  private renderGizmo(): void {
    const size = Math.round(Math.min(96, this.container.clientWidth / 4, this.container.clientHeight / 3));
    if (size < 32) return;
    // 本体のカメラと同じ向きで、原点を見る。
    const dir = new THREE.Vector3().subVectors(this.camera.position, this.controls.target).normalize();
    this.gizmoCamera.position.copy(dir).multiplyScalar(4);
    this.gizmoCamera.up.copy(this.camera.up);
    this.gizmoCamera.lookAt(0, 0, 0);
    this.renderer.autoClear = false;
    this.renderer.clearDepth();
    this.renderer.setViewport(4, 4, size, size);
    this.renderer.render(this.gizmoScene, this.gizmoCamera);
    this.renderer.setViewport(0, 0, this.container.clientWidth, this.container.clientHeight);
    this.renderer.autoClear = true;
  }

  // ---------------------------------------------------------------------------
  // 公開する操作
  // ---------------------------------------------------------------------------

  setMode(mode: CadKind): void {
    this.mode = mode;
    this.hover = null;
    this.refreshColors();
  }

  setSelection(selection: CadSelection[]): void {
    this.selection = selection;
    this.refreshColors();
  }

  applyTheme(colors: CadSceneColors): void {
    this.colors = colors;
    this.renderer.setClearColor(new THREE.Color(colors.background));
    this.refreshColors();
  }

  setView(view: ViewName): void {
    const dirs: Record<ViewName, [number, number, number]> = {
      iso: [1, -1, 0.8],
      front: [0, -1, 0],
      top: [0, 0, 1],
      right: [1, 0, 0],
    };
    const dir = new THREE.Vector3(...dirs[view]).normalize();
    this.camera.up.set(0, 0, 1);
    if (view === 'top') this.camera.up.set(0, 1, 0);
    this.camera.position.copy(this.center).addScaledVector(dir, this.fitDistance(dir));
    this.controls.target.copy(this.center);
    this.camera.lookAt(this.center);
    this.controls.update();
    this.requestRender();
  }

  /** dir の向きから中心を見たとき、バウンディングボックスの 8 つの角が画面に収まるカメラの距離。 */
  private fitDistance(dir: THREE.Vector3): number {
    const tanV = Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2));
    const tanH = tanV * this.camera.aspect;
    // カメラの右・上の向き（lookAt と同じ作り方）。
    const right = new THREE.Vector3().crossVectors(this.camera.up, dir);
    if (right.lengthSq() < 1e-12) right.set(1, 0, 0);
    right.normalize();
    const up = new THREE.Vector3().crossVectors(dir, right).normalize();
    const [x0, y0, z0] = this.data.bbox.min;
    const [x1, y1, z1] = this.data.bbox.max;
    let distance = 0;
    for (const x of [x0, x1])
      for (const y of [y0, y1])
        for (const z of [z0, z1]) {
          const p = new THREE.Vector3(x, y, z).sub(this.center);
          // 角がカメラ側へ出ている分（p・dir）だけ近くなるので、その分も足す。
          const toward = p.dot(dir);
          distance = Math.max(distance, toward + Math.abs(p.dot(right)) / tanH, toward + Math.abs(p.dot(up)) / tanV);
        }
    return Math.max(distance, this.radius * 0.1) * FIT_MARGIN;
  }

  resize(): void {
    const w = Math.max(this.container.clientWidth, 1);
    const h = Math.max(this.container.clientHeight, 1);
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    (this.edgeHighlight?.material as LineMaterial | undefined)?.resolution.set(w, h);
    this.requestRender();
  }

  dispose(): void {
    this.resizeObserver.disconnect();
    this.controls.dispose();
    const el = this.renderer.domElement;
    el.removeEventListener('pointerdown', this.handlePointerDown);
    el.removeEventListener('pointerup', this.handlePointerUp);
    el.removeEventListener('pointermove', this.handlePointerMove);
    el.removeEventListener('pointerleave', this.handlePointerLeave);
    const disposeObject = (obj: THREE.Object3D) => {
      const o = obj as THREE.Mesh;
      o.geometry?.dispose();
      const m = o.material as THREE.Material | THREE.Material[] | undefined;
      for (const x of Array.isArray(m) ? m : m ? [m] : []) {
        (x as THREE.SpriteMaterial).map?.dispose();
        x.dispose();
      }
    };
    this.scene.traverse(disposeObject);
    this.gizmoScene.traverse(disposeObject);
    this.renderer.dispose();
    el.remove();
  }

  // ---------------------------------------------------------------------------
  // 色（ハイライト）
  // ---------------------------------------------------------------------------

  /** 選択・ホバーの状態から、その要素が今どの色か。 */
  private stateOf(kind: CadKind, id: number): 'hover' | 'selected' | null {
    const hit = (s: CadSelection | null) => !!s && this.covers(s, kind, id);
    if (hit(this.hover)) return 'hover';
    if (this.selection.some((s) => this.covers(s, kind, id))) return 'selected';
    return null;
  }

  /** 選択 s が要素 (kind, id) を含むか（部品は、その部品の面・エッジを含む）。 */
  private covers(s: CadSelection, kind: CadKind, id: number): boolean {
    if (s.kind === kind) return s.id === id;
    if (s.kind === 'part') {
      const items = kind === 'face' ? this.data.faces : kind === 'edge' ? this.data.edgeList : null;
      return !!items && items[id]?.part === s.id;
    }
    return false;
  }

  private refreshColors(): void {
    const face = new THREE.Color(this.colors.face);
    const edge = new THREE.Color(this.colors.edge);
    const vertex = new THREE.Color(this.colors.vertex);
    const hover = new THREE.Color(this.colors.hover);
    const selected = new THREE.Color(this.colors.selected);
    const pick = (state: 'hover' | 'selected' | null, base: THREE.Color) => (state === 'hover' ? hover : state === 'selected' ? selected : base);

    if (this.mesh) {
      const attr = this.mesh.geometry.getAttribute('color') as THREE.BufferAttribute;
      const arr = attr.array as Float32Array;
      this.faceVertexRanges.forEach(([lo, hi], id) => {
        const state = this.stateOf('face', id);
        if (state === null && this.baseFaceColors) {
          arr.set(this.baseFaceColors.subarray(lo * 3, hi * 3), lo * 3);
          return;
        }
        const c = pick(state, face);
        for (let v = lo; v < hi; v++) {
          arr[v * 3] = c.r;
          arr[v * 3 + 1] = c.g;
          arr[v * 3 + 2] = c.b;
        }
      });
      attr.needsUpdate = true;
    }
    if (this.lines && this.edgePositions) {
      const attr = this.lines.geometry.getAttribute('color') as THREE.BufferAttribute;
      const arr = attr.array as Float32Array;
      const hiPos: number[] = [];
      const hiCol: number[] = [];
      this.data.edges.ranges.forEach(([s, n], id) => {
        const state = this.stateOf('edge', id);
        const c = pick(state, edge);
        for (let v = s * 2; v < (s + n) * 2; v++) {
          arr[v * 3] = c.r;
          arr[v * 3 + 1] = c.g;
          arr[v * 3 + 2] = c.b;
        }
        // 選択・ホバー中のエッジ（部品の選択に含まれるものも）は太線でも重ねる。
        if (state !== null) {
          for (let v = s * 2; v < (s + n) * 2; v++) {
            hiPos.push(this.edgePositions![v * 3], this.edgePositions![v * 3 + 1], this.edgePositions![v * 3 + 2]);
            hiCol.push(c.r, c.g, c.b);
          }
        }
      });
      attr.needsUpdate = true;
      if (this.edgeHighlight) {
        this.edgeHighlight.visible = hiPos.length > 0;
        if (hiPos.length) {
          this.edgeHighlight.geometry.dispose();
          const g = new LineSegmentsGeometry();
          g.setPositions(hiPos);
          g.setColors(hiCol);
          this.edgeHighlight.geometry = g;
        }
      }
    }
    if (this.points) {
      const attr = this.points.geometry.getAttribute('color') as THREE.BufferAttribute;
      const arr = attr.array as Float32Array;
      this.data.vertexList.forEach((info, id) => {
        const state = this.stateOf('vertex', id);
        const c = pick(state, vertex);
        // 頂点を選ぶとき以外は、単独の点（形の面・エッジに属さない点）と選択中の頂点だけを見せる。
        const visible = this.mode === 'vertex' || info.free || state !== null;
        arr[id * 4] = c.r;
        arr[id * 4 + 1] = c.g;
        arr[id * 4 + 2] = c.b;
        arr[id * 4 + 3] = visible ? 1 : 0;
      });
      attr.needsUpdate = true;
    }
    this.requestRender();
  }

  // ---------------------------------------------------------------------------
  // 当たり判定
  // ---------------------------------------------------------------------------

  private worldPerPixel(distance: number): number {
    const h = Math.max(this.container.clientHeight, 1);
    return (2 * distance * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2))) / h;
  }

  private pickAt(clientX: number, clientY: number): CadSelection | null {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const meshHit = this.mesh ? this.raycaster.intersectObject(this.mesh, false)[0] : undefined;
    const meshDistance = meshHit?.distance ?? Infinity;
    const scale = this.worldPerPixel(this.camera.position.distanceTo(this.controls.target));
    // 面の手前にあるもの（同じ面上の線・点を含む）だけを当てる。
    const inFront = (d: number) => d <= meshDistance + this.radius * 0.004;

    if (this.mode === 'edge' && this.lines) {
      this.raycaster.params.Line = { threshold: scale * PICK_PIXELS_LINE };
      const hit = this.raycaster.intersectObject(this.lines, false).find((h) => inFront(h.distance));
      if (hit?.index !== undefined) return { kind: 'edge', id: rangeIndex(this.edgeSegStarts, Math.floor(hit.index / 2)) };
      return null;
    }
    if (this.mode === 'vertex' && this.points) {
      this.raycaster.params.Points = { threshold: scale * PICK_PIXELS_POINT };
      const hit = this.raycaster.intersectObject(this.points, false).find((h) => inFront(h.distance));
      if (hit?.index !== undefined) return { kind: 'vertex', id: hit.index };
      return null;
    }
    if (!meshHit || meshHit.faceIndex === undefined || meshHit.faceIndex === null) return null;
    const faceId = rangeIndex(this.faceTriStarts, meshHit.faceIndex);
    if (this.mode === 'part') {
      const part = this.data.faces[faceId]?.part;
      return part === undefined ? null : { kind: 'part', id: part };
    }
    return this.mode === 'face' ? { kind: 'face', id: faceId } : null;
  }

  private readonly handlePointerDown = (e: PointerEvent) => {
    this.downAt = { x: e.clientX, y: e.clientY };
  };

  private readonly handlePointerUp = (e: PointerEvent) => {
    const down = this.downAt;
    this.downAt = null;
    if (!down || e.button !== 0) return;
    if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > CLICK_MOVE_PIXELS) return;
    const item = this.pickAt(e.clientX, e.clientY);
    if (item) this.callbacks.onPick(item);
  };

  private readonly handlePointerMove = (e: PointerEvent) => {
    // ドラッグ（回転・移動）中は当たり判定をしない（重いモデルで操作が引っかかるため）。
    if (e.buttons !== 0) return;
    const item = this.pickAt(e.clientX, e.clientY);
    const same = item && this.hover ? item.kind === this.hover.kind && item.id === this.hover.id : item === this.hover;
    if (same) return;
    this.hover = item;
    this.callbacks.onHover(item);
    this.refreshColors();
  };

  private readonly handlePointerLeave = () => {
    if (!this.hover) return;
    this.hover = null;
    this.callbacks.onHover(null);
    this.refreshColors();
  };

  private requestRender(): void {
    if (this.renderQueued) return;
    this.renderQueued = true;
    requestAnimationFrame(() => {
      this.renderQueued = false;
      this.renderer.render(this.scene, this.camera);
      this.renderGizmo();
    });
  }
}

/** starts（昇順）のうち value 以下で最大のものの位置（範囲の番号）。空の範囲（同じ starts が続く）は後ろを選ぶ。 */
function rangeIndex(starts: number[], value: number): number {
  let lo = 0;
  let hi = starts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (starts[mid] <= value) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

function srgbToLinear(c: number): number {
  return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}
