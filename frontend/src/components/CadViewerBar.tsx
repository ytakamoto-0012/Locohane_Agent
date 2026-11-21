import { useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
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
import { CadSidebar } from './cad/CadSidebar';

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

/** 確定時に返す、変えた色（キーは ID の文字列）。 */
export interface CadColorChanges {
  faces: Record<string, string>;
  parts: Record<string, string>;
}

/** 画面のテーマ（ライト / ダーク、アクセント色）から 3D 表示の色を決める。 */
function sceneColors(el: HTMLElement): CadSceneColors {
  const style = getComputedStyle(el);
  const dark = document.documentElement.dataset.theme === 'dark';
  const accent = toHex(style.getPropertyValue('--accent').trim()) ?? '#6d5ef8';
  return {
    background: dark ? '#17171a' : '#f7f7f9',
    face: dark ? '#8d93a0' : '#b9bfcb',
    edge: dark ? '#e4e4ea' : '#2b2c31',
    vertex: dark ? '#f0f0f4' : '#1f2023',
    hover: '#f5a524',
    selected: accent
  };
}

function toHex(value: string): string | null {
  if (/^#[0-9a-f]{6}$/i.test(value)) return value;
  const m = value.match(/^rgba?\((\d+),\s*(\d+),\s*(\d+)/i);
  if (!m) return null;
  return '#' + [m[1], m[2], m[3]].map((v) => Number(v).toString(16).padStart(2, '0')).join('');
}

function fmt(n: unknown): string {
  return typeof n === 'number' ? String(Math.round(n * 1000) / 1000) : '';
}

/** ホバー中・選択中の要素の短い説明（例: 「面 #3 PLANE 面積 600 RGB(200, 0, 0) >Z」）。 */
function describe(item: CadItemInfo | undefined, label: string, color: string | null, t: (k: MessageKey) => string): string {
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
      onSubmit={(selected, colors, message, view, screenshot) =>
        askUser.callback({ submitted: true, selected, colors, message, ...view, ...(screenshot ? { screenshot } : {}) })
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
  /** screenshot: 指示があるときだけ撮った今の画面（PNG の data URL）。 */
  onSubmit: (selected: CadSelection[], colors: CadColorChanges, message: string, view: CadViewState, screenshot: string | null) => void;
  onCancel: (colors: CadColorChanges, view: CadViewState) => void;
}

/** 確定・閉じる・保存のときに返す表示の状態（backend は変更があればビューア用データに反映して自動で保存する）。 */
interface CadViewState {
  hidden_parts: number[];
  visible: { face: boolean; edge: boolean; vertex: boolean };
}

function initialHiddenParts(data: CadData): Set<number> {
  // 「保存」したデータは保存したときの表示に戻す。それ以外は、DXF で非表示・フリーズのレイヤーを
  // 最初から隠す（CAD で開いたときと同じ見え方）。
  if (data.viewState?.hiddenParts) return new Set(data.viewState.hiddenParts);
  return new Set((data.parts ?? []).filter((p) => p.layer && (p.layer.off || p.layer.frozen)).map((p) => p.id));
}

type SaveState = { kind: 'idle' } | { kind: 'saving' } | { kind: 'saved'; path: string } | { kind: 'error'; message: string };

function CadViewerPanel({ question, kinds, data, saveId, screenshotLongSide, onSubmit, onCancel }: PanelProps) {
  const { t } = useI18n();
  const client = useContext(ChainlitContext);
  const sessionId = useRecoilValue(sessionIdState);
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' });
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
  const hasParts = (data.parts?.length ?? 0) > 0;
  const [sidebarOpen, setSidebarOpen] = useState(hasParts);
  const [kindVisible, setKindVisible] = useState({ face: true, edge: true, vertex: true, ...data.viewState?.visible });
  const [hiddenParts, setHiddenParts] = useState<Set<number>>(() => initialHiddenParts(data));
  const [colors, setColors] = useState<CadColorOverrides>({ faces: new Map(), parts: new Map() });

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
          { onHover: setHover, onPick: toggle, onBoxPick: boxPick, onPivotSet: () => setPivotPicking(false) },
          sceneColors(host)
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
  }, [data, kinds, toggle, boxPick]);

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

  // 保存した後に色・表示を変えたら「保存しました」を消す（保存したファイルは前の内容のまま）。
  useEffect(() => {
    setSaveState((s) => (s.kind === 'saved' ? { kind: 'idle' } : s));
  }, [colors, hiddenParts, kindVisible]);

  // テーマ（ライト / ダーク・アクセント色）の切り替えに追従する。
  useEffect(() => {
    const observer = new MutationObserver(() => {
      if (hostRef.current) sceneRef.current?.applyTheme(sceneColors(hostRef.current));
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
    if (!hover) return t(boxMode === 'off' ? 'cad.hint' : 'cad.hintBox');
    return describe(itemsOf(data, hover.kind)[hover.id], label(hover), effectiveColor(data, colors, hover), t);
  }, [hover, data, label, t, colors, boxMode, pivotPicking]);

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
  const viewState = (): CadViewState => ({ hidden_parts: [...hiddenParts], visible: kindVisible });
  const submit = () => {
    const text = message.trim();
    // 指示があるときだけ、今の画面（選んだ要素の強調を含む）を低解像度で撮って AI へ一緒に見せる。
    const screenshot = text ? (sceneRef.current?.snapshot(screenshotLongSide) ?? null) : null;
    onSubmit(selection, colorChanges(), text, viewState(), screenshot);
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
    <div className={`ask-form-bar cad-viewer-bar${expanded ? ' cad-viewer-expanded' : ''}`}>
      {question && <div className="ask-form-question">{question}</div>}
      <div className="cad-viewer-toolbar">
        <div className="cad-viewer-modes" role="radiogroup" aria-label={t('cad.selectKind')}>
          {kinds.map((k) => (
            <button
              key={k}
              type="button"
              role="radio"
              aria-checked={mode === k}
              className={`cad-viewer-chip${mode === k ? ' active' : ''}`}
              onClick={() => setMode(k)}
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
              className={`cad-viewer-chip${boxMode === b.id ? ' active' : ''}`}
              onClick={() => setBoxMode(b.id)}
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
            kindVisible={kindVisible}
            onKindVisible={(k, v) => setKindVisible((cur) => ({ ...cur, [k]: v }))}
            hiddenParts={hiddenParts}
            onHiddenParts={setHiddenParts}
            colors={colors}
            onColors={setColors}
            onTogglePart={(id) => toggle({ kind: 'part', id })}
            onHoverPart={(id) => sceneRef.current?.setExternalHover(id === null ? null : { kind: 'part', id })}
          />
        )}
        <div className="cad-viewer-host" ref={hostRef}>
          {status === 'loading' && <div className="cad-viewer-overlay">{t('cad.loading')}</div>}
          {status === 'error' && <div className="cad-viewer-overlay">{t('cad.webglError')}</div>}
        </div>
      </div>
      <div className="cad-viewer-status">
        <span className="cad-viewer-hover">{hoverText}</span>
        <span className="cad-viewer-summary">{summary}</span>
      </div>
      <div className="cad-viewer-selected">
        {selection.length === 0 ? (
          <span className="cad-viewer-empty">{t('cad.noneSelected')}</span>
        ) : (
          selection.map((s) => (
            <span
              key={`${s.kind}-${s.id}`}
              className="cad-viewer-tag"
              title={describe(itemsOf(data, s.kind)[s.id], label(s), effectiveColor(data, colors, s), t)}
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
        <button type="button" className="ask-form-cancel" title={t('cad.cancelTitle')} onClick={() => onCancel(colorChanges(), viewState())}>
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
          <button type="button" className="ask-form-submit" title={t('cad.confirmTitle')} onClick={submit}>
            {t('cad.confirm', { count: selection.length })}
          </button>
        </div>
      </div>
    </div>
  );
}
