import { useCallback, useContext, useEffect, useMemo, useRef, useState, type MutableRefObject, type ReactElement } from 'react';
import { useRecoilValue } from 'recoil';
import { ChainlitContext, sessionIdState, useChatData, type IAction } from '@chainlit/react-client';
import { BACKEND_URL } from '../chainlitClient';
import { useI18n } from '../i18n';
import type { MessageKey } from '../i18n/messages';
import {
  CAD_DATA_FORMAT,
  attributeColor,
  connectedFaces,
  decodeFloat32,
  fetchCadFloat32,
  fetchCadPackage,
  hasFaceAngles,
  isMatrix4,
  dropUnchangedPartColors,
  effectiveColor,
  featureSizeLabel,
  holeGroup,
  itemsOf,
  partOf,
  pmiById,
  pmiTargets,
  replaceTopology,
  rgbText,
  sameSelection,
  selectionKey,
  toLocalPoint,
  toLocalVector,
  type BoxMode,
  type CadAlignmentInfo,
  type CadColorOverrides,
  type CadData,
  type CadDeviationStats,
  type CadItemInfo,
  type CadKind,
  type CadLocalAxis,
  type CadSavedHistory,
  type CadSelection,
  type CadTopology,
  type CadViewerProps,
  DATUM_NAMES,
  isHoleDatum,
  type DatumMode,
  type DatumName,
  type Datums,
  type DatumScenePick,
  MODEL_MARKS,
  splitGrid,
  splitLayoutsFor,
  type SplitLayout
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
import { NumberInput } from './cad/NumberInput';
import type { CadAttributeEdits } from './cad/CadStepPanel';
import { restoreHistory, serializeHistory, type EditSnapshot } from './cad/editHistory';
import {
  AlignRow,
  AnalysisSettingsRow,
  datumLabel,
  ScalarLegend,
  type AlignState,
  type AnalysisMode,
  type RangeState
} from './cad/CadAnalysisPanel';
import { autoRange, defaultZebra, curvatureValues, type CurvatureKind, type SurfaceDisplay, type ZebraSettings } from './cad/surfaceShading';

const KIND_LABEL: Record<CadKind, MessageKey> = {
  face: 'cad.kind.face',
  edge: 'cad.kind.edge',
  vertex: 'cad.kind.vertex',
  part: 'cad.kind.part',
  hole: 'cad.kind.hole',
  outer: 'cad.kind.outer'
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

/** 計測の一覧に出す説明（ツールチップ。距離は各軸の成分も。ローカル座標系のときはその軸の成分）。 */
function measureTitle(m: CadMeasurement, t: (k: MessageKey) => string, axis: CadLocalAxis | null): string {
  const parts = [`${t(`cad.measure.tool.${m.tool}` as MessageKey)}  ${m.draw.label}`];
  const delta = m.result.delta;
  if (Array.isArray(delta)) {
    const d = toLocalVector(axis, delta.map(Number));
    const names = axis ? ["ΔX'", "ΔY'", "ΔZ'"] : ['ΔX', 'ΔY', 'ΔZ'];
    parts.push(names.map((k, i) => `${k} ${fmtNum(d[i])}`).join('  '));
  }
  if (m.approximate) parts.push(t('cad.measure.approx'));
  return parts.join('\n');
}

/** 確定時に返す、変えた色（キーは ID の文字列）。 */
export interface CadColorChanges {
  faces: Record<string, string | null>;
  parts: Record<string, string | null>;
}

/** 画面のテーマ（ライト / ダーク）から 3D 表示の色を決める。 */
function sceneColors(): CadSceneColors {
  const dark = document.documentElement.dataset.theme === 'dark';
  return {
    background: dark ? '#17171a' : '#f7f7f9',
    // 非表示領域は青みのある背景にして、ふだんの画面と見分けられるようにする。
    hiddenBackground: dark ? '#14202e' : '#e3ecf7',
    face: dark ? '#8d93a0' : '#b9bfcb',
    edge: dark ? '#e4e4ea' : '#2b2c31',
    vertex: dark ? '#f0f0f4' : '#1f2023',
    // 選択はオレンジで目立たせ、ホバーはそれと見分けられる水色にする。
    hover: '#1fb6d4',
    selected: SELECTED_COLOR,
    // PMI は CAD の注記と同じく背景に対して最も読みやすい色（styles.css の .cad-viewer-pmi-* と同じ）。
    pmi: dark ? '#ffffff' : '#000000'
  };
}

/**
 * 比較するときの、① 基準モデルと ②③④ 比較モデルの初めの色（① 緑・② 赤・③ 青・④ 黄）。選択のオレンジ・ホバーの水色・
 * 計測の紫と見分けられ、重ねても区別しやすい色にしてある。
 */
const COMPARE_COLORS = ['#2fa84f', '#e5484d', '#3e7bfa', '#e0b000'];

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
    out.push(`${kind} ${featureSizeLabel(feat)}${extra}`);
  }
  const ids = Array.isArray(item.pmi) ? (item.pmi as string[]) : [];
  if (ids.length) {
    const byId = pmiById(data);
    out.push(...ids.slice(0, 3).map((id) => byId.get(id)?.text ?? id));
  }
  return out;
}

/**
 * ホバー中・選択中の要素の短い説明（例: 「面 #3 PLANE 面積 600 RGB(200, 0, 0) >Z」）。
 * axis（ローカル座標系）を渡すと、点・穴の中心と法線をその座標系で出す。
 */
function describe(
  item: CadItemInfo | undefined,
  label: string,
  color: string | null,
  t: (k: MessageKey) => string,
  data?: CadData,
  axis: CadLocalAxis | null = null
): string {
  if (!item) return label;
  const parts = [label];
  const xyz = (v: number[]) => `(${v.map(fmt).join(', ')})`;
  if (item.kind === 'part') parts.push(item.path ?? item.name ?? '');
  else if (item.geomType && item.kind !== 'hole' && item.kind !== 'outer') parts.push(item.geomType);
  if (item.kind === 'outer') {
    if (typeof item.perimeter === 'number') parts.push(`${t('cad.info.length')} ${fmt(item.perimeter)}`);
    if (item.center) parts.push(xyz(toLocalPoint(axis, item.center)));
    return parts.filter(Boolean).join('  ');
  }
  if (item.kind === 'hole') {
    // 閉じた形の穴の口は形と大きさ（□20×10 等）と、通し・止まり。
    if (typeof item.shape === 'string') parts.push(featureSizeLabel(item as Parameters<typeof featureSizeLabel>[0]));
    else if (typeof item.diameter === 'number') parts.push(`${t('cad.info.diameter')}${fmt(item.diameter)}`);
    if (typeof item.through === 'boolean') parts.push(item.through ? t('cad.info.through') : t('cad.info.blind'));
    if (item.center) parts.push(xyz(toLocalPoint(axis, item.center)));
    if (item.normal) parts.push(`${t('cad.info.normal')} ${xyz(toLocalVector(axis, item.normal))}`);
    return parts.filter(Boolean).join('  ');
  }
  if (item.area !== undefined) parts.push(`${t('cad.info.area')} ${fmt(item.area)}`);
  if (item.length !== undefined) parts.push(`${t('cad.info.length')} ${fmt(item.length)}`);
  if (item.radius !== undefined) parts.push(`R ${fmt(item.radius)}`);
  if (item.point) parts.push(xyz(toLocalPoint(axis, item.point)));
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
  // 答えを待つ期限（backend の [cad_viewer] timeout_seconds）は、要素が届いた時点から数える（backend は送った時点から
  // 待ち始める）。ビューア用データを読み終えてから数えると、大きなモデルでは読み込みの時間だけ残り時間が長く出ていた。
  // 出し直したとき（要素の ID が変わる）はそこから数え直す。
  const timeoutSeconds = props?.timeout_seconds ?? 0;
  const deadlineAt = useMemo(
    () => (timeoutSeconds > 0 ? Date.now() + timeoutSeconds * 1000 : 0),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 要素が変わったときだけ数え直す
    [element?.id]
  );
  if (!askUser || !element || !props) return null;
  const panel = (data: CadData) => (
    <CadViewerPanel
      key={element.id}
      question={props.question ?? ''}
      kinds={props.select?.length ? props.select : ['face']}
      data={data}
      saveId={props.save_id ?? ''}
      screenshotLongSide={props.screenshot_long_side ?? 0}
      holeAngleDefault={props.hole_normal_angle_deg ?? 3}
      holeSizeDefault={props.hole_size_tolerance ?? 0.3}
      connectedAngleDefault={props.connected_face_angle_deg ?? 0}
      sewDefault={props.sew_tolerance ?? 0.5}
      deadlineAt={deadlineAt}
      canAlign={!!props.can_align}
      deviationToleranceDefault={props.deviation_tolerance ?? data.comparisons?.find((c) => c.deviation?.tolerance)?.deviation?.tolerance ?? 0.1}
      datumPadRadiusDefault={defaultPadRadius(props.datum_pad_radius, data)}
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
      onSwap={(model) => askUser.callback({ submitted: false, selected: [], swap_models: model })}
    />
  );
  if (props.data_id) {
    // ビューア用データは props に入っておらず、HTTP で取りに行く（src/tools/cad_viewer.py の _VIEWER_PACKAGES）。
    // 読み込めなかった・読み込み中に閉じたときは、表示の状態を返さない（backend は自動保存をせずに LLM へ伝える）。
    return (
      <CadViewerLoader
        key={element.id}
        dataId={props.data_id}
        question={props.question ?? ''}
        onClose={(loadError) =>
          askUser.callback(
            loadError ? { submitted: false, selected: [], load_error: loadError } : { submitted: false, selected: [], load_cancelled: true }
          )
        }
      >
        {panel}
      </CadViewerLoader>
    );
  }
  if (!props.data || props.data.format !== CAD_DATA_FORMAT) return null;
  return panel(props.data);
}

