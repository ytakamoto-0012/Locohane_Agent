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
import {
  decodeFloat32,
  decodeUint32,
  decodeUint8,
  type BoxMode,
  type CadColorOverrides,
  type CadData,
  type CadKind,
  type CadSelection,
  type CadVisibility
} from './cadData';

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
  /** 範囲選択で囲んだ要素。remove は Ctrl（⌘）を押しながら囲んだとき（選択から外す）。 */
  onBoxPick: (items: CadSelection[], remove: boolean) => void;
}

/** 視点。'xy+' は XY 平面を +Z 側から見る（X が右・Y が上）。'-' は裏側から見る（上の向きは同じ）。 */
export type ViewName = 'iso' | 'xy+' | 'yz+' | 'zx+' | 'xy-' | 'yz-' | 'zx-';

// 視点ごとの、中心からカメラへの向きと画面の上の向き。平面の 1 つめの軸が右、2 つめの軸が上になる。
const VIEW_DIRS: Record<ViewName, { dir: [number, number, number]; up: [number, number, number] }> = {
  iso: { dir: [1, -1, 0.8], up: [0, 0, 1] },
  'xy+': { dir: [0, 0, 1], up: [0, 1, 0] },
  'xy-': { dir: [0, 0, -1], up: [0, 1, 0] },
  'yz+': { dir: [1, 0, 0], up: [0, 0, 1] },
  'yz-': { dir: [-1, 0, 0], up: [0, 0, 1] },
  'zx+': { dir: [0, 1, 0], up: [1, 0, 0] },
  'zx-': { dir: [0, -1, 0], up: [1, 0, 0] }
};

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
  private meshPositions: Float32Array | null = null;
  // 元の三角形の番号。部品を隠すときは、その面の三角形を潰した（面積 0 の）番号に置き換える。
  private baseIndices: Uint32Array | null = null;
  private lines: THREE.LineSegments | null = null;
  private edgeHighlight: LineSegments2 | null = null;
  private edgePositions: Float32Array | null = null;
  private points: THREE.Points | null = null;
  private vertexPositions: Float32Array | null = null;
  private faceVertexRanges: Range[] = [];
  private faceTriStarts: number[] = [];
  private edgeSegStarts: number[] = [];
  private baseFaceColors: Float32Array | null = null;

  private mode: CadKind = 'face';
  private selection: CadSelection[] = [];
  private hover: CadSelection | null = null;
  // サイドバー（部品のツリー）でマウスを乗せている要素。
  private externalHover: CadSelection | null = null;
  private visibility: CadVisibility = { face: true, edge: true, vertex: true, hiddenParts: new Set() };
  private colorOverrides: CadColorOverrides = { faces: new Map(), parts: new Map() };
  private boxMode: BoxMode = 'off';
  private readonly boxEl: HTMLDivElement;
  private downAt: { x: number; y: number } | null = null;
  private boxing = false;
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
    this.boxEl = document.createElement('div');
    this.boxEl.className = 'cad-viewer-box';
    this.boxEl.hidden = true;
    container.appendChild(this.boxEl);

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
    this.meshPositions = positions;
    this.baseIndices = indices;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setIndex(new THREE.BufferAttribute(indices.slice(), 1));
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
    // 描く位置は写しを使う（隠す部品のエッジは線分を 1 点に潰す）。元の位置は当たり判定と範囲選択に使う。
    geometry.setAttribute('position', new THREE.BufferAttribute(positions.slice(), 3));
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
    this.vertexPositions = positions;
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

  /** サイドバーの部品にマウスを乗せたとき、その部品を浮かせて見せる（null で戻す）。 */
  setExternalHover(item: CadSelection | null): void {
    this.externalHover = item;
    this.refreshColors();
  }

  /** 種類ごとの表示と、隠す部品を変える。隠したものは当たり判定・範囲選択の対象にもしない。 */
  setVisibility(visibility: CadVisibility): void {
    this.visibility = visibility;
    if (this.mesh) {
      this.mesh.visible = visibility.face;
      const index = this.mesh.geometry.getIndex();
      if (index && this.baseIndices) {
        const arr = index.array as Uint32Array;
        arr.set(this.baseIndices);
        this.data.mesh.faceRanges.forEach(([s, c], id) => {
          if (c && this.partHidden(this.data.faces[id]?.part)) arr.fill(this.baseIndices![s * 3], s * 3, (s + c) * 3);
        });
        index.needsUpdate = true;
      }
    }
    if (this.lines && this.edgePositions) {
      this.lines.visible = visibility.edge;
      const attr = this.lines.geometry.getAttribute('position') as THREE.BufferAttribute;
      const arr = attr.array as Float32Array;
      arr.set(this.edgePositions);
      this.data.edges.ranges.forEach(([s, n], id) => {
        if (!n || !this.partHidden(this.data.edgeList[id]?.part)) return;
        for (let v = s * 2; v < (s + n) * 2; v++) arr.copyWithin(v * 3, s * 6, s * 6 + 3);
      });
      attr.needsUpdate = true;
      this.lines.geometry.computeBoundingSphere();
    }
    if (this.points) this.points.visible = visibility.vertex;
    this.hover = null;
    this.refreshColors();
  }

  /** 範囲選択のしかたを変える。'off' 以外では左ドラッグが回転でなく範囲選択になる。 */
  setBoxMode(mode: BoxMode): void {
    this.boxMode = mode;
    // 左ボタンを OrbitControls に渡さない（Ctrl + 左ドラッグの移動も含めて）。右ドラッグの移動・ホイールの拡大縮小はそのまま。
    (this.controls.mouseButtons as { LEFT: THREE.MOUSE | null }).LEFT = mode === 'off' ? THREE.MOUSE.ROTATE : null;
    this.boxEl.classList.toggle('crossing', mode === 'crossing');
  }

  setView(view: ViewName): void {
    const { dir: d, up } = VIEW_DIRS[view];
    const dir = new THREE.Vector3(...d).normalize();
    this.setUp(new THREE.Vector3(...up));
    this.camera.position.copy(this.center).addScaledVector(dir, this.fitDistance(dir));
    this.controls.target.copy(this.center);
    this.camera.lookAt(this.center);
    this.controls.update();
    this.requestRender();
  }

  /**
   * 画面の上の向きを変える。OrbitControls は作られたときの camera.up を回転の軸として持ち続ける
   * （内部の _quat）ので、合わせて直す。直さないと、上の向きを変えた視点で回転が傾く。
   */
  private setUp(up: THREE.Vector3): void {
    this.camera.up.copy(up);
    const c = this.controls as unknown as { _quat?: THREE.Quaternion; _quatInverse?: THREE.Quaternion };
    if (c._quat && c._quatInverse) {
      c._quat.setFromUnitVectors(up, new THREE.Vector3(0, 1, 0));
      c._quatInverse.copy(c._quat).invert();
    }
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
    this.boxEl.remove();
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
    if (hit(this.hover) || hit(this.externalHover)) return 'hover';
    if (this.selection.some((s) => this.covers(s, kind, id))) return 'selected';
    return null;
  }

  /** 選択 s が要素 (kind, id) を含むか（部品は、その部品の面・エッジを含む）。 */
  private covers(s: CadSelection, kind: CadKind, id: number): boolean {
    if (s.kind === kind) return s.id === id;
    if (s.kind === 'part') {
      const items = kind === 'face' ? this.data.faces : kind === 'edge' ? this.data.edgeList : kind === 'vertex' ? this.data.vertexList : null;
      return !!items && items[id]?.part === s.id;
    }
    return false;
  }

  /** 今の種類で選べる、見えている要素の ID（全選択に使う）。 */
  selectableIds(kind: CadKind): number[] {
    const n =
      kind === 'face'
        ? this.data.faces.length
        : kind === 'edge'
          ? this.data.edgeList.length
          : kind === 'vertex'
            ? this.data.vertexList.length
            : (this.data.parts ?? []).length;
    const ids: number[] = [];
    for (let id = 0; id < n; id++) {
      if (kind === 'face' && !(this.data.mesh.faceRanges[id]?.[1] ?? 0)) continue;
      if (this.visible(kind, id)) ids.push(id);
    }
    return ids;
  }

  /** 利用者が変えた色（面ごと・部品ごと）。部品の色はその部品の面（DXF のレイヤーは線も）に付く。 */
  setColorOverrides(overrides: CadColorOverrides): void {
    this.colorOverrides = overrides;
    this.refreshColors();
  }

  private partHidden(part: number | undefined): boolean {
    return part !== undefined && this.visibility.hiddenParts.has(part);
  }

  /** 要素が今見えているか（種類の表示と、部品を隠す設定）。 */
  private visible(kind: CadKind, id: number): boolean {
    const v = this.visibility;
    switch (kind) {
      case 'face':
        return v.face && !this.partHidden(this.data.faces[id]?.part);
      case 'edge':
        return v.edge && !this.partHidden(this.data.edgeList[id]?.part);
      case 'vertex': {
        if (!v.vertex) return false;
        const info = this.data.vertexList[id];
        if (info?.part !== undefined) return !this.partHidden(info.part);
        // 部品の番号を持たない（前の版の）データは、つながるエッジがすべて隠れていれば隠す。
        const edges = info?.edges ?? [];
        return !edges.length || edges.some((e) => !this.partHidden(this.data.edgeList[e]?.part));
      }
      case 'part':
        return !this.partHidden(id);
    }
  }

  private refreshColors(): void {
    const face = new THREE.Color(this.colors.face);
    const edge = new THREE.Color(this.colors.edge);
    const vertex = new THREE.Color(this.colors.vertex);
    const hover = new THREE.Color(this.colors.hover);
    const selected = new THREE.Color(this.colors.selected);
    const pick = (state: 'hover' | 'selected' | null, base: THREE.Color) => (state === 'hover' ? hover : state === 'selected' ? selected : base);

    const overrides = this.colorOverrides;
    const colorCache = new Map<string, THREE.Color>();
    const colorOf = (hex: string) => {
      let c = colorCache.get(hex);
      if (!c) colorCache.set(hex, (c = new THREE.Color(hex)));
      return c;
    };
    const parts = this.data.parts ?? [];

    if (this.mesh) {
      const attr = this.mesh.geometry.getAttribute('color') as THREE.BufferAttribute;
      const arr = attr.array as Float32Array;
      this.faceVertexRanges.forEach(([lo, hi], id) => {
        const state = this.stateOf('face', id);
        const part = this.data.faces[id]?.part;
        const changed = overrides.faces.get(id) ?? (part !== undefined ? overrides.parts.get(part) : undefined);
        // ファイルの色（頂点色）は、色を持つ面だけに使う（色の無い面はテーマの既定色）。
        if (state === null && !changed && this.baseFaceColors && this.data.faces[id]?.color) {
          arr.set(this.baseFaceColors.subarray(lo * 3, hi * 3), lo * 3);
          return;
        }
        const c = pick(state, changed ? colorOf(changed) : face);
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
        // DXF のレイヤー（部品）の線は、レイヤーの色（変えたらその色）で描く。
        const pi = this.data.edgeList[id]?.part;
        const layerPart = pi !== undefined && parts[pi]?.layer ? parts[pi] : undefined;
        const layerColor = layerPart ? (overrides.parts.get(pi!) ?? layerPart.color ?? null) : null;
        const c = pick(state, layerColor ? colorOf(layerColor) : edge);
        for (let v = s * 2; v < (s + n) * 2; v++) {
          arr[v * 3] = c.r;
          arr[v * 3 + 1] = c.g;
          arr[v * 3 + 2] = c.b;
        }
        // 選択・ホバー中のエッジ（部品の選択に含まれるものも）は太線でも重ねる（隠しているものは除く）。
        if (state !== null && this.visible('edge', id)) {
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
        const visible = (this.mode === 'vertex' || info.free || state !== null) && this.visible('vertex', id);
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
    const meshHit =
      this.mesh && this.visibility.face ? this.raycaster.intersectObject(this.mesh, false).find((h) => h.faceIndex != null) : undefined;
    const meshDistance = meshHit?.distance ?? Infinity;
    const scale = this.worldPerPixel(this.camera.position.distanceTo(this.controls.target));
    // 面の手前にあるもの（同じ面上の線・点を含む）だけを当てる。
    const inFront = (d: number) => d <= meshDistance + this.radius * 0.004;
    const edgeAt = (): number | null => {
      if (!this.lines || !this.visibility.edge) return null;
      this.raycaster.params.Line = { threshold: scale * PICK_PIXELS_LINE };
      for (const h of this.raycaster.intersectObject(this.lines, false)) {
        if (!inFront(h.distance)) break;
        if (h.index === undefined) continue;
        const id = rangeIndex(this.edgeSegStarts, Math.floor(h.index / 2));
        if (this.visible('edge', id)) return id;
      }
      return null;
    };
    const vertexAt = (): number | null => {
      if (!this.points || !this.visibility.vertex) return null;
      this.raycaster.params.Points = { threshold: scale * PICK_PIXELS_POINT };
      for (const h of this.raycaster.intersectObject(this.points, false)) {
        if (!inFront(h.distance)) break;
        if (h.index !== undefined && this.visible('vertex', h.index)) return h.index;
      }
      return null;
    };

    if (this.mode === 'edge') {
      const id = edgeAt();
      return id === null ? null : { kind: 'edge', id };
    }
    if (this.mode === 'vertex') {
      const id = vertexAt();
      return id === null ? null : { kind: 'vertex', id };
    }
    const faceId = meshHit?.faceIndex != null ? rangeIndex(this.faceTriStarts, meshHit.faceIndex) : null;
    if (this.mode === 'part') {
      // 面が無い部品（DXF の線だけのレイヤー等）も選べるよう、面に当たらなければエッジ・点で探す。
      let part = faceId !== null ? this.data.faces[faceId]?.part : undefined;
      if (part === undefined) {
        const edge = edgeAt();
        part = edge !== null ? this.data.edgeList[edge]?.part : undefined;
      }
      if (part === undefined) {
        const vertex = vertexAt();
        part = vertex !== null ? this.data.vertexList[vertex]?.part : undefined;
      }
      return part === undefined ? null : { kind: 'part', id: part };
    }
    return this.mode === 'face' && faceId !== null ? { kind: 'face', id: faceId } : null;
  }

  // ---------------------------------------------------------------------------
  // 範囲選択
  // ---------------------------------------------------------------------------

  /** 点の並び（xyz の繰り返し）を画面上の位置（canvas 内のピクセル）にする。カメラの後ろの点は NaN。 */
  private project(positions: Float32Array): Float32Array {
    const m = new THREE.Matrix4().multiplyMatrices(this.camera.projectionMatrix, this.camera.matrixWorldInverse).elements;
    const w = Math.max(this.container.clientWidth, 1);
    const h = Math.max(this.container.clientHeight, 1);
    const n = positions.length / 3;
    const out = new Float32Array(n * 2);
    for (let i = 0; i < n; i++) {
      const x = positions[i * 3];
      const y = positions[i * 3 + 1];
      const z = positions[i * 3 + 2];
      const cw = m[3] * x + m[7] * y + m[11] * z + m[15];
      if (cw <= 1e-9) {
        out[i * 2] = out[i * 2 + 1] = NaN;
        continue;
      }
      out[i * 2] = (((m[0] * x + m[4] * y + m[8] * z + m[12]) / cw + 1) / 2) * w;
      out[i * 2 + 1] = ((1 - (m[1] * x + m[5] * y + m[9] * z + m[13]) / cw) / 2) * h;
    }
    return out;
  }

  /**
   * 枠 r（canvas 内のピクセル）で囲んだ、今の種類の要素。奥に隠れているものも含む（見えている形の裏側も選べる）。
   * window は要素の点がすべて枠の中、crossing は枠の中に点があるか、線・三角形が枠に掛かるもの。
   */
  private boxPick(r: Rect, crossing: boolean): CadSelection[] {
    const inside = (p: Float32Array, i: number) => {
      const x = p[i * 2];
      const y = p[i * 2 + 1];
      return x >= r.x0 && x <= r.x1 && y >= r.y0 && y <= r.y1;
    };
    // 0 = 掛からない、1 = 掛かる（一部が中）、2 = すべて中。
    const faceHits = (): Uint8Array => {
      const out = new Uint8Array(this.data.faces.length);
      if (!this.meshPositions || !this.baseIndices) return out;
      const p = this.project(this.meshPositions);
      const idx = this.baseIndices;
      this.data.mesh.faceRanges.forEach(([s, c], id) => {
        if (!c || !this.visible('face', id)) return;
        const [lo, hi] = this.faceVertexRanges[id];
        let all = true;
        let any = false;
        for (let v = lo; v < hi; v++) {
          if (inside(p, v)) any = true;
          else all = false;
        }
        if (all) out[id] = 2;
        else if (any) out[id] = 1;
        else if (crossing) {
          for (let t = s; t < s + c; t++) {
            const a = idx[t * 3];
            const b = idx[t * 3 + 1];
            const d = idx[t * 3 + 2];
            if (triangleCrossesRect(p[a * 2], p[a * 2 + 1], p[b * 2], p[b * 2 + 1], p[d * 2], p[d * 2 + 1], r)) {
              out[id] = 1;
              break;
            }
          }
        }
      });
      return out;
    };
    const edgeHits = (): Uint8Array => {
      const out = new Uint8Array(this.data.edgeList.length);
      if (!this.edgePositions) return out;
      const p = this.project(this.edgePositions);
      this.data.edges.ranges.forEach(([s, n], id) => {
        if (!n || !this.visible('edge', id)) return;
        let all = true;
        let any = false;
        for (let v = s * 2; v < (s + n) * 2; v++) {
          if (inside(p, v)) any = true;
          else all = false;
        }
        if (all) out[id] = 2;
        else if (any) out[id] = 1;
        else if (crossing) {
          for (let v = s * 2; v < (s + n) * 2; v += 2) {
            if (segmentCrossesRect(p[v * 2], p[v * 2 + 1], p[v * 2 + 2], p[v * 2 + 3], r)) {
              out[id] = 1;
              break;
            }
          }
        }
      });
      return out;
    };
    const vertexHits = (): Uint8Array => {
      const out = new Uint8Array(this.data.vertexList.length);
      if (!this.vertexPositions) return out;
      const p = this.project(this.vertexPositions);
      this.data.vertexList.forEach((info, id) => {
        // 頂点を選ぶとき以外は、見えている単独の点だけを数える（形の角の頂点は描いていないため）。
        const shown = this.mode === 'vertex' || info.free;
        if (shown && this.visible('vertex', id) && inside(p, id)) out[id] = 2;
      });
      return out;
    };
    const ok = (hit: number) => (crossing ? hit > 0 : hit === 2);
    const pickIds = (hits: Uint8Array) => Array.from(hits.keys()).filter((i) => ok(hits[i]));

    switch (this.mode) {
      case 'face':
        return pickIds(faceHits()).map((id) => ({ kind: 'face' as const, id }));
      case 'edge':
        return pickIds(edgeHits()).map((id) => ({ kind: 'edge' as const, id }));
      case 'vertex':
        return pickIds(vertexHits()).map((id) => ({ kind: 'vertex' as const, id }));
      case 'part': {
        // 部品は、見えている面・エッジ・単独の点をまとめて判定する（window はすべて中、crossing はどれか 1 つ）。
        const parts = this.data.parts ?? [];
        const all = new Array<boolean>(parts.length).fill(true);
        const any = new Array<boolean>(parts.length).fill(false);
        const has = new Array<boolean>(parts.length).fill(false);
        const add = (items: { part?: number }[], hits: Uint8Array, counted: (id: number) => boolean) => {
          items.forEach((info, id) => {
            const pi = info.part;
            if (pi === undefined || pi >= parts.length || this.partHidden(pi) || !counted(id)) return;
            has[pi] = true;
            if (ok(hits[id])) any[pi] = true;
            if (hits[id] !== 2) all[pi] = false;
          });
        };
        if (this.visibility.face) add(this.data.faces, faceHits(), (id) => (this.data.mesh.faceRanges[id]?.[1] ?? 0) > 0);
        if (this.visibility.edge) add(this.data.edgeList, edgeHits(), (id) => (this.data.edges.ranges[id]?.[1] ?? 0) > 0);
        if (this.visibility.vertex) add(this.data.vertexList, vertexHits(), (id) => !!this.data.vertexList[id]?.free);
        return parts
          .map((_, id) => id)
          .filter((id) => has[id] && (crossing ? any[id] : all[id]))
          .map((id) => ({ kind: 'part' as const, id }));
      }
    }
  }

  private localPoint(e: PointerEvent): { x: number; y: number } {
    const rect = this.renderer.domElement.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  private boxRect(e: PointerEvent): Rect | null {
    if (!this.downAt) return null;
    const a = this.localPoint(e);
    const rect = this.renderer.domElement.getBoundingClientRect();
    const b = { x: this.downAt.x - rect.left, y: this.downAt.y - rect.top };
    return { x0: Math.min(a.x, b.x), y0: Math.min(a.y, b.y), x1: Math.max(a.x, b.x), y1: Math.max(a.y, b.y) };
  }

  private readonly handlePointerDown = (e: PointerEvent) => {
    this.downAt = { x: e.clientX, y: e.clientY };
    this.boxing = this.boxMode !== 'off' && e.button === 0;
    if (this.boxing) this.renderer.domElement.setPointerCapture(e.pointerId);
  };

  private readonly handlePointerUp = (e: PointerEvent) => {
    const down = this.downAt;
    const boxing = this.boxing;
    const rect = boxing ? this.boxRect(e) : null;
    this.downAt = null;
    this.boxing = false;
    this.boxEl.hidden = true;
    if (!down || e.button !== 0) return;
    if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > CLICK_MOVE_PIXELS) {
      if (boxing && rect) this.callbacks.onBoxPick(this.boxPick(rect, this.boxMode === 'crossing'), e.ctrlKey || e.metaKey);
      return;
    }
    const item = this.pickAt(e.clientX, e.clientY);
    if (item) this.callbacks.onPick(item);
  };

  private readonly handlePointerMove = (e: PointerEvent) => {
    if (this.boxing) {
      const r = this.boxRect(e);
      if (r && Math.max(r.x1 - r.x0, r.y1 - r.y0) > CLICK_MOVE_PIXELS) {
        Object.assign(this.boxEl.style, { left: `${r.x0}px`, top: `${r.y0}px`, width: `${r.x1 - r.x0}px`, height: `${r.y1 - r.y0}px` });
        this.boxEl.hidden = false;
      }
      return;
    }
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

interface Rect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

function pointInRect(x: number, y: number, r: Rect): boolean {
  return x >= r.x0 && x <= r.x1 && y >= r.y0 && y <= r.y1;
}

/** 線分 ab と線分 cd が交わるか。 */
function segmentsCross(ax: number, ay: number, bx: number, by: number, cx: number, cy: number, dx: number, dy: number): boolean {
  const o = (px: number, py: number, qx: number, qy: number, rx: number, ry: number) => (qx - px) * (ry - py) - (qy - py) * (rx - px);
  const d1 = o(cx, cy, dx, dy, ax, ay);
  const d2 = o(cx, cy, dx, dy, bx, by);
  const d3 = o(ax, ay, bx, by, cx, cy);
  const d4 = o(ax, ay, bx, by, dx, dy);
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}

/** 画面上の線分が枠に掛かるか（カメラの後ろの点 = NaN を含む線分は掛からないとする）。 */
function segmentCrossesRect(ax: number, ay: number, bx: number, by: number, r: Rect): boolean {
  if (Number.isNaN(ax) || Number.isNaN(bx)) return false;
  if (pointInRect(ax, ay, r) || pointInRect(bx, by, r)) return true;
  return (
    segmentsCross(ax, ay, bx, by, r.x0, r.y0, r.x1, r.y0) ||
    segmentsCross(ax, ay, bx, by, r.x1, r.y0, r.x1, r.y1) ||
    segmentsCross(ax, ay, bx, by, r.x1, r.y1, r.x0, r.y1) ||
    segmentsCross(ax, ay, bx, by, r.x0, r.y1, r.x0, r.y0)
  );
}

/** 画面上の三角形が枠に掛かるか（辺が枠と交わるか、枠が三角形の中にある）。 */
function triangleCrossesRect(ax: number, ay: number, bx: number, by: number, cx: number, cy: number, r: Rect): boolean {
  if (Number.isNaN(ax) || Number.isNaN(bx) || Number.isNaN(cx)) return false;
  if (segmentCrossesRect(ax, ay, bx, by, r) || segmentCrossesRect(bx, by, cx, cy, r) || segmentCrossesRect(cx, cy, ax, ay, r)) return true;
  // 枠が三角形より小さく、三角形の中にすっぽり入っている場合（枠の中心で調べる）。
  const px = (r.x0 + r.x1) / 2;
  const py = (r.y0 + r.y1) / 2;
  const s1 = (bx - ax) * (py - ay) - (by - ay) * (px - ax);
  const s2 = (cx - bx) * (py - by) - (cy - by) * (px - bx);
  const s3 = (ax - cx) * (py - cy) - (ay - cy) * (px - cx);
  return (s1 >= 0 && s2 >= 0 && s3 >= 0) || (s1 <= 0 && s2 <= 0 && s3 <= 0);
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
