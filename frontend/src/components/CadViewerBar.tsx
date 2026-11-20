import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useChatData } from '@chainlit/react-client';
import { useI18n } from '../i18n';
import type { MessageKey } from '../i18n/messages';
import {
  CAD_DATA_FORMAT,
  itemsOf,
  sameSelection,
  type CadData,
  type CadItemInfo,
  type CadKind,
  type CadSelection,
  type CadViewerProps
} from './cad/cadData';
import type { CadScene, CadSceneColors, ViewName } from './cad/CadScene';

const KIND_LABEL: Record<CadKind, MessageKey> = {
  face: 'cad.kind.face',
  edge: 'cad.kind.edge',
  vertex: 'cad.kind.vertex',
  part: 'cad.kind.part'
};
const VIEWS: { id: ViewName; label: MessageKey }[] = [
  { id: 'iso', label: 'cad.view.iso' },
  { id: 'front', label: 'cad.view.front' },
  { id: 'top', label: 'cad.view.top' },
  { id: 'right', label: 'cad.view.right' }
];

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

/** ホバー中・選択中の要素の短い説明（例: 「面 #3 PLANE 面積 600 >Z」）。 */
function describe(item: CadItemInfo | undefined, label: string, t: (k: MessageKey) => string): string {
  if (!item) return label;
  const parts = [label];
  if (item.kind === 'part') parts.push(item.path ?? item.name ?? '');
  else if (item.geomType) parts.push(item.geomType);
  if (item.area !== undefined) parts.push(`${t('cad.info.area')} ${fmt(item.area)}`);
  if (item.length !== undefined) parts.push(`${t('cad.info.length')} ${fmt(item.length)}`);
  if (item.radius !== undefined) parts.push(`R ${fmt(item.radius)}`);
  if (item.point) parts.push(`(${item.point.map(fmt).join(', ')})`);
  if (item.free) parts.push(t('cad.info.free'));
  const sel = item.selectors?.[0] ?? item.meshHints?.[0];
  if (sel) parts.push(sel);
  return parts.filter(Boolean).join('  ');
}

/**
 * show_cad_viewer（src/tools/cad_viewer.py）専用の表示コンポーネント。
 * backend では cl.AskElementMessage（spec.type === 'element'）として届く。name が "CadViewer" の
 * カスタム要素の props（question / select / data）から 3D ビューアを描き、確定時に askUser.callback へ
 * { submitted: true, selected: [{ kind, id }] }、閉じたときは { submitted: false } を返す。
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
      onSubmit={(selected) => askUser.callback({ submitted: true, selected })}
      onCancel={() => askUser.callback({ submitted: false, selected: [] })}
    />
  );
}

interface PanelProps {
  question: string;
  kinds: CadKind[];
  data: CadData;
  onSubmit: (selected: CadSelection[]) => void;
  onCancel: () => void;
}

function CadViewerPanel({ question, kinds, data, onSubmit, onCancel }: PanelProps) {
  const { t } = useI18n();
  const hostRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<CadScene | null>(null);
  const [mode, setMode] = useState<CadKind>(kinds[0]);
  const [selection, setSelection] = useState<CadSelection[]>([]);
  const [hover, setHover] = useState<CadSelection | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [expanded, setExpanded] = useState(false);

  const label = useCallback((s: CadSelection) => `${t(KIND_LABEL[s.kind])} #${s.id}`, [t]);
  const toggle = useCallback((item: CadSelection) => {
    setSelection((cur) => (cur.some((s) => sameSelection(s, item)) ? cur.filter((s) => !sameSelection(s, item)) : [...cur, item]));
  }, []);

  // three.js は import() で必要になったときだけ読む。
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let disposed = false;
    import('./cad/CadScene')
      .then(({ CadScene }) => {
        if (disposed) return;
        sceneRef.current = new CadScene(host, data, { onHover: setHover, onPick: toggle }, sceneColors(host));
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
  }, [data, kinds, toggle]);

  useEffect(() => {
    sceneRef.current?.setMode(mode);
  }, [mode, status]);
  useEffect(() => {
    sceneRef.current?.setSelection(selection);
  }, [selection, status]);

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
    if (!hover) return t('cad.hint');
    return describe(itemsOf(data, hover.kind)[hover.id], label(hover), t);
  }, [hover, data, label, t]);

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
        </div>
        <div className="cad-viewer-views">
          {VIEWS.map((v) => (
            <button key={v.id} type="button" className="cad-viewer-chip" onClick={() => sceneRef.current?.setView(v.id)}>
              {t(v.label)}
            </button>
          ))}
          <button type="button" className="cad-viewer-chip" onClick={() => setExpanded((x) => !x)}>
            {expanded ? t('cad.shrink') : t('cad.expand')}
          </button>
        </div>
      </div>
      <div className="cad-viewer-host" ref={hostRef}>
        {status === 'loading' && <div className="cad-viewer-overlay">{t('cad.loading')}</div>}
        {status === 'error' && <div className="cad-viewer-overlay">{t('cad.webglError')}</div>}
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
            <span key={`${s.kind}-${s.id}`} className="cad-viewer-tag" title={describe(itemsOf(data, s.kind)[s.id], label(s), t)}>
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
      <div className="ask-form-actions">
        <button type="button" className="ask-form-cancel" onClick={onCancel}>
          {t('cad.close')}
        </button>
        <button type="button" className="ask-form-submit" onClick={() => onSubmit(selection)}>
          {t('cad.confirm', { count: selection.length })}
        </button>
      </div>
    </div>
  );
}