type LoadState = { kind: 'loading' } | { kind: 'ready'; data: CadData } | { kind: 'error'; error: string };

/** ビューア用データ（app.py の GET /locohane/cad/data/{id}）を取ってきてから、ビューアを出す。 */
function CadViewerLoader({
  dataId,
  question,
  onClose,
  children
}: {
  dataId: string;
  question: string;
  /** loadError: 読み込めなかった理由（読み込み中にキャンセルしたときは null）。 */
  onClose: (loadError: string | null) => void;
  children: (data: CadData) => ReactElement;
}) {
  const { t } = useI18n();
  const [state, setState] = useState<LoadState>({ kind: 'loading' });
  useEffect(() => {
    const controller = new AbortController();
    setState({ kind: 'loading' });
    fetchCadPackage(BACKEND_URL, dataId, controller.signal)
      .then((data) => {
        if (data.format !== CAD_DATA_FORMAT) throw new Error(`unexpected viewer data format ${String(data.format)}`);
        setState({ kind: 'ready', data });
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted) return;
        setState({ kind: 'error', error: e instanceof Error ? `${e.name}: ${e.message}` : String(e) });
      });
    return () => controller.abort();
  }, [dataId]);
  if (state.kind === 'ready') return children(state.data);
  return (
    <div className="ask-form-bar cad-viewer-bar">
      {question && <div className="ask-form-question">{question}</div>}
      <div className="cad-viewer-host">
        <div className="cad-viewer-overlay" role="status">
          {state.kind === 'loading' ? t('cad.loadingData') : t('cad.loadFailed', { error: state.error })}
        </div>
      </div>
      <div className="ask-form-actions">
        <button type="button" className="ask-form-cancel" onClick={() => onClose(state.kind === 'error' ? state.error : null)}>
          {t('cad.close')}
        </button>
      </div>
    </div>
  );
}

interface PanelProps {
  question: string;
  kinds: CadKind[];
  data: CadData;
  saveId: string;
  screenshotLongSide: number;
  /** 穴にマウスを乗せたとき、同じ向きとしてまとめる法線の角度差の初めの値（度）。 */
  holeAngleDefault: number;
  /** 穴をまとめるときの穴サイズ誤差の初めの値（モデルの単位。0 以下なら初めは条件を外す）。 */
  holeSizeDefault: number;
  /** 「連続面」の折れ角の上限の初めの値（度。0 以下なら初めは条件を外す）。 */
  connectedAngleDefault: number;
  /** 「縫合」の欄の初めの値（モデルの単位）。 */
  sewDefault: number;
  /** backend が答えを待つ期限（Date.now() の値。0 = 無期限）。残りが少なくなったら残り時間を出す。 */
  deadlineAt: number;
  /** 基準モデルとの位置合わせをやり直せるか（app.py の cad_viewer_align）。 */
  canAlign: boolean;
  /** 偏差の公差（±）の初めの値。 */
  deviationToleranceDefault: number;
  /** 点のデータムの当て面の半径の初めの値（モデルの単位）。 */
  datumPadRadiusDefault: number;
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
  /** 比較モデル model（0 = ②）を ① にして開き直す（backend が役割を入れ替えたビューア用データを作り、ビューアを出し直す）。 */
  onSwap: (model: number) => void;
}

/** 確定・閉じる・保存のときに返す表示の状態（backend は変更があればビューア用データに反映して自動で保存する）。 */
interface CadViewState {
  hidden_parts: number[];
  visible: { face: boolean; edge: boolean; vertex: boolean };
  /** STEP の属性を編集した製品（製品名 → 編集後の一覧）。編集が無ければ入れない。 */
  attributes?: CadAttributeEdits;
  /** 元に戻す・やり直すの履歴（backend はビューア用データの viewState.history に入れる）。戻れる時点が無ければ入れない。 */
  history?: CadSavedHistory;
  /** 「座標系」で選んでいたローカル座標系（data.localAxes の ID）。ワールドなら入れない。 */
  local_axis?: number;
  /** 「縫合」で穴・外周を求め直した（backend は求め直したデータを自動で保存する）。 */
  sewn?: boolean;
  /** 基準モデルとの偏差の公差（±）。基準モデルがあるときだけ（backend は統計をこの公差で求める）。 */
  deviation_tolerance?: number;
}

/** 点のデータムの当て面の半径の初めの値。設定が 0 以下なら形の対角線の 1/50（src/cad/align.py の PAD_RADIUS_RATIO）を 2 桁に丸める。 */
function defaultPadRadius(setting: number | undefined, data: CadData): number {
  if (typeof setting === 'number' && setting > 0) return setting;
  const s = data.bbox?.size ?? [];
  const diag = Math.hypot(s[0] ?? 0, s[1] ?? 0, s[2] ?? 0) || 1;
  return Number((diag / 50).toPrecision(2));
}

// 残り時間を出し始める秒数（3D ビューア専用のタイムアウトの残りがこれを切ったら出す）。
const TIMEOUT_WARN_SECONDS = 300;

function initialHiddenParts(data: CadData): Set<number> {
  // 「保存」したデータは保存したときの表示に戻す。それ以外は、DXF で非表示・フリーズのレイヤーを
  // 最初から隠す（CAD で開いたときと同じ見え方）。
  if (data.viewState?.hiddenParts) return new Set(data.viewState.hiddenParts);
  return new Set((data.parts ?? []).filter((p) => p.layer && (p.layer.off || p.layer.frozen)).map((p) => p.id));
}

/** 元に戻す・やり直すの対象（選択・変えた色・隠した部品・種類ごとの表示・属性の編集）。カメラの向きは含めない。 */
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
function useEditHistory(state: EditSnapshot, apply: (s: EditSnapshot) => void, initial?: () => { past: EditSnapshot[]; future: EditSnapshot[] }) {
  // 自動保存・保存したデータを開き直したときは、前に開いていたときの履歴から始める（editHistory.ts の restoreHistory）。
  const seeded = useRef<{ past: EditSnapshot[]; future: EditSnapshot[] } | null>(null);
  if (seeded.current === null) seeded.current = initial?.() ?? { past: [], future: [] };
  const past = useRef<EditSnapshot[]>(seeded.current.past);
  const future = useRef<EditSnapshot[]>(seeded.current.future);
  const last = useRef<EditSnapshot>(state);
  const lastChange = useRef({ keys: '', at: 0 });
  const applyRef = useRef(apply);
  applyRef.current = apply;
  const [, setVersion] = useState(0);
  const { selection, colors, hiddenParts, hiddenItems, kindVisible, attributeEdits } = state;

  useEffect(() => {
    const cur: EditSnapshot = { selection, colors, hiddenParts, hiddenItems, kindVisible, attributeEdits };
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
  }, [selection, colors, hiddenParts, hiddenItems, kindVisible, attributeEdits]);

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
  // 保存するときに履歴を読む（ビューア用データの viewState.history に入れる）。
  const entries = useCallback(() => ({ past: past.current, future: future.current, current: last.current }), []);
  // 履歴の各時点を書き換える（「縫合」で穴・外周の ID が変わったとき、古い ID の穴・外周を外す）。
  // 今の状態（last）も書き換えるので、呼んだ側は state を同じ fn で書き換えること（変更として積まないため）。
  const rewrite = useCallback((fn: (s: EditSnapshot) => EditSnapshot) => {
    past.current = past.current.map(fn);
    future.current = future.current.map(fn);
    last.current = fn(last.current);
    return last.current;
  }, []);
  const canUndo = past.current.length > 0;
  const canRedo = future.current.length > 0;
  return useMemo(() => ({ undo, redo, canUndo, canRedo, entries, rewrite }), [undo, redo, canUndo, canRedo, entries, rewrite]);
}

/** 穴・外周（ID が「縫合」で変わる）を選択・隠した要素から外した時点。 */
function withoutLoops(s: EditSnapshot): EditSnapshot {
  const loop = (k: string) => k === 'hole' || k === 'outer';
  const selection = s.selection.some((x) => loop(x.kind)) ? s.selection.filter((x) => !loop(x.kind)) : s.selection;
  const hiddenItems = [...s.hiddenItems].some((k) => loop(k.split(':')[0]))
    ? new Set([...s.hiddenItems].filter((k) => !loop(k.split(':')[0])))
    : s.hiddenItems;
  return selection === s.selection && hiddenItems === s.hiddenItems ? s : { ...s, selection, hiddenItems };
}

type SaveState ={ kind: 'idle' } | { kind: 'saving' } | { kind: 'saved'; path: string; notes: string[] } | { kind: 'error'; message: string };

