import { useCallback, useContext, useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react';
import { useRecoilValue } from 'recoil';
import { ChainlitContext, sessionIdState, useChatData, type IAction } from '@chainlit/react-client';
import { useI18n } from '../i18n';
import type { MessageKey } from '../i18n/messages';
import {
  CAD_DATA_FORMAT,
  attributeColor,
  effectiveColor,
  itemsOf,
  partOf,
  pmiById,
  rgbText,
  sameSelection,
  type BoxMode,
  type CadColorOverrides,
  type CadData,
  type CadItemInfo,
  type CadKind,
  type CadSelection,
  type CadViewerProps
} from './cad/cadData';
import type { CadScene, CadSceneColors, ViewName } from './cad/CadScene';
import {
  MEASURE_TOOLS,
  PICKS_NEEDED,
  fmtNum,
  isPlane,
  measure,
  type CadMeasurement,
  type MeasureError,
  type MeasurePick,
  type MeasureResult,
  type MeasureTool
} from './cad/measure';
import { CadSidebar } from './cad/CadSidebar';
import type { CadAttributeEdits } from './cad/CadStepPanel';

const KIND_LABEL: Record<CadKind, MessageKey> = {
  face: 'cad.kind.face',
  edge: 'cad.kind.edge',
  vertex: 'cad.kind.vertex',
  part: 'cad.kind.part'
};
// 平面の 1 つめの軸が右・2 つめの軸が上。'+' は平面の法線（1 つめ × 2 つめ）の正の側から、'-' は裏側から見る。
const VIEWS: ViewName[] = ['xy+', 'yz+', 'zx+', 'xy-', 'yz-', 'zx-'];
const BOX_MODES: { id: BoxMode; label: MessageKey; title: MessageKey }[] = [
  { id: 'off', label: 'cad.box.off', title: 'cad.box.offTitle' },
  { id: 'window', label: 'cad.box.window', title: 'cad.box.windowTitle' },
  { id: 'crossing', label: 'cad.box.crossing', title: 'cad.box.crossingTitle' }
];

// 覚えておく計測の数の上限（古いものから消す。src/tools/cad_viewer.py の _MEASUREMENTS_MAX と同じ）。
const MEASUREMENTS_MAX = 50;

/** 計測の一覧に出す説明（ツールチップ。距離は各軸の成分も）。 */
function measureTitle(m: CadMeasurement, t: (k: MessageKey) => string): string {
  const parts = [`${t(`cad.measure.tool.${m.tool}` as MessageKey)}  ${m.draw.label}`];
  const delta = m.result.delta;
  if (Array.isArray(delta)) parts.push(['ΔX', 'ΔY', 'ΔZ'].map((k, i) => `${k} ${fmtNum(Number(delta[i]))}`).join('  '));
  if (m.approximate) parts.push(t('cad.measure.approx'));
  return parts.join('\n');
}

/** 確定時に返す、変えた色（キーは ID の文字列）。 */
export interface CadColorChanges {
  faces: Record<string, string>;
  parts: Record<string, string>;
}

/** 画面のテーマ（ライト / ダーク）から 3D 表示の色を決める。 */
function sceneColors(): CadSceneColors {
  const dark = document.documentElement.dataset.theme === 'dark';
  return {
    background: dark ? '#17171a' : '#f7f7f9',
    face: dark ? '#8d93a0' : '#b9bfcb',
    edge: dark ? '#e4e4ea' : '#2b2c31',
    vertex: dark ? '#f0f0f4' : '#1f2023',
    // 選択はオレンジで目立たせ、ホバーはそれと見分けられる水色にする。
    hover: '#1fb6d4',
    selected: SELECTED_COLOR
  };
}

/** 選んだ要素の色（3D 表示・選択中タグ・部品ツリーで共通。styles.css の --cad-selected と同じ値）。 */
const SELECTED_COLOR = '#ff7a00';

/** スマホなど幅の狭い画面か（styles.css の @media (max-width: 768px) と同じ境目）。 */
function isNarrowScreen(): boolean {
  return typeof window !== 'undefined' && window.matchMedia?.('(max-width: 768px)').matches === true;
}

function fmt(n: unknown): string {
  return typeof n === 'number' ? String(Math.round(n * 1000) / 1000) : '';
}

/** 面の属する穴・ボスと、付いた PMI の短い説明（例: 「穴 ⌀5 通し」「⌀10 +0.1/-0.05」）。 */
function extraInfo(item: CadItemInfo, data: CadData | undefined, t: (k: MessageKey) => string): string[] {
  if (!data) return [];
  const out: string[] = [];
  const feat = typeof item.feature === 'string' ? data.features?.find((f) => f.id === item.feature) : undefined;
  if (feat) {
    const kind = feat.type === 'hole' ? t('cad.info.feature') : t('cad.info.boss');
    const extra = feat.type === 'hole' ? ` ${feat.through ? t('cad.info.through') : `${t('cad.info.blind')} ${fmt(feat.depth)}`}` : '';
    out.push(`${kind} ⌀${fmt(feat.diameter)}${extra}`);
  }
  const ids = Array.isArray(item.pmi) ? (item.pmi as string[]) : [];
  if (ids.length) {
    const byId = pmiById(data);
    out.push(...ids.slice(0, 3).map((id) => byId.get(id)?.text ?? id));
  }
  return out;
}

/** ホバー中・選択中の要素の短い説明（例: 「面 #3 PLANE 面積 600 RGB(200, 0, 0) >Z」）。 */
function describe(item: CadItemInfo | undefined, label: string, color: string | null, t: (k: MessageKey) => string, data?: CadData): string {
  if (!item) return label;
  const parts = [label];
  if (item.kind === 'part') parts.push(item.path ?? item.name ?? '');
  else if (item.geomType) parts.push(item.geomType);
  if (item.area !== undefined) parts.push(`${t('cad.info.area')} ${fmt(item.area)}`);
  if (item.length !== undefined) parts.push(`${t('cad.info.length')} ${fmt(item.length)}`);
  if (item.radius !== undefined) parts.push(`R ${fmt(item.radius)}`);
  if (item.point) parts.push(`(${item.point.map(fmt).join(', ')})`);
  if (item.free) parts.push(t('cad.info.free'));
  if (color) parts.push(rgbText(color));
  const sel = item.selectors?.[0] ?? item.meshHints?.[0];
  if (sel) parts.push(sel);
  parts.push(...extraInfo(item, data, t));
  return parts.filter(Boolean).join('  ');
}

/**
 * show_cad_viewer（src/tools/cad_viewer.py）専用の表示コンポーネント。
 * backend では cl.AskElementMessage（spec.type === 'element'）として届く。name が "CadViewer" の
 * カスタム要素の props（question / select / data）から 3D ビューアを描き、確定時に askUser.callback へ
 * { submitted: true, selected: [{ kind, id }], colors: { faces, parts } }、閉じたときは { submitted: false } を返す。
 * AskFormBar（labels）・AskChoiceFormBar（choices）とは props の形で排他になる。
 */
export function CadViewerBar() {
  const { askUser, elements } = useChatData();
  const element =
    askUser?.spec.type === 'element'
      ? elements.find((e) => e.type === 'custom' && e.id === askUser.spec.element_id && e.name === 'CadViewer')
      : undefined;
  const props = element?.type === 'custom' ? (element.props as CadViewerProps) : undefined;
  if (!askUser || !element || !props?.data || props.data.format !== CAD_DATA_FORMAT) return null;
  return (
    <CadViewerPanel
      key={element.id}
      question={props.question ?? ''}
      kinds={props.select?.length ? props.select : ['face']}
      data={props.data}
      saveId={props.save_id ?? ''}
      screenshotLongSide={props.screenshot_long_side ?? 0}
      onSubmit={(selected, colors, message, view, screenshot, measurements) =>
        askUser.callback({
          submitted: true,
          selected,
          colors,
          message,
          ...view,
          ...(screenshot ? { screenshot } : {}),
          ...(measurements.length ? { measurements } : {})
        })
      }
      onCancel={(colors, view) => askUser.callback({ submitted: false, selected: [], colors, ...view })}
    />
  );
}

interface PanelProps {
  question: string;
  kinds: CadKind[];
  data: CadData;
  saveId: string;
  screenshotLongSide: number;
  /** screenshot: 指示があるときだけ撮った今の画面（PNG の data URL）。measurements: ビューアで測った結果。 */
  onSubmit: (
    selected: CadSelection[],
    colors: CadColorChanges,
    message: string,
    view: CadViewState,
    screenshot: string | null,
    measurements: MeasureResult[]
  ) => void;
  onCancel: (colors: CadColorChanges, view: CadViewState) => void;
}

/** 確定・閉じる・保存のときに返す表示の状態（backend は変更があればビューア用データに反映して自動で保存する）。 */
interface CadViewState {
  hidden_parts: number[];
  visible: { face: boolean; edge: boolean; vertex: boolean };
  /** STEP の属性を編集した製品（製品名 → 編集後の一覧）。編集が無ければ入れない。 */
  attributes?: CadAttributeEdits;
}

function initialHiddenParts(data: CadData): Set<number> {
  // 「保存」したデータは保存したときの表示に戻す。それ以外は、DXF で非表示・フリーズのレイヤーを
  // 最初から隠す（CAD で開いたときと同じ見え方）。
  if (data.viewState?.hiddenParts) return new Set(data.viewState.hiddenParts);
  return new Set((data.parts ?? []).filter((p) => p.layer && (p.layer.off || p.layer.frozen)).map((p) => p.id));
}

/** 元に戻す・やり直すの対象（選択・変えた色・隠した部品・種類ごとの表示・属性の編集）。カメラの向きは含めない。 */
interface EditSnapshot {
  selection: CadSelection[];
  colors: CadColorOverrides;
  hiddenParts: Set<number>;
  kindVisible: { face: boolean; edge: boolean; vertex: boolean };
  attributeEdits: CadAttributeEdits;
}

// 覚えておく変更の数の上限（古いものから捨てる）。
const HISTORY_LIMIT = 200;
// 属性の入力欄を続けて打った変更は、この時間（ミリ秒）内なら 1 回の変更にまとめる（1 文字ずつ戻らないように）。
const HISTORY_MERGE_MS = 1000;

function changedKeys(a: EditSnapshot, b: EditSnapshot): string {
  return (Object.keys(a) as (keyof EditSnapshot)[]).filter((k) => a[k] !== b[k]).join(',');
}

/**
 * 編集の履歴。state が変わるたびに変わる前の状態を積み、undo / redo で apply に渡して戻す。
 * 各 state は変更のたびに新しいオブジェクトになる（React の state の作法）ので、参照の違いで変更を見分ける。
 */
function useEditHistory(state: EditSnapshot, apply: (s: EditSnapshot) => void) {
  const past = useRef<EditSnapshot[]>([]);
  const future = useRef<EditSnapshot[]>([]);
  const last = useRef<EditSnapshot>(state);
  const lastChange = useRef({ keys: '', at: 0 });
  const applyRef = useRef(apply);
  applyRef.current = apply;
  const [, setVersion] = useState(0);
  const { selection, colors, hiddenParts, kindVisible, attributeEdits } = state;

  useEffect(() => {
    const cur: EditSnapshot = { selection, colors, hiddenParts, kindVisible, attributeEdits };
    const keys = changedKeys(cur, last.current);
    // 変更なし、または undo / redo で戻した状態そのもの（last を先に書き換えてある）。
    if (!keys) return;
    const now = Date.now();
    const merge = keys === 'attributeEdits' && lastChange.current.keys === keys && now - lastChange.current.at < HISTORY_MERGE_MS;
    if (!merge) {
      past.current.push(last.current);
      if (past.current.length > HISTORY_LIMIT) past.current.shift();
    }
    future.current = [];
    last.current = cur;
    lastChange.current = { keys, at: now };
    setVersion((v) => v + 1);
  }, [selection, colors, hiddenParts, kindVisible, attributeEdits]);

  const step = useCallback((from: MutableRefObject<EditSnapshot[]>, to: MutableRefObject<EditSnapshot[]>) => {
    const target = from.current.pop();
    if (!target) return;
    to.current.push(last.current);
    last.current = target;
    lastChange.current = { keys: '', at: 0 };
    applyRef.current(target);
    setVersion((v) => v + 1);
  }, []);
  const undo = useCallback(() => step(past, future), [step]);
  const redo = useCallback(() => step(future, past), [step]);
  const canUndo = past.current.length > 0;
  const canRedo = future.current.length > 0;
  return useMemo(() => ({ undo, redo, canUndo, canRedo }), [undo, redo, canUndo, canRedo]);
}

type SaveState ={ kind: 'idle' } | { kind: 'saving' } | { kind: 'saved'; path: string } | { kind: 'error'; message: string };

function CadViewerPanel({ question, kinds, data, saveId, screenshotLongSide, onSubmit, onCancel }: PanelProps) {
  const { t } = useI18n();
  const client = useContext(ChainlitContext);
  const sessionId = useRecoilValue(sessionIdState);
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' });
  // 確定・キャンセルを押した後、backend がビューアを閉じるまでの間（二度押しを防ぎ、処理中と分かるようにする）。
  const [closing, setClosing] = useState<'submit' | 'cancel' | null>(null);
  // 選択と一緒に AI へ返す指示（「この面に φ5 の穴を開けて」等）。
  const [message, setMessage] = useState('');
  const hostRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<CadScene | null>(null);
  const [mode, setMode] = useState<CadKind>(kinds[0]);
  const [selection, setSelection] = useState<CadSelection[]>([]);
  const [hover, setHover] = useState<CadSelection | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [expanded, setExpanded] = useState(false);
  const [boxMode, setBoxMode] = useState<BoxMode>('off');
  // 「回転中心」: 次にクリックした点を回転の中心にする（その 1 回だけ）。
  const [pivotPicking, setPivotPicking] = useState(false);
  // 空間の原点 (0, 0, 0) の XYZ 軸の表示。
  const [originAxes, setOriginAxes] = useState(true);
  // 計測: 計測の行を開いているか、今の計測の種類（null なら計測しない = クリックで選択）、途中で拾った点、測った結果。
  const [measureOpen, setMeasureOpen] = useState(false);
  const [measureTool, setMeasureTool] = useState<MeasureTool | null>(null);
  const [pending, setPending] = useState<MeasurePick[]>([]);
  const [measurements, setMeasurements] = useState<CadMeasurement[]>([]);
  const [measureError, setMeasureError] = useState<MeasureError | null>(null);
  // CadScene のコールバックは作ったときのものが残るので、今の状態は ref で読む。
  const measureRef = useRef({ tool: measureTool, pending });
  measureRef.current = { tool: measureTool, pending };
  const hasParts = (data.parts?.length ?? 0) > 0;
  // 狭い画面ではサイドバーが 3D 表示の場所を取るため、最初は閉じておく（ボタンで開ける）。
  const [sidebarOpen, setSidebarOpen] = useState((hasParts || !!data.step) && !isNarrowScreen());
  // STEP の属性の編集（製品名 → 編集後の一覧）。
  const [attributeEdits, setAttributeEdits] = useState<CadAttributeEdits>({});
  // 前の版からの変化の重ね表示（compare_with で比べたときだけ）。
  const hasDiff = !!(data.diff?.added || data.diff?.removed);
  const [diffVisible, setDiffVisible] = useState(true);
  const [kindVisible, setKindVisible] = useState({ face: true, edge: true, vertex: true, ...data.viewState?.visible });
  const [hiddenParts, setHiddenParts] = useState<Set<number>>(() => initialHiddenParts(data));
  const [colors, setColors] = useState<CadColorOverrides>({ faces: new Map(), parts: new Map() });
  const history = useEditHistory(
    { selection, colors, hiddenParts, kindVisible, attributeEdits },
    (s) => {
      setSelection(s.selection);
      setColors(s.colors);
      setHiddenParts(s.hiddenParts);
      setKindVisible(s.kindVisible);
      setAttributeEdits(s.attributeEdits);
    }
  );
  const barRef = useRef<HTMLDivElement>(null);
  // Ctrl+Z / Ctrl+Y（Ctrl+Shift+Z）: ポインタがビューアの上にあるか、ビューアの中にフォーカスがあるときだけ
  // （チャットの入力欄など、ほかの場所の元に戻すを横取りしない）。文字の入力欄の中ではブラウザに任せる。
  const pointerInside = useRef(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      const key = e.key.toLowerCase();
      const isUndo = key === 'z' && !e.shiftKey;
      const isRedo = key === 'y' || (key === 'z' && e.shiftKey);
      if (!isUndo && !isRedo) return;
      const bar = barRef.current;
      const target = e.target as HTMLElement | null;
      if (!bar || !(pointerInside.current || (target && bar.contains(target)))) return;
      if (target && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))) return;
      e.preventDefault();
      if (isUndo) history.undo();
      else history.redo();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [history]);

  const label = useCallback(
    (s: CadSelection) => {
      const base = `${t(KIND_LABEL[s.kind])} #${s.id}`;
      const name = s.kind === 'part' ? data.parts?.[s.id]?.name : undefined;
      return name ? `${base} ${name}` : base;
    },
    [t, data]
  );
  const toggle = useCallback((item: CadSelection) => {
    setSelection((cur) => (cur.some((s) => sameSelection(s, item)) ? cur.filter((s) => !sameSelection(s, item)) : [...cur, item]));
  }, []);
  const boxPick = useCallback((items: CadSelection[], remove: boolean) => {
    setSelection((cur) => {
      if (remove) return cur.filter((s) => !items.some((i) => sameSelection(i, s)));
      const added = items.filter((i) => !cur.some((s) => sameSelection(s, i)));
      return added.length ? [...cur, ...added] : cur;
    });
  }, []);

  const infoOf = useCallback((s: CadSelection | null) => (s ? itemsOf(data, s.kind)[s.id] : undefined), [data]);
  // 計測のモードでクリックした点・要素。要る数だけ集まったら測る（平面どうしは平面以外をその場で断る）。
  const measurePick = useCallback(
    (pick: MeasurePick) => {
      const { tool, pending: cur } = measureRef.current;
      if (!tool) return;
      setMeasureError(null);
      if (tool === 'planes' && !isPlane(infoOf(pick.item))) {
        setMeasureError('notPlane');
        return;
      }
      const picks = [...cur, pick];
      if (picks.length < PICKS_NEEDED[tool]) {
        setPending(picks);
        return;
      }
      setPending([]);
      const out = measure(tool, picks, infoOf);
      if ('error' in out) setMeasureError(out.error);
      else setMeasurements((list) => [...list, out.ok].slice(-MEASUREMENTS_MAX));
    },
    [infoOf]
  );
  const chooseMeasureTool = (tool: MeasureTool | null) => {
    setMeasureTool(tool);
    setPending([]);
    setMeasureError(null);
    if (tool) {
      setBoxMode('off');
      setPivotPicking(false);
    }
  };

  // three.js は import() で必要になったときだけ読む。
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let disposed = false;
    import('./cad/CadScene')
      .then(({ CadScene }) => {
        if (disposed) return;
        sceneRef.current = new CadScene(
          host,
          data,
          { onHover: setHover, onPick: toggle, onBoxPick: boxPick, onPivotSet: () => setPivotPicking(false), onMeasurePick: measurePick },
          sceneColors()
        );
        sceneRef.current.setMode(kinds[0]);
        setStatus('ready');
      })
      .catch((err) => {
        console.error('Could not start the 3D viewer', err);
        if (!disposed) setStatus('error');
      });
    return () => {
      disposed = true;
      sceneRef.current?.dispose();
      sceneRef.current = null;
    };
    // data・kinds は要素ごとに固定（key={element.id} で作り直す）。
  }, [data, kinds, toggle, boxPick, measurePick]);

  useEffect(() => {
    sceneRef.current?.setMode(mode);
  }, [mode, status]);
  useEffect(() => {
    sceneRef.current?.setSelection(selection);
  }, [selection, status]);
  useEffect(() => {
    sceneRef.current?.setBoxMode(boxMode);
  }, [boxMode, status]);
  useEffect(() => {
    sceneRef.current?.setPivotPicking(pivotPicking);
  }, [pivotPicking, status]);
  useEffect(() => {
    sceneRef.current?.setOriginAxesVisible(originAxes);
  }, [originAxes, status]);
  useEffect(() => {
    sceneRef.current?.setVisibility({ ...kindVisible, hiddenParts });
  }, [kindVisible, hiddenParts, status]);
  useEffect(() => {
    sceneRef.current?.setColorOverrides(colors);
  }, [colors, status]);
  useEffect(() => {
    sceneRef.current?.setDiffVisible(diffVisible);
  }, [diffVisible, status]);
  useEffect(() => {
    sceneRef.current?.setMeasureTool(measureTool);
  }, [measureTool, status]);
  useEffect(() => {
    sceneRef.current?.setMeasureOverlay(
      measurements.map((m) => m.draw),
      pending.map((p) => p.point)
    );
  }, [measurements, pending, status]);
  // 計測の途中なら Esc で拾った点を捨ててやり直す（ポインタがビューアの上にあるときだけ。拡大表示の Esc より先に効かせる）。
  useEffect(() => {
    if (!pending.length && !measureError) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || !pointerInside.current) return;
      e.stopImmediatePropagation();
      setPending([]);
      setMeasureError(null);
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [pending, measureError]);

  // 保存した後に色・表示を変えたら「保存しました」を消す（保存したファイルは前の内容のまま）。
  useEffect(() => {
    setSaveState((s) => (s.kind === 'saved' ? { kind: 'idle' } : s));
  }, [colors, hiddenParts, kindVisible, attributeEdits]);

  // テーマ（ライト / ダーク・アクセント色）の切り替えに追従する。
  useEffect(() => {
    const observer = new MutationObserver(() => {
      sceneRef.current?.applyTheme(sceneColors());
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'style', 'class'] });
    return () => observer.disconnect();
  }, []);

  // 全画面表示中は Esc で戻す。
  useEffect(() => {
    if (!expanded) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setExpanded(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [expanded]);

  const hoverText = useMemo(() => {
    if (pivotPicking) return t('cad.hintPivot');
    const item = hover ? describe(itemsOf(data, hover.kind)[hover.id], label(hover), effectiveColor(data, colors, hover), t, data) : '';
    if (measureTool) {
      if (measureError) return t(`cad.measure.error.${measureError}` as MessageKey);
      const hint = t(`cad.measure.hint.${measureTool}` as MessageKey, { n: pending.length + 1 });
      return item ? `${hint}  ${item}` : hint;
    }
    return item || t(boxMode === 'off' ? 'cad.hint' : 'cad.hintBox');
  }, [hover, data, label, t, colors, boxMode, pivotPicking, measureTool, measureError, pending]);

  const selectAll = () => {
    const ids = sceneRef.current?.selectableIds(mode) ?? [];
    boxPick(
      ids.map((id) => ({ kind: mode, id })),
      false
    );
  };

  // 選んでいる要素（種類を問わない）と同じ色・同じ部品（レイヤー）の、今の種類の見えている要素をまとめて選ぶ。
  const sameKeys = useMemo(() => {
    const colorSet = new Set<string>();
    const partSet = new Set<number>();
    for (const s of selection) {
      const c = attributeColor(data, colors, s);
      if (c) colorSet.add(c);
      const p = partOf(data, s);
      if (p !== null) partSet.add(p);
    }
    return { colors: colorSet, parts: partSet };
  }, [selection, data, colors]);
  const canSameColor = sameKeys.colors.size > 0;
  const canSamePart = sameKeys.parts.size > 0 && mode !== 'part';
  const selectSame = (by: 'color' | 'part') => {
    const ids = (sceneRef.current?.selectableIds(mode) ?? []).filter((id) => {
      const s = { kind: mode, id };
      if (by === 'part') {
        const p = partOf(data, s);
        return p !== null && sameKeys.parts.has(p);
      }
      const c = attributeColor(data, colors, s);
      return c !== null && sameKeys.colors.has(c);
    });
    boxPick(
      ids.map((id) => ({ kind: mode, id })),
      false
    );
  };

  const colorChanges = (): CadColorChanges => {
    const changes: CadColorChanges = { faces: {}, parts: {} };
    colors.faces.forEach((hex, id) => (changes.faces[String(id)] = hex));
    colors.parts.forEach((hex, id) => (changes.parts[String(id)] = hex));
    return changes;
  };
  const viewState = (): CadViewState => ({
    hidden_parts: [...hiddenParts],
    visible: kindVisible,
    ...(Object.keys(attributeEdits).length ? { attributes: attributeEdits } : {})
  });
  const cancel = () => {
    if (closing) return;
    setClosing('cancel');
    onCancel(colorChanges(), viewState());
  };
  const submit = () => {
    if (closing) return;
    setClosing('submit');
    const text = message.trim();
    // 指示があるときだけ、今の画面（選んだ要素の強調を含む）を低解像度で撮って AI へ一緒に見せる。
    const screenshot = text ? (sceneRef.current?.snapshot(screenshotLongSide) ?? null) : null;
    onSubmit(
      selection,
      colorChanges(),
      text,
      viewState(),
      screenshot,
      measurements.map((m) => m.result)
    );
  };

  // 変えた色・隠した部品を反映した 3D モデル（STEP / DXF / 色付き STL）を書き出す（app.py の cad_viewer_save）。
  // ダウンロードボタンは backend がチャットに出す。ビューアは閉じない。
  const save = async () => {
    if (!saveId || saveState.kind === 'saving') return;
    setSaveState({ kind: 'saving' });
    const action: IAction = {
      id: '',
      name: 'cad_viewer_save',
      payload: { save_id: saveId, colors: colorChanges(), ...viewState() },
      label: '',
      tooltip: '',
      forId: '',
      onClick: () => {}
    };
    try {
      const res = await client.callAction(action, sessionId);
      const r = res?.response;
      if (r?.ok) setSaveState({ kind: 'saved', path: String(r.path ?? '') });
      else setSaveState({ kind: 'error', message: String(r?.error ?? t('cad.saveFailed')) });
    } catch (err) {
      setSaveState({ kind: 'error', message: err instanceof Error ? err.message : t('cad.saveFailed') });
    }
  };
  const saveText =
    saveState.kind === 'saved'
      ? t('cad.saved', { path: saveState.path })
      : saveState.kind === 'error'
        ? `${t('cad.saveFailed')} ${saveState.message}`
        : '';

  const summary = `${data.source.format ?? ''}${data.source.path ? ' ' + data.source.path.split(/[\\/]/).pop() : ''}  ${data.bbox.size
    .map(fmt)
    .join(' × ')}${data.units !== 'unknown' ? ' ' + data.units : ''}`;

  return (
    <div
      ref={barRef}
      className={`ask-form-bar cad-viewer-bar${expanded ? ' cad-viewer-expanded' : ''}${closing ? ' cad-viewer-busy' : ''}`}
      aria-busy={!!closing}
      onPointerEnter={() => (pointerInside.current = true)}
      onPointerLeave={() => (pointerInside.current = false)}
    >
      {question && <div className="ask-form-question">{question}</div>}
      <div className="cad-viewer-toolbar">
        <div className="cad-viewer-history">
          <button type="button" className="cad-viewer-chip" title={t('cad.undoTitle')} aria-label={t('cad.undo')} disabled={!history.canUndo} onClick={history.undo}>
            ↶ {t('cad.undo')}
          </button>
          <button type="button" className="cad-viewer-chip" title={t('cad.redoTitle')} aria-label={t('cad.redo')} disabled={!history.canRedo} onClick={history.redo}>
            ↷ {t('cad.redo')}
          </button>
        </div>
        <div className="cad-viewer-modes" role="radiogroup" aria-label={t('cad.selectKind')}>
          {kinds.map((k) => (
            <button
              key={k}
              type="button"
              role="radio"
              aria-checked={mode === k}
              className={`cad-viewer-chip${mode === k && !measureTool ? ' active' : ''}`}
              onClick={() => {
                setMode(k);
                chooseMeasureTool(null);
              }}
            >
              {t(KIND_LABEL[k])}
            </button>
          ))}
          <span className="cad-viewer-sep" />
          {BOX_MODES.map((b) => (
            <button
              key={b.id}
              type="button"
              role="radio"
              aria-checked={boxMode === b.id}
              title={t(b.title)}
              className={`cad-viewer-chip${boxMode === b.id && !measureTool ? ' active' : ''}`}
              onClick={() => {
                setBoxMode(b.id);
                chooseMeasureTool(null);
              }}
            >
              {t(b.label)}
            </button>
          ))}
          <button type="button" className="cad-viewer-chip" title={t('cad.selectAllTitle')} onClick={selectAll}>
            {t('cad.selectAll')}
          </button>
          <button
            type="button"
            className="cad-viewer-chip"
            title={t('cad.deselectAllTitle')}
            disabled={selection.length === 0}
            onClick={() => setSelection([])}
          >
            {t('cad.deselectAll')}
          </button>
          <button
            type="button"
            className="cad-viewer-chip"
            title={t('cad.sameColorTitle')}
            disabled={!canSameColor}
            onClick={() => selectSame('color')}
          >
            {t('cad.sameColor')}
          </button>
          {hasParts && (
            <button
              type="button"
              className="cad-viewer-chip"
              title={t('cad.samePartTitle')}
              disabled={!canSamePart}
              onClick={() => selectSame('part')}
            >
              {t('cad.samePart')}
            </button>
          )}
        </div>
        <div className="cad-viewer-views">
          {VIEWS.map((v) => (
            <button
              key={v}
              type="button"
              className="cad-viewer-chip cad-viewer-view"
              title={t(`cad.view.${v}` as MessageKey)}
              onClick={() => sceneRef.current?.setView(v)}
            >
              {v.toUpperCase()}
            </button>
          ))}
          <button type="button" className="cad-viewer-chip" title={t('cad.fitTitle')} onClick={() => sceneRef.current?.fitAll()}>
            {t('cad.fit')}
          </button>
          <button
            type="button"
            className={`cad-viewer-chip${pivotPicking ? ' active' : ''}`}
            aria-pressed={pivotPicking}
            title={t('cad.pivotTitle')}
            onClick={() => setPivotPicking((x) => !x)}
          >
            {t('cad.pivot')}
          </button>
          <button
            type="button"
            className={`cad-viewer-chip${originAxes ? ' active' : ''}`}
            aria-pressed={originAxes}
            title={t('cad.originAxesTitle')}
            onClick={() => setOriginAxes((x) => !x)}
          >
            {t('cad.originAxes')}
          </button>
          <button
            type="button"
            className={`cad-viewer-chip${measureOpen ? ' active' : ''}`}
            aria-pressed={measureOpen}
            title={t('cad.measureTitle')}
            onClick={() => {
              const open = !measureOpen;
              setMeasureOpen(open);
              chooseMeasureTool(open ? (measureTool ?? MEASURE_TOOLS[0]) : null);
            }}
          >
            {t('cad.measure')}
          </button>
          {hasDiff && (
            <button
              type="button"
              className={`cad-viewer-chip${diffVisible ? ' active' : ''}`}
              aria-pressed={diffVisible}
              title={t('cad.diffTitle')}
              onClick={() => setDiffVisible((x) => !x)}
            >
              {t('cad.diff')}
            </button>
          )}
          <button
            type="button"
            className={`cad-viewer-chip${sidebarOpen ? ' active' : ''}`}
            aria-pressed={sidebarOpen}
            onClick={() => setSidebarOpen((x) => !x)}
          >
            {t('cad.sidebar')}
          </button>
          <button type="button" className="cad-viewer-chip" onClick={() => setExpanded((x) => !x)}>
            {expanded ? t('cad.shrink') : t('cad.expand')}
          </button>
        </div>
      </div>
      {measureOpen && (
        <div className="cad-viewer-measure-bar" role="radiogroup" aria-label={t('cad.measure')}>
          {MEASURE_TOOLS.map((m) => (
            <button
              key={m}
              type="button"
              role="radio"
              aria-checked={measureTool === m}
              title={t(`cad.measure.tool.${m}Title` as MessageKey)}
              className={`cad-viewer-chip${measureTool === m ? ' active' : ''}`}
              onClick={() => chooseMeasureTool(m)}
            >
              {t(`cad.measure.tool.${m}` as MessageKey)}
            </button>
          ))}
          <button
            type="button"
            className="cad-viewer-chip"
            title={t('cad.measure.clearTitle')}
            disabled={!measurements.length && !pending.length}
            onClick={() => {
              setMeasurements([]);
              setPending([]);
              setMeasureError(null);
            }}
          >
            {t('cad.measure.clear')}
          </button>
        </div>
      )}
      <div className="cad-viewer-body">
        {sidebarOpen && (
          <CadSidebar
            data={data}
            kinds={kinds}
            selection={selection}
            kindVisible={kindVisible}
            onKindVisible={(k, v) => setKindVisible((cur) => ({ ...cur, [k]: v }))}
            hiddenParts={hiddenParts}
            onHiddenParts={setHiddenParts}
            colors={colors}
            onColors={setColors}
            onTogglePart={(id) => toggle({ kind: 'part', id })}
            onHoverPart={(id) => sceneRef.current?.setExternalHover(id === null ? null : { kind: 'part', id })}
            attributeEdits={attributeEdits}
            onAttributeEdits={setAttributeEdits}
            onSelectItems={(items) => {
              // PMI のクリック: 指す要素を選び、そこへ寄せる（選べない種類の要素も、寄せる範囲には含める）。
              boxPick(items.filter((s) => kinds.includes(s.kind)), false);
              sceneRef.current?.fitItems(items);
            }}
          />
        )}
        <div className="cad-viewer-host" ref={hostRef}>
          {status === 'loading' && <div className="cad-viewer-overlay">{t('cad.loading')}</div>}
          {status === 'error' && <div className="cad-viewer-overlay">{t('cad.webglError')}</div>}
          {closing && (
            <div className="cad-viewer-overlay cad-viewer-closing" role="status">
              <span className="cad-viewer-spinner" aria-hidden="true" />
              {t(closing === 'submit' ? 'cad.submitting' : 'cad.closing')}
            </div>
          )}
        </div>
      </div>
      <div className="cad-viewer-status">
        <span className="cad-viewer-hover">{hoverText}</span>
        {hasDiff && diffVisible && <span className="cad-viewer-legend">{t('cad.diffLegend')}</span>}
        <span className="cad-viewer-summary">{summary}</span>
      </div>
      {measurements.length > 0 && (
        <div className="cad-viewer-measures">
          {measurements.map((m, i) => (
            <span key={i} className="cad-viewer-measure-tag" title={measureTitle(m, t)}>
              {`${i + 1}: ${m.tool === 'planes' && m.result.type === 'plane_angle' ? t('cad.measure.angle') : t(`cad.measure.tool.${m.tool}` as MessageKey)} ${m.draw.label}`}
              <button
                type="button"
                aria-label={t('cad.measure.remove')}
                onClick={() => setMeasurements((list) => list.filter((_, j) => j !== i))}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="cad-viewer-selected">
        {selection.length === 0 ? (
          <span className="cad-viewer-empty">{t('cad.noneSelected')}</span>
        ) : (
          selection.map((s) => (
            <span
              key={`${s.kind}-${s.id}`}
              className="cad-viewer-tag"
              title={describe(itemsOf(data, s.kind)[s.id], label(s), effectiveColor(data, colors, s), t, data)}
            >
              {label(s)}
              <button type="button" aria-label={t('cad.remove')} onClick={() => toggle(s)}>
                ×
              </button>
            </span>
          ))
        )}
        {selection.length > 0 && (
          <button type="button" className="cad-viewer-clear" onClick={() => setSelection([])}>
            {t('cad.clear')}
          </button>
        )}
      </div>
      <textarea
        className="cad-viewer-message"
        rows={2}
        value={message}
        placeholder={t('cad.messagePlaceholder')}
        aria-label={t('cad.message')}
        onChange={(e) => setMessage(e.target.value)}
        onKeyDown={(e) => {
          // Ctrl（⌘）+ Enter で確定。Enter だけなら改行（IME の変換確定と区別するため）。
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <div className="ask-form-actions">
        {/* 左端はキャンセル（ほかの質問ツールと同じ位置）。保存・確定は右にまとめる。 */}
        <button type="button" className="ask-form-cancel" title={t('cad.cancelTitle')} disabled={!!closing} onClick={cancel}>
          {t('cad.close')}
        </button>
        <div className="cad-viewer-actions-right">
          {saveText && (
            <span className={`cad-viewer-save-status${saveState.kind === 'error' ? ' error' : ''}`} title={saveText}>
              {saveText}
            </span>
          )}
          {saveId && (
            <button
              type="button"
              className="ask-form-cancel"
              title={t('cad.saveTitle')}
              disabled={saveState.kind === 'saving'}
              onClick={() => void save()}
            >
              {saveState.kind === 'saving' ? t('cad.saving') : t('cad.save')}
            </button>
          )}
          <button type="button" className="ask-form-submit" title={t('cad.confirmTitle')} disabled={!!closing} onClick={submit}>
            {closing === 'submit' && <span className="cad-viewer-spinner" aria-hidden="true" />}
            {closing === 'submit' ? t('cad.submitting') : t('cad.confirm', { count: selection.length })}
          </button>
        </div>
      </div>
    </div>
  );
}