function CadViewerPanel({
  question,
  kinds,
  data,
  saveId,
  screenshotLongSide,
  holeAngleDefault,
  holeSizeDefault,
  connectedAngleDefault,
  sewDefault,
  deadlineAt,
  canAlign,
  deviationToleranceDefault,
  datumPadRadiusDefault,
  onSubmit,
  onCancel,
  onSwap
}: PanelProps) {
  const { t } = useI18n();
  const client = useContext(ChainlitContext);
  const sessionId = useRecoilValue(sessionIdState);
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' });
  // 確定・キャンセル・入れ替えを押した後、backend がビューアを閉じるまでの間（二度押しを防ぎ、処理中と分かるようにする）。
  const [closing, setClosing] = useState<'submit' | 'cancel' | 'swap' | null>(null);
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
  // 穴にマウスを乗せたとき、同じ向きとしてまとめる法線の角度差（度）。
  const [holeAngle, setHoleAngle] = useState(Math.min(holeAngleDefault, 90));
  // 穴をまとめるときの穴サイズ誤差（使うか・値）。
  const [holeSizeOn, setHoleSizeOn] = useState(holeSizeDefault > 0);
  const [holeSize, setHoleSize] = useState(holeSizeDefault > 0 ? holeSizeDefault : 0.3);
  const holeSizeTol = holeSizeOn ? holeSize : null;
  // 「連続面」で隣の面へ広げる境目の折れ角の上限（使うか・値。度）。
  const [connectedAngleOn, setConnectedAngleOn] = useState(connectedAngleDefault > 0);
  const [connectedAngle, setConnectedAngle] = useState(connectedAngleDefault > 0 ? Math.min(connectedAngleDefault, 180) : 10);
  // 「縫合」: 縫合トレランス、求め直しの状態、このビューアで縫合したか（確定・閉じるときに backend へ伝える）。
  // 縫合で穴・外周が無くなっても欄は残す（縫合をやめて戻せるように）。
  const [canSew] = useState(() => !!saveId && !!(data.holes || data.holeList?.length || data.outerList?.length));
  const [sewTol, setSewTol] = useState(data.holes?.sewTolerance || sewDefault);
  const [sewState, setSewState] = useState<{ kind: 'idle' } | { kind: 'sewing' } | { kind: 'error'; message: string }>({ kind: 'idle' });
  const [appliedSew, setAppliedSew] = useState(data.holes?.sewTolerance ?? 0);
  const sewnRef = useRef(false);
  // 縫合で data の穴・外周を書き換えたら増やす（穴・外周の一覧を使う表示を描き直すため）。
  const [topologyVersion, setTopologyVersion] = useState(0);
  // PMI の 3D ラベルを出すか。
  const pmiItems = useMemo(() => [...pmiById(data).values()], [data]);
  const [pmiLabels, setPmiLabels] = useState(false);
  // 3D ビューア専用のタイムアウト（[cad_viewer] timeout_seconds）の残り秒数。
  const deadline = useRef(deadlineAt);
  const [remaining, setRemaining] = useState<number | null>(null);
  // 「座標系」で選んだローカル座標系（null ならワールド）。
  const localAxes = data.localAxes ?? [];
  const [localAxisId, setLocalAxisId] = useState<number | null>(null);
  const localAxis = localAxisId === null ? null : (localAxes[localAxisId] ?? null);
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
  // サイドバーには選択中の要素の一覧（「選択」タブ）もあるので、広い画面では部品の有無にかかわらず開いておく。
  // 比較モデル ②③④（compare_models）があれば、① 基準モデルと一緒にアセンブリのように並べて表示・非表示と色を変えられる。
  const comparisons = useMemo(() => (data.comparisons ?? []).slice(0, 3), [data]);
  const usable = useMemo(() => comparisons.map((c) => !c.error && !!c.indices && !!c.positions), [comparisons]);
  const hasRefModel = usable.some(Boolean);
  const [sidebarOpen, setSidebarOpen] = useState(() => !isNarrowScreen());
  // STEP の属性の編集（製品名 → 編集後の一覧）。
  const [attributeEdits, setAttributeEdits] = useState<CadAttributeEdits>({});
  // 前の版からの変化の重ね表示（compare_with で比べたときだけ）。
  const hasDiff = !!(data.diff?.added || data.diff?.removed);
  const [diffVisible, setDiffVisible] = useState(true);
  // 表面の解析表示（ゼブラ・曲率・偏差）。曲率は src/cad/analysis.py の主曲率、偏差は基準モデルとの符号付き距離。
  const hasCurvature = !!data.analysis?.k1;
  // ① からの偏差を持つ比較モデルがあるか（偏差の表示・位置合わせ）。
  const hasReference = comparisons.some((c, i) => usable[i] && !!c.deviation?.values);
  const [analysisMode, setAnalysisMode] = useState<AnalysisMode>('normal');
  const [zebra, setZebra] = useState<ZebraSettings>(() => defaultZebra(data.bbox));
  // ゼブラの平面の光源の「基準点」: 次にクリックした点を基準点にする（その 1 回だけ）。
  const [zebraBasePicking, setZebraBasePicking] = useState(false);
  const [curvatureKind, setCurvatureKind] = useState<CurvatureKind>('mean');
  const [curvRange, setCurvRange] = useState<RangeState | null>(null);
  const [devRange, setDevRange] = useState<RangeState | null>(null);
  // 範囲の最小・最大を 0 を挟んで連動させるか（曲率の大きさ以外の曲率・偏差）。
  const [rangeLinked, setRangeLinked] = useState(true);
  const [tolerance, setTolerance] = useState(deviationToleranceDefault);
  const [scalarHover, setScalarHover] = useState<number | null>(null);
  // 偏差の表示中、マウスの下の比較モデル（1〜3 = ②③④）とその点の偏差。
  const [compareHover, setCompareHover] = useState<{ model: number; value: number } | null>(null);
  // 比較モデルの ① への位置合わせ。偏差の値は位置を合わせ直すたびに backend から取り直す。
  const [alignOpen, setAlignOpen] = useState(false);
  // 比べるときはすべてを最初から見せる（① 緑・② 赤・③ 青・④ 黄。②③④ は半透明。色はサイドバーの「比較モデル」で変えられる）。
  const [compareVisible, setCompareVisible] = useState<boolean[]>(() => usable.slice());
  const [compareOpacity, setCompareOpacity] = useState(0.35);
  const [compareColors, setCompareColors] = useState<string[]>(() => COMPARE_COLORS.slice(1));
  const [modelVisible, setModelVisible] = useState(true);
  const [modelColor, setModelColor] = useState(COMPARE_COLORS[0]);
  // 比較モデルごとの、頂点ごとの ① からの偏差と、位置合わせの記録・偏差の統計（②③④ の順）。
  const [deviationValues, setDeviationValues] = useState<(Float32Array | null)[]>(() =>
    comparisons.map((c, i) => (usable[i] && c.deviation?.values ? decodeFloat32(c.deviation.values) : null))
  );
  const [alignInfos, setAlignInfos] = useState<(CadAlignmentInfo | null)[]>(() => comparisons.map((c) => c.deviation?.alignment ?? null));
  const [alignStatsList, setAlignStatsList] = useState<(CadDeviationStats | null)[]>(() => comparisons.map((c) => c.deviation?.stats ?? null));
  const [alignState, setAlignState] = useState<AlignState>({ kind: 'idle' });
  // 位置を合わせ直す比較モデル（data.comparisons の位置、-1 = すべて）。
  const [alignTarget, setAlignTarget] = useState(-1);
  // 画面分割と、画面ごとに映すモデル（0 = ①、1〜3 = ②③④）。初めは ①②③④ の順。
  const shownModels = useMemo(() => [0, ...usable.flatMap((u, i) => (u ? [i + 1] : []))], [usable]);
  const splitLayouts = useMemo(() => splitLayoutsFor(shownModels.length), [shownModels]);
  const [split, setSplit] = useState<SplitLayout>('single');
  const [paneModels, setPaneModels] = useState<number[]>(() => [...shownModels, ...[0, 0, 0, 0]].slice(0, 4));
  // データム（A〜D は点、H1・H2 は基準穴。D・穴は任意）と、次にクリックして拾うデータム・D の扱い・点の当て面の半径。
  // 拾うもの（点・穴）は枠で決まる。
  const [datums, setDatums] = useState<Datums>({});
  const [datumPicking, setDatumPicking] = useState<DatumName | null>(null);
  const datumKind = datumPicking && isHoleDatum(datumPicking) ? 'hole' : 'point';
  const [datumMode, setDatumMode] = useState<DatumMode>('priority');
  const [padRadius, setPadRadius] = useState(datumPadRadiusDefault);
  const curvature = useMemo(() => {
    if (!data.analysis?.k1 || !data.analysis.k2) return null;
    return { k1: decodeFloat32(data.analysis.k1), k2: decodeFloat32(data.analysis.k2) };
  }, [data]);
  const curvValues = useMemo(
    () => (curvature && analysisMode === 'curvature' ? curvatureValues(curvature.k1, curvature.k2, curvatureKind) : null),
    [curvature, curvatureKind, analysisMode]
  );
  // 範囲を自動にしているときは、値の分布から決める（曲率は平均・ガウス・k1・k2 を 0 中心、偏差も 0 中心）。
  const effCurvRange = useMemo<RangeState>(() => {
    if (curvRange && !curvRange.auto) return curvRange;
    if (!curvValues) return { min: -1, max: 1, auto: true };
    const [min, max] = autoRange(curvValues, curvatureKind !== 'absmax');
    return { min: curvatureKind === 'absmax' ? 0 : min, max, auto: true };
  }, [curvRange, curvValues, curvatureKind]);
  // 偏差の範囲を自動で決めるときは、すべての比較モデルの値をまとめて見る（色の意味をそろえるため、範囲は共通）。
  const allDeviation = useMemo(() => {
    const list = deviationValues.filter((v): v is Float32Array => !!v);
    if (list.length <= 1) return list[0] ?? null;
    const out = new Float32Array(list.reduce((n, v) => n + v.length, 0));
    let at = 0;
    for (const v of list) {
      out.set(v, at);
      at += v.length;
    }
    return out;
  }, [deviationValues]);
  const effDevRange = useMemo<RangeState>(() => {
    if (devRange && !devRange.auto) return devRange;
    if (!allDeviation) return { min: -1, max: 1, auto: true };
    const [min, max] = autoRange(allDeviation, true, 0.01, 0.99);
    const m = Math.max(max, tolerance * 3);
    return { min: Math.min(min, -m), max: m, auto: true };
  }, [devRange, allDeviation, tolerance]);
  const surfaceDisplay = useMemo<SurfaceDisplay>(() => {
    if (analysisMode === 'zebra') return { mode: 'zebra', zebra };
    if (analysisMode === 'curvature' && curvValues)
      return { mode: 'scalar', scalar: { values: curvValues, min: effCurvRange.min, max: effCurvRange.max, tolerance: 0, scheme: 'rainbow' } };
    // 偏差は ① でなく比較モデルの上に描く（setDeviationDisplay）。① はふつうの表示。
    return { mode: 'normal' };
  }, [analysisMode, zebra, curvValues, effCurvRange]);
  const deviationOn = analysisMode === 'deviation' && hasReference;
  const [kindVisible, setKindVisible] = useState({ face: true, edge: true, vertex: true, ...data.viewState?.visible });
  const [hiddenParts, setHiddenParts] = useState<Set<number>>(() => initialHiddenParts(data));
  // 「非表示」で 1 つずつ隠した面・エッジ・頂点・穴・外周（selectionKey の集合）。部品は hiddenParts に入れる。
  const [hiddenItems, setHiddenItems] = useState<Set<string>>(() => new Set());
  // 非表示領域: 隠したものだけを映す画面（CAD の「表示/非表示空間の切り替え」）。入る前の選択は取っておき、戻るときに戻す。
  const [hiddenSpace, setHiddenSpace] = useState(false);
  const stashedSelection = useRef<CadSelection[]>([]);
  const [colors, setColors] = useState<CadColorOverrides>({ faces: new Map(), parts: new Map() });
  const history = useEditHistory(
    { selection, colors, hiddenParts, hiddenItems, kindVisible, attributeEdits },
    (s) => {
      setSelection(s.selection);
      setColors(s.colors);
      setHiddenParts(s.hiddenParts);
      setHiddenItems(s.hiddenItems);
      setKindVisible(s.kindVisible);
      setAttributeEdits(s.attributeEdits);
    },
    () => restoreHistory(data)
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
  // 穴のクリック: 同じ向きの穴をまとめて選ぶ（すべて選んであれば、まとめて外す）。
  const pickGroup = useCallback((items: CadSelection[]) => {
    if (!items.length) return;
    setSelection((cur) => {
      const have = new Set(cur.map((s) => selectionKey(s.kind, s.id)));
      if (items.every((i) => have.has(selectionKey(i.kind, i.id)))) {
        const drop = new Set(items.map((i) => selectionKey(i.kind, i.id)));
        return cur.filter((s) => !drop.has(selectionKey(s.kind, s.id)));
      }
      return [...cur, ...items.filter((i) => !have.has(selectionKey(i.kind, i.id)))];
    });
  }, []);
  const boxPick = useCallback((items: CadSelection[], remove: boolean) => {
    // 枠で囲んだ要素・今の選択はどちらも数万件になりうるので、集合で引く（配列どうしを探すと 2 乗の時間がかかる）。
    setSelection((cur) => {
      if (remove) {
        const drop = new Set(items.map((i) => selectionKey(i.kind, i.id)));
        const kept = cur.filter((s) => !drop.has(selectionKey(s.kind, s.id)));
        return kept.length === cur.length ? cur : kept;
      }
      const have = new Set(cur.map((s) => selectionKey(s.kind, s.id)));
      const added: CadSelection[] = [];
      for (const i of items) {
        const key = selectionKey(i.kind, i.id);
        if (have.has(key)) continue;
        have.add(key);
        added.push(i);
      }
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
      const out = measure(tool, picks, infoOf, (id) => sceneRef.current?.edgePoints(id) ?? []);
      if ('error' in out) setMeasureError(out.error);
      else setMeasurements((list) => [...list, out.ok].slice(-MEASUREMENTS_MAX));
    },
    [infoOf]
  );
  // PMI（3D ラベル・パネルの一覧）のクリック: 指す要素を選び、そこへ寄せる（選べない種類の要素も、寄せる範囲には含める）。
  // データムを拾うモードでクリックした点・穴を、拾っているデータムにする（穴のピンの直径は初めは穴の径）。
  // 点の枠（A〜D）には点、穴の枠（H1・H2）には穴だけを入れる（シーンは枠に合わせて拾うものを変えている）。
  const datumPickRef = useRef<(p: DatumScenePick) => void>(() => {});
  datumPickRef.current = (p: DatumScenePick) => {
    const name = datumPicking;
    if (!name || (p.type === 'hole') !== isHoleDatum(name)) return;
    setDatums((cur) => ({ ...cur, [name]: p.type === 'hole' ? { ...p, pin: Number(p.diameter.toFixed(3)) } : p }));
    setDatumPicking(null);
  };
  const selectPmiRef = useRef<(id: string) => void>(() => {});
  selectPmiRef.current = (id: string) => {
    const item = pmiById(data).get(id);
    if (!item) return;
    const items = pmiTargets(item);
    boxPick(
      items.filter((s) => kinds.includes(s.kind)),
      false
    );
    sceneRef.current?.fitItems(items);
  };
  const chooseMeasureTool = (tool: MeasureTool | null) => {
    setMeasureTool(tool);
    setPending([]);
    setMeasureError(null);
    if (tool) {
      setBoxMode('off');
      setPivotPicking(false);
      setDatumPicking(null);
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
          {
            onHover: setHover,
            onPick: toggle,
            onPickGroup: pickGroup,
            onBoxPick: boxPick,
            onPivotSet: () => setPivotPicking(false),
            onZebraBasePick: (p) => {
              setZebra((z) => ({ ...z, planeBase: p }));
              setZebraBasePicking(false);
            },
            onMeasurePick: measurePick,
            onPmiClick: (id) => selectPmiRef.current(id),
            onDatumPick: (p) => datumPickRef.current(p),
            onScalarHover: setScalarHover,
            onComparisonHover: setCompareHover
          },
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
  }, [data, kinds, toggle, pickGroup, boxPick, measurePick]);

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
  // 基準点を選べるのはゼブラの平面の光源を表示しているときだけ。
  const basePickingOn = zebraBasePicking && analysisMode === 'zebra' && zebra.light === 'plane';
  useEffect(() => {
    sceneRef.current?.setZebraBasePicking(basePickingOn);
  }, [basePickingOn, status]);
  // データムを拾えるのは位置合わせの欄を開いているときだけ。印も開いているときだけ出す。
  const datumPickingOn = alignOpen && datumPicking !== null;
  useEffect(() => {
    sceneRef.current?.setDatumPicking(datumPickingOn ? datumKind : null);
  }, [datumPickingOn, datumKind, status]);
  useEffect(() => {
    sceneRef.current?.setDatumOverlay(alignOpen ? DATUM_NAMES.flatMap((n) => (datums[n] ? [{ name: datumLabel(n, t), datum: datums[n]! }] : [])) : []);
  }, [alignOpen, datums, status, t]);
  useEffect(() => {
    sceneRef.current?.setOriginAxesVisible(originAxes);
  }, [originAxes, status]);
  useEffect(() => {
    sceneRef.current?.setHoleAngle(holeAngle);
  }, [holeAngle, status]);
  useEffect(() => {
    sceneRef.current?.setHoleSizeTolerance(holeSizeTol);
  }, [holeSizeTol, status]);
  useEffect(() => {
    sceneRef.current?.setMeasureStep(pending.length);
  }, [pending, status]);
  useEffect(() => {
    sceneRef.current?.setPmiLabels(
      pmiLabels
        ? pmiItems.map((p) => ({ id: p.id, kind: p.kind, text: p.text, targets: pmiTargets(p), graphic: p.graphic, graphicOnly: p.graphicOnly }))
        : []
    );
  }, [pmiLabels, pmiItems, status]);
  // タイムアウトの残り（残りが TIMEOUT_WARN_SECONDS を切ったら出す。過ぎると backend がビューアを閉じる）。
  useEffect(() => {
    if (!deadline.current) return;
    const tick = () => {
      const left = Math.max(0, Math.round((deadline.current - Date.now()) / 1000));
      setRemaining(left <= TIMEOUT_WARN_SECONDS ? left : null);
    };
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    sceneRef.current?.setLocalAxis(localAxis);
  }, [localAxis, status]);
  useEffect(() => {
    sceneRef.current?.setVisibility({ ...kindVisible, hiddenParts, hiddenItems, swapped: hiddenSpace, model: modelVisible });
  }, [kindVisible, hiddenParts, hiddenItems, hiddenSpace, modelVisible, status]);
  useEffect(() => {
    sceneRef.current?.setColorOverrides(colors);
  }, [colors, status]);
  useEffect(() => {
    sceneRef.current?.setModelColor(hasRefModel ? modelColor : null);
  }, [hasRefModel, modelColor, status]);
  useEffect(() => {
    sceneRef.current?.setDiffVisible(diffVisible);
  }, [diffVisible, status]);
  useEffect(() => {
    sceneRef.current?.setSurfaceDisplay(surfaceDisplay);
    if (surfaceDisplay.mode !== 'scalar') setScalarHover(null);
  }, [surfaceDisplay, status]);
  useEffect(() => {
    // 「面」のチェックを外したら ②③④ もワイヤー表示（それぞれの色の線）にし、「エッジ」の表示に合わせて折れ目の線も描く。
    comparisons.forEach((_, i) =>
      sceneRef.current?.setComparisonOverlay(i, {
        visible: compareVisible[i],
        opacity: compareOpacity,
        color: compareColors[i],
        wire: !kindVisible.face,
        edges: kindVisible.edge
      })
    );
  }, [comparisons, compareVisible, compareOpacity, compareColors, kindVisible.face, kindVisible.edge, status]);
  useEffect(() => {
    sceneRef.current?.setDeviationDisplay(deviationOn ? { min: effDevRange.min, max: effDevRange.max, tolerance } : null);
    if (!deviationOn) setCompareHover(null);
  }, [deviationOn, effDevRange, tolerance, status]);
  useEffect(() => {
    sceneRef.current?.setSplit(split, paneModels);
  }, [split, paneModels, status]);
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

  const hoverText = useMemo(() => {
    // 縫合で穴・外周の一覧が変わったら（topologyVersion）描き直す。
    void topologyVersion;
    if (pivotPicking) return t('cad.hintPivot');
    if (basePickingOn) return t('cad.hintZebraBase');
    if (datumPickingOn && datumPicking) return t(datumKind === 'hole' ? 'cad.hintDatumHole' : 'cad.hintDatumPoint', { name: datumLabel(datumPicking, t) });
    let item = hover ? describe(itemsOf(data, hover.kind)[hover.id], label(hover), effectiveColor(data, colors, hover), t, data, localAxis) : '';
    if (hover?.kind === 'hole') {
      const n = sceneRef.current?.holeGroupOf(hover.id).length ?? holeGroup(data, hover.id, holeAngle, holeSizeTol).length;
      item +=
        holeSizeTol === null
          ? `  ${t('cad.holeGroup', { count: n, deg: fmt(holeAngle) })}`
          : `  ${t('cad.holeGroupSize', { count: n, deg: fmt(holeAngle), size: fmt(holeSizeTol) })}`;
    }
    if (measureTool) {
      if (measureError) return t(`cad.measure.error.${measureError}` as MessageKey);
      const hint = t(`cad.measure.hint.${measureTool}` as MessageKey, { n: pending.length + 1 });
      return item ? `${hint}  ${item}` : hint;
    }
    if (!item && mode === 'hole' && boxMode === 'off') return t('cad.hintHole', { deg: fmt(holeAngle) });
    if (!item && mode === 'outer' && boxMode === 'off') return t('cad.hintOuter');
    // 曲率・偏差の表示中は、マウスの下の値（曲率は曲率半径も）を先に出す。
    if (compareHover && deviationOn) {
      const v = compareHover.value;
      const value = t('cad.deviation.value', { mark: MODEL_MARKS[compareHover.model] ?? '', value: `${v > 0 ? '+' : ''}${fmtNum(v)}` });
      return item ? `${value}  ${item}` : value;
    }
    if (scalarHover !== null && analysisMode === 'curvature') {
      const value = t('cad.curvature.value', {
        value: fmtNum(scalarHover),
        radius: Math.abs(scalarHover) > 1e-9 && curvatureKind !== 'gauss' ? fmtNum(1 / Math.abs(scalarHover)) : '∞'
      });
      return item ? `${value}  ${item}` : value;
    }
    return item || t(boxMode === 'off' ? 'cad.hint' : 'cad.hintBox');
  }, [hover, data, label, t, colors, boxMode, pivotPicking, basePickingOn, datumPickingOn, datumPicking, datumKind, measureTool, measureError, pending, localAxis, holeAngle, holeSizeTol, mode, topologyVersion, scalarHover, analysisMode, curvatureKind, compareHover, deviationOn]);

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

  // 連続する面: 選んでいる面（穴・外周を選んでいればその周りの面、部品ならその面）とつながった面（面のまとまり。
  // 縫合・トポロジートレランスでの突き合わせを含む）の、見えている面をすべて選ぶ。
  const startFaces = useMemo(() => {
    // 縫合で穴・外周の周りの面が変わったら（topologyVersion）求め直す。
    void topologyVersion;
    const out = new Set<number>();
    for (const s of selection) {
      if (s.kind === 'face') out.add(s.id);
      else if (s.kind === 'hole' || s.kind === 'outer') {
        const fs = itemsOf(data, s.kind)[s.id]?.faces;
        if (Array.isArray(fs)) for (const f of fs as number[]) out.add(f);
      } else if (s.kind === 'part') data.faces.forEach((f, i) => f.part === s.id && out.add(i));
    }
    return out;
  }, [selection, data, topologyVersion]);
  const canConnected = mode === 'face' && startFaces.size > 0;
  // 折れ角の閾値は、隣り合う面の折れ角があるデータ（縫合で求め直しても入る）のときだけ使える。
  const faceAngles = hasFaceAngles(data);
  const selectConnected = () => {
    const visible = new Set(sceneRef.current?.selectableIds('face') ?? []);
    boxPick(
      connectedFaces(data, startFaces, faceAngles && connectedAngleOn ? connectedAngle : null)
        .filter((id) => visible.has(id))
        .map((id) => ({ kind: 'face' as const, id })),
      false
    );
  };

  // 縫合: 縫合トレランスで穴・外周・面のまとまりを求め直す（app.py の cad_viewer_sew）。穴・外周の ID が変わるので、
  // 選んでいた・隠していた穴・外周は外す（元に戻すの履歴からも）。
  const sew = async (tolerance: number) => {
    if (!saveId || sewState.kind === 'sewing' || !Number.isFinite(tolerance) || tolerance < 0) return;
    setSewState({ kind: 'sewing' });
    const action: IAction = {
      id: '',
      name: 'cad_viewer_sew',
      payload: { save_id: saveId, tolerance },
      label: '',
      tooltip: '',
      forId: '',
      onClick: () => {}
    };
    try {
      const res = await client.callAction(action, sessionId);
      const r = res?.response;
      if (!r?.ok) {
        setSewState({ kind: 'error', message: String(r?.error ?? t('cad.sewFailed')) });
        return;
      }
      replaceTopology(data, r as CadTopology);
      const cur = history.rewrite(withoutLoops);
      setSelection(cur.selection);
      setHiddenItems(cur.hiddenItems);
      // 非表示領域に入っている間に縫合したときは、入る前の選択（戻るときに戻す）からも古い ID の穴・外周を外す。
      stashedSelection.current = stashedSelection.current.filter((s) => s.kind !== 'hole' && s.kind !== 'outer');
      setHover(null);
      sceneRef.current?.reloadTopology();
      sewnRef.current = true;
      setAppliedSew(data.holes?.sewTolerance ?? 0);
      setTopologyVersion((v) => v + 1);
      setSewState({ kind: 'idle' });
      if ((mode === 'hole' && !data.holeList?.length) || (mode === 'outer' && !data.outerList?.length))
        setMode(kinds.includes('face') ? 'face' : kinds[0]);
    } catch (err) {
      setSewState({ kind: 'error', message: err instanceof Error ? err.message : t('cad.sewFailed') });
    }
  };
  const sewText =
    sewState.kind === 'sewing'
      ? t('cad.sewing')
      : sewState.kind === 'error'
        ? `${t('cad.sewFailed')} ${sewState.message}`
        : appliedSew > 0
          ? t('cad.sewn', { tol: fmt(appliedSew), holes: data.holeList?.length ?? 0, outers: data.outerList?.length ?? 0 })
          : '';

  // 表示・非表示（CAD の「表示/非表示」と「表示/非表示空間の切り替え」）。
  const hiddenCount = hiddenItems.size + hiddenParts.size;
  /** 選んでいる要素を隠す（部品は部品ごと、ほかは要素ごと）。隠したものは選択から外す。 */
  const hideSelected = () => {
    if (!selection.length) return;
    const parts = new Set(hiddenParts);
    const items = new Set(hiddenItems);
    for (const s of selection) {
      if (s.kind === 'part') parts.add(s.id);
      else items.add(selectionKey(s.kind, s.id));
    }
    if (parts.size !== hiddenParts.size) setHiddenParts(parts);
    if (items.size !== hiddenItems.size) setHiddenItems(items);
    setSelection([]);
  };
  const enterHiddenSpace = () => {
    stashedSelection.current = selection;
    setSelection([]);
    setHiddenSpace(true);
    setMeasureOpen(false);
    chooseMeasureTool(null);
    setPivotPicking(false);
  };
  /** 元の画面に戻る。入る前の選択に、表示に戻した要素（shown）を加えて選ぶ。 */
  const leaveHiddenSpace = (shown: CadSelection[] = []) => {
    const keep = stashedSelection.current;
    stashedSelection.current = [];
    const have = new Set(keep.map((s) => selectionKey(s.kind, s.id)));
    setSelection([...keep, ...shown.filter((s) => !have.has(selectionKey(s.kind, s.id)))]);
    setHiddenSpace(false);
  };
  /**
   * 非表示領域で選んだ要素を表示に戻して、元の画面に戻る。部品ごと隠した要素は、その部品ごと表示に戻す。
   * 穴・外周は周りの面がすべて隠れていると隠れるので、周りの面も戻す。
   */
  const showSelected = () => {
    const parts = new Set(hiddenParts);
    const items = new Set(hiddenItems);
    const unhide = (s: CadSelection) => {
      if (s.kind === 'part') {
        parts.delete(s.id);
        return;
      }
      items.delete(selectionKey(s.kind, s.id));
      const p = partOf(data, s);
      if (p !== null) parts.delete(p);
      const info = itemsOf(data, s.kind)[s.id];
      if ((s.kind === 'hole' || s.kind === 'outer') && Array.isArray(info?.faces))
        for (const f of info.faces as number[]) unhide({ kind: 'face', id: f });
      // 部品の番号を持たない頂点は、つながるエッジの部品で隠れる。
      if (s.kind === 'vertex' && p === null && Array.isArray(info?.edges))
        for (const e of info.edges as number[]) {
          const ep = partOf(data, { kind: 'edge', id: e });
          if (ep !== null) parts.delete(ep);
        }
    };
    selection.forEach(unhide);
    if (parts.size !== hiddenParts.size) setHiddenParts(parts);
    if (items.size !== hiddenItems.size) setHiddenItems(items);
    leaveHiddenSpace(selection);
  };
  const showAll = () => {
    if (hiddenParts.size) setHiddenParts(new Set());
    if (hiddenItems.size) setHiddenItems(new Set());
    leaveHiddenSpace();
  };

  // Esc: 通常以外のモード（非表示領域・計測・回転中心・範囲選択）なら通常に戻し、通常のときは全選択解除。
  // 何も選んでいなければ拡大表示を戻す。ポインタがビューアの上にあるか、ビューアの中にフォーカスがあるとき
  // （拡大表示中はいつでも）だけ効き、文字の入力欄の中では何もしない。計測の途中の点を捨てる Esc は先に効く（上の capture）。
  const onEscape = useRef<(e: KeyboardEvent) => void>(() => {});
  onEscape.current = (e: KeyboardEvent) => {
    const bar = barRef.current;
    const target = e.target as HTMLElement | null;
    if (!bar || closing) return;
    if (!(expanded || pointerInside.current || (target && bar.contains(target)))) return;
    if (target && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))) return;
    if (hiddenSpace) leaveHiddenSpace();
    else if (measureOpen || measureTool) {
      setMeasureOpen(false);
      chooseMeasureTool(null);
    } else if (pivotPicking) setPivotPicking(false);
    else if (basePickingOn) setZebraBasePicking(false);
    else if (datumPickingOn) setDatumPicking(null);
    else if (boxMode !== 'off') setBoxMode('off');
    else if (selection.length) setSelection([]);
    else if (expanded) setExpanded(false);
    else return;
    e.preventDefault();
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !e.isComposing) onEscape.current(e);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const colorChanges = (): CadColorChanges => {
    const changes: CadColorChanges = { faces: {}, parts: {} };
    colors.faces.forEach((hex, id) => (changes.faces[String(id)] = hex));
    colors.parts.forEach((hex, id) => (changes.parts[String(id)] = hex));
    return changes;
  };
  const viewState = (): CadViewState => {
    const h = history.entries();
    const saved = serializeHistory(data, h.past, h.future, { selection, colors, hiddenParts, hiddenItems, kindVisible, attributeEdits });
    return {
      hidden_parts: [...hiddenParts],
      visible: kindVisible,
      ...(Object.keys(attributeEdits).length ? { attributes: attributeEdits } : {}),
      ...(saved ? { history: saved } : {}),
      ...(localAxis ? { local_axis: localAxis.id } : {}),
      ...(sewnRef.current ? { sewn: true } : {}),
      ...(hasReference ? { deviation_tolerance: tolerance } : {})
    };
  };
  const cancel = () => {
    if (closing) return;
    setClosing('cancel');
    onCancel(colorChanges(), viewState());
  };
  const swap = (model: number) => {
    if (closing) return;
    setClosing('swap');
    onSwap(model);
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
      if (r?.ok)
        setSaveState({
          kind: 'saved',
          path: String(r.path ?? ''),
          // 書き出しの注意書き（PMI を書けなかった・DXF では面の色を書けない等）。黙って欠けたファイルにしないよう出す。
          notes: Array.isArray(r.notes) ? r.notes.map((n: unknown) => String(n)).filter(Boolean) : []
        });
      else setSaveState({ kind: 'error', message: String(r?.error ?? t('cad.saveFailed')) });
    } catch (err) {
      setSaveState({ kind: 'error', message: err instanceof Error ? err.message : t('cad.saveFailed') });
    }
  };
  // 比較モデルの ① への位置合わせをやり直す（app.py の cad_viewer_align）。偏差の値は返ってきた ID で取りに行く。
  const selectedFaceIds = useMemo(() => selection.filter((s) => s.kind === 'face').map((s) => s.id), [selection]);
  const alignModels = useMemo(
    () => comparisons.flatMap((c, i) => (usable[i] && c.deviation?.values ? [i] : [])),
    [comparisons, usable]
  );
  const runAlign = async (method: 'bestfit' | 'faces' | 'datum' | 'reset' | 'initial') => {
    if (!saveId || alignState.kind === 'busy') return;
    setAlignState({ kind: 'busy' });
    const models = alignTarget >= 0 ? [alignTarget] : alignModels;
    const action: IAction = {
      id: '',
      name: 'cad_viewer_align',
      payload: { save_id: saveId, models, method, faces: selectedFaceIds, datums, datum_mode: datumMode, pad_radius: padRadius, tolerance },
      label: '',
      tooltip: '',
      forId: '',
      onClick: () => {}
    };
    try {
      const res = await client.callAction(action, sessionId);
      const r = res?.response;
      if (!r?.ok) {
        setAlignState({ kind: 'error', message: String(r?.error ?? t('cad.align.failedUnknown')) });
        return;
      }
      type AlignResultPayload = { model: number; values_id: string; transform?: { matrix?: unknown }; alignment?: CadAlignmentInfo; stats?: CadDeviationStats };
      const results = (Array.isArray(r.results) ? r.results : []) as AlignResultPayload[];
      const fetched = await Promise.all(results.map((x) => fetchCadFloat32(BACKEND_URL, String(x.values_id))));
      const valuesNext = [...deviationValues];
      const infosNext = [...alignInfos];
      const statsNext = [...alignStatsList];
      results.forEach((x, k) => {
        if (!(x.model >= 0 && x.model < comparisons.length)) return;
        valuesNext[x.model] = fetched[k];
        infosNext[x.model] = x.alignment ?? null;
        statsNext[x.model] = x.stats ?? null;
        sceneRef.current?.setComparisonValues(x.model, fetched[k]);
        const matrix = x.transform?.matrix;
        if (isMatrix4(matrix)) sceneRef.current?.setComparisonOverlay(x.model, { matrix });
      });
      setDeviationValues(valuesNext);
      setAlignInfos(infosNext);
      setAlignStatsList(statsNext);
      // 一部のモデルだけ合わせられなかったときは、その理由を出す。
      const errors = (Array.isArray(r.errors) ? r.errors : []) as { model: number; error: string }[];
      setAlignState(
        errors.length
          ? { kind: 'error', message: errors.map((e) => `${MODEL_MARKS[e.model + 1] ?? ''} ${e.error}`).join(' / ') }
          : { kind: 'idle' }
      );
      // 合わせたら偏差の色を見せる。
      setAnalysisMode('deviation');
    } catch (err) {
      setAlignState({ kind: 'error', message: err instanceof Error ? err.message : t('cad.align.failedUnknown') });
    }
  };
  // データムを拾い始める（null でやめる）。ほかのクリックで点を拾うモード（計測・回転中心・ゼブラの基準点）はやめる。
  const pickDatum = (name: DatumName | null) => {
    setDatumPicking(name);
    if (name) {
      chooseMeasureTool(null);
      setPivotPicking(false);
      setZebraBasePicking(false);
    }
  };
  const setPin = (name: DatumName, pin: number) =>
    setDatums((cur) => {
      const d = cur[name];
      return d?.type === 'hole' ? { ...cur, [name]: { ...d, pin } } : cur;
    });

  const saveText =
    saveState.kind === 'saved'
      ? saveState.notes.length
        ? t('cad.savedWithNotes', { path: saveState.path, notes: saveState.notes.join(' / ') })
        : t('cad.saved', { path: saveState.path })
      : saveState.kind === 'error'
        ? `${t('cad.saveFailed')} ${saveState.message}`
        : '';

  const summary = `${data.source.format ?? ''}${data.source.path ? ' ' + data.source.path.split(/[\\/]/).pop() : ''}  ${data.bbox.size
    .map(fmt)
    .join(' × ')}${data.units !== 'unknown' ? ' ' + data.units : ''}`;

  // 左のサイドバーの「選択」タブ: 選んでいる要素のタグ（数万件になりうるので、3D 表示の下ではなくパネルに出す）。
  const selectionPanel = (
    <div className="cad-viewer-selected">
      {selection.length === 0 ? (
        <span className="cad-viewer-empty">{t('cad.noneSelected')}</span>
      ) : (
        <>
          <button type="button" className="cad-viewer-clear" onClick={() => setSelection([])}>
            {t('cad.clear')}
          </button>
          {selection.map((s) => (
            <span
              key={`${s.kind}-${s.id}`}
              className="cad-viewer-tag"
              title={describe(itemsOf(data, s.kind)[s.id], label(s), effectiveColor(data, colors, s), t, data, localAxis)}
            >
              {label(s)}
              <button type="button" aria-label={t('cad.remove')} onClick={() => toggle(s)}>
                ×
              </button>
            </span>
          ))}
        </>
      )}
    </div>
  );
  // 右のパネル（計測・解析表示・位置合わせの設定）は、どれかを開いているときだけ出す。
  const toolsOpen = measureOpen || analysisMode !== 'normal' || (alignOpen && hasReference);
  const closeMeasure = () => {
    setMeasureOpen(false);
    chooseMeasureTool(null);
  };

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
          {kinds.includes('face') && (
            <button
              type="button"
              className="cad-viewer-chip"
              title={t('cad.connectedFacesTitle')}
              disabled={!canConnected}
              onClick={selectConnected}
            >
              {t('cad.connectedFaces')}
            </button>
          )}
          {kinds.includes('face') && faceAngles && (
            <label className="cad-viewer-chip cad-viewer-hole-size" title={t('cad.connectedAngleTitle')}>
              <input
                type="checkbox"
                checked={connectedAngleOn}
                aria-label={t('cad.connectedAngleToggle')}
                onChange={(e) => setConnectedAngleOn(e.target.checked)}
              />
              {t('cad.connectedAngle')}
              <NumberInput
                min={0}
                max={180}
                step={1}
                value={connectedAngle}
                disabled={!connectedAngleOn}
                aria-label={t('cad.connectedAngleTitle')}
                onChange={setConnectedAngle}
              />
              °
            </label>
          )}
          <span className="cad-viewer-sep" />
          {hiddenSpace ? (
            <>
              <button
                type="button"
                className="cad-viewer-chip active"
                title={t('cad.showSelectedTitle')}
                disabled={selection.length === 0}
                onClick={showSelected}
              >
                {t('cad.show')}
              </button>
              <button type="button" className="cad-viewer-chip" title={t('cad.showAllTitle')} disabled={hiddenCount === 0} onClick={showAll}>
                {t('cad.showAll')}
              </button>
              <button type="button" className="cad-viewer-chip" title={t('cad.showBackTitle')} onClick={() => leaveHiddenSpace()}>
                {t('cad.showBack')}
              </button>
            </>
          ) : (
            <>
              <button type="button" className="cad-viewer-chip" title={t('cad.hideTitle')} disabled={selection.length === 0} onClick={hideSelected}>
                {t('cad.hide')}
              </button>
              <button type="button" className="cad-viewer-chip" title={t('cad.showTitle')} disabled={hiddenCount === 0} onClick={enterHiddenSpace}>
                {t('cad.show')}
              </button>
            </>
          )}
          {mode === 'hole' && (
            <label className="cad-viewer-chip cad-viewer-hole-angle" title={t('cad.holeAngleTitle')}>
              {t('cad.holeAngle')}
              <NumberInput min={0} max={90} step={0.5} value={holeAngle} aria-label={t('cad.holeAngleTitle')} onChange={setHoleAngle} />
              °
            </label>
          )}
          {mode === 'hole' && (
            <label className="cad-viewer-chip cad-viewer-hole-size" title={t('cad.holeSizeTitle')}>
              <input
                type="checkbox"
                checked={holeSizeOn}
                aria-label={t('cad.holeSizeToggle')}
                onChange={(e) => setHoleSizeOn(e.target.checked)}
              />
              {t('cad.holeSize')}
              <NumberInput min={0} step={0.1} value={holeSize} disabled={!holeSizeOn} aria-label={t('cad.holeSizeTitle')} onChange={setHoleSize} />
            </label>
          )}
          {canSew && (mode === 'hole' || mode === 'outer') && (
            <span className="cad-viewer-chip cad-viewer-sew" title={sewText || t('cad.sewTitle')}>
              {t('cad.sewTol')}
              <NumberInput
                min={0}
                step={0.1}
                value={sewTol}
                aria-label={t('cad.sewTitle')}
                disabled={sewState.kind === 'sewing' || !!closing}
                onChange={setSewTol}
                onEnter={(v) => void sew(v)}
              />
              <button type="button" className="cad-viewer-chip" disabled={sewState.kind === 'sewing' || !!closing} onClick={() => void sew(sewTol)}>
                {sewState.kind === 'sewing' ? t('cad.sewing') : t('cad.sew')}
              </button>
              {appliedSew > 0 && (
                <button type="button" className="cad-viewer-chip" title={t('cad.unsewTitle')} disabled={sewState.kind === 'sewing' || !!closing} onClick={() => void sew(0)}>
                  {t('cad.unsew')}
                </button>
              )}
            </span>
          )}
        </div>
        <div className="cad-viewer-views">
          {localAxes.length > 0 && (
            <label className="cad-viewer-chip cad-viewer-coords" title={t('cad.coordsTitle')}>
              {t('cad.coords')}
              <select
                value={localAxisId === null ? '' : String(localAxisId)}
                aria-label={t('cad.coords')}
                onChange={(e) => {
                  const id = e.target.value === '' ? null : Number(e.target.value);
                  setLocalAxisId(id);
                  // 切り替えたら、その座標系の斜めの向きから見る（視点のボタンもその座標系になる）。
                  const axis = id === null ? null : (localAxes[id] ?? null);
                  sceneRef.current?.setLocalAxis(axis);
                  sceneRef.current?.setView('iso');
                }}
              >
                <option value="">{t('cad.coords.world')}</option>
                {localAxes.map((a) => (
                  <option key={a.id} value={String(a.id)}>
                    {t('cad.coords.local', { id: a.id + 1 })}
                    {a.rightHanded === false ? t('cad.coords.leftHanded') : ''}
                  </option>
                ))}
              </select>
            </label>
          )}
          {VIEWS.map((v) => (
            <button
              key={v}
              type="button"
              className="cad-viewer-chip cad-viewer-view"
              title={t(`cad.view.${v}` as MessageKey) + (localAxis ? " (X' Y' Z')" : '')}
              onClick={() => sceneRef.current?.setView(v)}
            >
              {localAxis ? `${v.toUpperCase()}'` : v.toUpperCase()}
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
            onClick={() => {
              setPivotPicking((x) => !x);
              setZebraBasePicking(false);
              setDatumPicking(null);
            }}
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
          {pmiItems.length > 0 && (
            <button
              type="button"
              className={`cad-viewer-chip${pmiLabels ? ' active' : ''}`}
              aria-pressed={pmiLabels}
              title={t('cad.pmiLabelsTitle')}
              onClick={() => setPmiLabels((x) => !x)}
            >
              {t('cad.pmiLabels')}
            </button>
          )}
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
          {(['zebra', 'curvature', 'deviation'] as const).map((m) =>
            m === 'curvature' && !hasCurvature ? null : m === 'deviation' && !hasReference ? null : (
              <button
                key={m}
                type="button"
                className={`cad-viewer-chip${analysisMode === m ? ' active' : ''}`}
                aria-pressed={analysisMode === m}
                title={t(`cad.analysis.${m}Title` as MessageKey)}
                onClick={() => setAnalysisMode((cur) => (cur === m ? 'normal' : m))}
              >
                {t(`cad.analysis.${m}` as MessageKey)}
              </button>
            )
          )}
          {hasReference && (
            <button
              type="button"
              className={`cad-viewer-chip${alignOpen ? ' active' : ''}`}
              aria-pressed={alignOpen}
              title={t('cad.alignTitle')}
              onClick={() => setAlignOpen((x) => !x)}
            >
              {t('cad.align')}
            </button>
          )}
          {splitLayouts.length > 1 && (
            <select
              className="cad-viewer-chip cad-viewer-split-select"
              value={split}
              title={t('cad.splitTitle')}
              aria-label={t('cad.split')}
              onChange={(e) => setSplit(e.target.value as SplitLayout)}
            >
              {splitLayouts.map((l) => (
                <option key={l} value={l}>
                  {l === 'single' ? `${t('cad.split')}: ${t('cad.split.single')}` : t(`cad.split.${l}` as MessageKey)}
                </option>
              ))}
            </select>
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
      <div className="cad-viewer-body">
        {sidebarOpen && (
          <CadSidebar
            data={data}
            kinds={kinds}
            selection={selection}
            selectionPanel={selectionPanel}
            kindVisible={kindVisible}
            onKindVisible={(k, v) => setKindVisible((cur) => ({ ...cur, [k]: v }))}
            hiddenParts={hiddenParts}
            onHiddenParts={setHiddenParts}
            colors={colors}
            onColors={(c) => setColors(dropUnchangedPartColors(data, c))}
            onTogglePart={(id) => toggle({ kind: 'part', id })}
            onHoverPart={(id) => sceneRef.current?.setExternalHover(id === null ? null : { kind: 'part', id })}
            attributeEdits={attributeEdits}
            onAttributeEdits={setAttributeEdits}
            compare={
              hasRefModel
                ? {
                    modelName: data.source.path ?? data.source.format ?? '',
                    modelVisible,
                    onModelVisible: setModelVisible,
                    modelColor,
                    onModelColor: setModelColor,
                    others: comparisons.map((c, i) => ({
                      name: c.source?.path ?? c.source?.format ?? '',
                      visible: compareVisible[i],
                      onVisible: (v: boolean) => setCompareVisible((cur) => cur.map((x, k) => (k === i ? v : x))),
                      color: compareColors[i],
                      onColor: (hex: string) => setCompareColors((cur) => cur.map((x, k) => (k === i ? hex : x))),
                      ...(usable[i] ? {} : { error: c.error ?? '' })
                    })),
                    opacity: compareOpacity,
                    onOpacity: setCompareOpacity,
                    // 入れ替えは backend で作り直すので、位置合わせを受け付けるビューア（① からの偏差がある）だけ。
                    onSwap: canAlign && saveId && !closing ? swap : undefined
                  }
                : undefined
            }
            onSelectItems={(items) => {
              // PMI のクリック: 指す要素を選び、そこへ寄せる（選べない種類の要素も、寄せる範囲には含める）。
              boxPick(items.filter((s) => kinds.includes(s.kind)), false);
              sceneRef.current?.fitItems(items);
            }}
          />
        )}
        <div className={`cad-viewer-host${hiddenSpace ? ' cad-viewer-hidden-space' : ''}`} ref={hostRef}>
          {hiddenSpace && status === 'ready' && (
            <div className="cad-viewer-hidden-banner" role="status">
              {hiddenCount ? t('cad.hiddenSpace', { count: hiddenCount }) : t('cad.hiddenSpaceEmpty')}
            </div>
          )}
          {status === 'ready' && split !== 'single' && (
            // 画面分割の枠と、画面ごとに映すモデルの選択（3D 表示は CadScene が 1 つの canvas に分けて描く）。
            <div
              className="cad-viewer-panes"
              style={{
                gridTemplateColumns: `repeat(${splitGrid(split).cols}, 1fr)`,
                gridTemplateRows: `repeat(${splitGrid(split).rows}, 1fr)`
              }}
            >
              {Array.from({ length: splitGrid(split).cols * splitGrid(split).rows }, (_, i) => (
                <div key={i} className="cad-viewer-pane">
                  <select
                    className="cad-viewer-pane-model"
                    value={paneModels[i] ?? 0}
                    title={t('cad.split.paneModel')}
                    aria-label={t('cad.split.paneModel')}
                    style={{ borderColor: (paneModels[i] ?? 0) === 0 ? modelColor : compareColors[(paneModels[i] ?? 1) - 1] }}
                    onChange={(e) => {
                      const m = Number(e.target.value);
                      setPaneModels((cur) => cur.map((x, k) => (k === i ? m : x)));
                    }}
                  >
                    {shownModels.map((m) => {
                      const src = m === 0 ? data.source.path : comparisons[m - 1]?.source?.path;
                      const name = (src ?? '').split(/[\\/]/).pop() || t(m === 0 ? 'cad.side.compareModel' : 'cad.side.compareReference', { mark: MODEL_MARKS[m] });
                      return (
                        <option key={m} value={m}>
                          {MODEL_MARKS[m]} {name}
                        </option>
                      );
                    })}
                  </select>
                </div>
              ))}
            </div>
          )}
          {status === 'ready' && (deviationOn || surfaceDisplay.mode === 'scalar') && (
            <ScalarLegend
              title={deviationOn ? t('cad.deviation.legend') : t(`cad.curvature.${curvatureKind}` as MessageKey)}
              min={deviationOn ? effDevRange.min : surfaceDisplay.mode === 'scalar' ? surfaceDisplay.scalar.min : 0}
              max={deviationOn ? effDevRange.max : surfaceDisplay.mode === 'scalar' ? surfaceDisplay.scalar.max : 1}
              tolerance={deviationOn ? tolerance : surfaceDisplay.mode === 'scalar' ? surfaceDisplay.scalar.tolerance : 0}
              scheme={deviationOn ? 'deviation' : surfaceDisplay.mode === 'scalar' ? surfaceDisplay.scalar.scheme : 'rainbow'}
            />
          )}
          {status === 'loading' && <div className="cad-viewer-overlay">{t('cad.loading')}</div>}
          {status === 'error' && <div className="cad-viewer-overlay">{t('cad.webglError')}</div>}
          {closing && (
            <div className="cad-viewer-overlay cad-viewer-closing" role="status">
              <span className="cad-viewer-spinner" aria-hidden="true" />
              {t(closing === 'submit' ? 'cad.submitting' : closing === 'swap' ? 'cad.side.swapping' : 'cad.closing')}
            </div>
          )}
        </div>
        {toolsOpen && (
          // 右のパネル: 計測・解析表示（ゼブラ・曲率・偏差）・位置合わせの設定。開いているものだけを縦に並べる。
          <aside className="cad-sidebar cad-tools-panel" aria-label={t('cad.tools')}>
            {measureOpen && (
              <section>
                <h4>
                  {t('cad.measure')}
                  <button type="button" className="cad-tools-close" aria-label={t('cad.tools.close')} title={t('cad.tools.close')} onClick={closeMeasure}>
                    ×
                  </button>
                </h4>
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
                {measurements.length > 0 && (
                  <div className="cad-viewer-measures">
                    {measurements.map((m, i) => (
                      <span key={i} className="cad-viewer-measure-tag" title={measureTitle(m, t, localAxis)}>
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
              </section>
            )}
            {analysisMode !== 'normal' && (
              <section>
                <h4>
                  {t(`cad.analysis.${analysisMode}` as MessageKey)}
                  <button
                    type="button"
                    className="cad-tools-close"
                    aria-label={t('cad.tools.close')}
                    title={t('cad.tools.close')}
                    onClick={() => setAnalysisMode('normal')}
                  >
                    ×
                  </button>
                </h4>
                <AnalysisSettingsRow
                  mode={analysisMode}
                  zebra={zebra}
                  onZebra={setZebra}
                  basePicking={basePickingOn}
                  onBasePicking={(on) => {
                    setZebraBasePicking(on);
                    if (on) {
                      setPivotPicking(false);
                      setDatumPicking(null);
                      chooseMeasureTool(null);
                      setBoxMode('off');
                    }
                  }}
                  curvatureKind={curvatureKind}
                  onCurvatureKind={(k) => {
                    setCurvatureKind(k);
                    setCurvRange(null);
                  }}
                  range={analysisMode === 'deviation' ? effDevRange : effCurvRange}
                  onRange={(r) => (analysisMode === 'deviation' ? setDevRange(r) : setCurvRange(r))}
                  onAutoRange={() => (analysisMode === 'deviation' ? setDevRange(null) : setCurvRange(null))}
                  linkable={analysisMode === 'deviation' || curvatureKind !== 'absmax'}
                  linked={rangeLinked}
                  onLinked={setRangeLinked}
                  tolerance={tolerance}
                  onTolerance={setTolerance}
                  units={data.units}
                />
              </section>
            )}
            {alignOpen && hasReference && (
              <section>
                <h4>
                  {t('cad.align')}
                  <button type="button" className="cad-tools-close" aria-label={t('cad.tools.close')} title={t('cad.tools.close')} onClick={() => setAlignOpen(false)}>
                    ×
                  </button>
                </h4>
                <AlignRow
                  canAlign={canAlign && !!saveId}
                  state={alignState}
                  results={alignModels.map((i) => ({ mark: MODEL_MARKS[i + 1], info: alignInfos[i], stats: alignStatsList[i] }))}
                  targets={alignModels.map((i) => ({ model: i, mark: MODEL_MARKS[i + 1], name: comparisons[i]?.source?.path ?? '' }))}
                  target={alignTarget}
                  onTarget={setAlignTarget}
                  datums={datums}
                  selectedFaces={selectedFaceIds.length}
                  overlayVisible={compareVisible.some((v, i) => v && usable[i])}
                  onOverlayVisible={(v) => setCompareVisible(usable.map((u) => u && v))}
                  overlayOpacity={compareOpacity}
                  onOverlayOpacity={setCompareOpacity}
                  datumPicking={datumPickingOn ? datumPicking : null}
                  onPickDatum={pickDatum}
                  onPin={setPin}
                  datumMode={datumMode}
                  onDatumMode={setDatumMode}
                  padRadius={padRadius}
                  onPadRadius={setPadRadius}
                  onClearDatums={() => {
                    setDatums({});
                    setDatumPicking(null);
                  }}
                  onAlign={(m) => void runAlign(m)}
                />
              </section>
            )}
          </aside>
        )}
      </div>
      <div className="cad-viewer-status">
        {remaining !== null && !closing && (
          <span className="cad-viewer-timeout" role="status">
            {t('cad.timeoutLeft', { min: Math.floor(remaining / 60), sec: String(remaining % 60).padStart(2, '0') })}
          </span>
        )}
        {sewText && (mode === 'hole' || mode === 'outer') && (
          <span className="cad-viewer-legend" title={sewText}>
            {sewText}
          </span>
        )}
        <span className="cad-viewer-hover" title={hoverText || undefined}>
          {hoverText}
        </span>
        {hasDiff && diffVisible && (
          <span className="cad-viewer-legend" title={t('cad.diffLegend')}>
            {t('cad.diffLegend')}
          </span>
        )}
        {comparisons.map((c, i) =>
          c.error ? (
            <span key={`cmp-error-${i}`} className="cad-viewer-legend cad-viewer-align-error" title={c.error}>
              {t('cad.deviation.error', { mark: MODEL_MARKS[i + 1], error: c.error })}
            </span>
          ) : null
        )}
        <span className="cad-viewer-summary">{summary}</span>
      </div>
      <textarea
        className="cad-viewer-message"
        rows={1}
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
            <span className={`cad-viewer-save-status${saveState.kind === 'error' || (saveState.kind === 'saved' && saveState.notes.length) ? ' error' : ''}`} title={saveText}>
              {saveText}
            </span>
          )}
          {saveId && (
            <button
              type="button"
              className="ask-form-cancel"
              title={t('cad.saveTitle')}
              disabled={saveState.kind === 'saving' || !!closing}
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
