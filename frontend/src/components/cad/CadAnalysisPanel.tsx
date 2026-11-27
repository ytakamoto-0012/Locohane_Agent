/**
 * 3D ビューアの表面の解析（ゼブラ・曲率・偏差）の設定の行と凡例、基準モデルとの位置合わせの行。
 * 状態は CadViewerBar.tsx の CadViewerPanel が持ち、ここは表示と入力だけ（three.js は使わない）。
 */
import { useI18n } from '../../i18n';
import type { MessageKey } from '../../i18n/messages';
import type { CadAlignmentInfo, CadDeviationStats } from './cadData';
import { fmtNum } from './measure';
import { CURVATURE_KINDS, MAX_ZEBRA_STRIPES, legendGradient, type CurvatureKind, type ScalarScheme, type ZebraLight, type ZebraOrientation, type ZebraSettings } from './surfaceShading';

export type AnalysisMode = 'normal' | 'zebra' | 'curvature' | 'deviation';
export type DatumName = 'A' | 'B' | 'C';
export type Datums = Record<DatumName, number[]>;
const DATUM_NAMES: DatumName[] = ['A', 'B', 'C'];
/** データムごとに決める自由度（src/cad/align.py の DATUM_DOF と同じ 3-2-1）。 */
const DATUM_DOF: Record<DatumName, number> = { A: 3, B: 2, C: 1 };
const ZEBRA_LIGHTS: ZebraLight[] = ['dome', 'cylinder', 'plane'];
const ZEBRA_ORIENTATIONS: ZebraOrientation[] = ['horizontal', 'vertical'];
const ZEBRA_ROT_AXES: [string, 'rotXDeg' | 'rotYDeg' | 'rotZDeg'][] = [
  ['X', 'rotXDeg'],
  ['Y', 'rotYDeg'],
  ['Z', 'rotZDeg']
];
/** 平面の光源の、基準点からの距離の欄（ZebraSettings.planeOffset の順）。 */
const ZEBRA_OFFSET_AXES = ['X', 'Y', 'Z'] as const;

/** 基準点からの距離の欄の刻み（今の距離の大きさのおよそ 1/10 の 1・10・100…）。 */
function offsetStep(off: number[]): number {
  const m = Math.max(...off.map(Math.abs));
  return m > 0 ? 10 ** Math.floor(Math.log10(m / 10)) : 1;
}

/** 数の入力欄（空・数でない入力は無視する）。 */
function NumberField({
  label,
  title,
  value,
  step,
  min,
  max,
  onChange
}: {
  label: string;
  title?: string;
  value: number;
  step: number;
  min?: number;
  max?: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="cad-viewer-chip cad-viewer-num" title={title ?? label}>
      {label}
      <input
        type="number"
        value={Number.isFinite(value) ? Number(value.toPrecision(6)) : ''}
        step={step}
        min={min}
        max={max}
        aria-label={title ?? label}
        onChange={(e) => {
          const v = Number(e.target.value);
          if (e.target.value !== '' && Number.isFinite(v)) onChange(min !== undefined || max !== undefined ? Math.min(max ?? v, Math.max(min ?? v, v)) : v);
        }}
      />
    </label>
  );
}

export interface RangeState {
  min: number;
  max: number;
  /** true なら値の分布から自動で決めた範囲。 */
  auto: boolean;
}

interface SettingsProps {
  mode: AnalysisMode;
  zebra: ZebraSettings;
  onZebra: (z: ZebraSettings) => void;
  /** 平面の光源の基準点をクリックで選んでいる最中か。 */
  basePicking: boolean;
  onBasePicking: (on: boolean) => void;
  curvatureKind: CurvatureKind;
  onCurvatureKind: (k: CurvatureKind) => void;
  range: RangeState;
  onRange: (r: RangeState) => void;
  onAutoRange: () => void;
  /** 0 を中心にそろえる値か（曲率の大きさ以外の曲率・偏差）。true のときだけ「連動」を出す。 */
  linkable: boolean;
  /** 連動: 最小・最大の片方を変えると、もう片方を 0 を挟んで同じ大きさ（±）にそろえる。 */
  linked: boolean;
  onLinked: (v: boolean) => void;
  tolerance: number;
  onTolerance: (v: number) => void;
  units: string;
}

/** 選んだ解析表示の設定の行（ゼブラの照明・縞、曲率の種類と範囲、偏差の公差と範囲）。 */
export function AnalysisSettingsRow(p: SettingsProps) {
  const { t } = useI18n();
  if (p.mode === 'normal') return null;
  if (p.mode === 'zebra') {
    const z = p.zebra;
    return (
      <div className="cad-viewer-measure-bar cad-viewer-analysis-bar" role="group" aria-label={t('cad.analysis.zebra')}>
        {ZEBRA_LIGHTS.map((l) => (
          <button
            key={l}
            type="button"
            role="radio"
            aria-checked={z.light === l}
            title={t(`cad.zebra.light.${l}Title` as MessageKey)}
            className={`cad-viewer-chip${z.light === l ? ' active' : ''}`}
            onClick={() => p.onZebra({ ...z, light: l })}
          >
            {t(`cad.zebra.light.${l}` as MessageKey)}
          </button>
        ))}
        {z.light !== 'dome' && (
          <>
            <span className="cad-viewer-sep" />
            {ZEBRA_ORIENTATIONS.map((o) => (
              <button
                key={o}
                type="button"
                role="radio"
                aria-checked={z.orientation === o}
                title={t(`cad.zebra.orient.${o}Title` as MessageKey)}
                className={`cad-viewer-chip${z.orientation === o ? ' active' : ''}`}
                onClick={() => p.onZebra({ ...z, orientation: o })}
              >
                {t(`cad.zebra.orient.${o}` as MessageKey)}
              </button>
            ))}
          </>
        )}
        <span className="cad-viewer-sep" />
        <NumberField
          label={t('cad.zebra.stripes')}
          value={z.stripes}
          step={1}
          min={1}
          max={MAX_ZEBRA_STRIPES}
          onChange={(v) => p.onZebra({ ...z, stripes: Math.round(v) })}
        />
        <NumberField label={t('cad.zebra.width')} title={t('cad.zebra.widthTitle')} value={z.width} step={0.05} min={0.05} max={0.95} onChange={(v) => p.onZebra({ ...z, width: v })} />
        <span className="cad-viewer-sep" />
        {ZEBRA_ROT_AXES.map(([axis, key]) => (
          <NumberField
            key={key}
            label={t('cad.zebra.rot', { axis })}
            title={t('cad.zebra.rotTitle', { axis })}
            value={z[key]}
            step={15}
            min={-180}
            max={180}
            onChange={(v) => p.onZebra({ ...z, [key]: v })}
          />
        ))}
        {z.light === 'plane' && (
          <>
            <span className="cad-viewer-sep" />
            <button
              type="button"
              className={`cad-viewer-chip${p.basePicking ? ' active' : ''}`}
              aria-pressed={p.basePicking}
              title={t('cad.zebra.baseTitle')}
              onClick={() => p.onBasePicking(!p.basePicking)}
            >
              {t('cad.zebra.base')}: ({z.planeBase.map((v) => fmtNum(v)).join(', ')})
            </button>
            <button
              type="button"
              className="cad-viewer-chip"
              disabled={z.planeBase.every((v) => v === 0)}
              title={t('cad.zebra.baseOriginTitle')}
              onClick={() => p.onZebra({ ...z, planeBase: [0, 0, 0] })}
            >
              {t('cad.zebra.baseOrigin')}
            </button>
            <button
              type="button"
              className={`cad-viewer-chip${z.planeAnchor === 'bottom' ? ' active' : ''}`}
              aria-pressed={z.planeAnchor === 'bottom'}
              title={t('cad.zebra.anchorTitle')}
              onClick={() => p.onZebra({ ...z, planeAnchor: z.planeAnchor === 'bottom' ? 'center' : 'bottom' })}
            >
              {z.planeAnchor === 'bottom' ? t('cad.zebra.anchorBottom') : t('cad.zebra.anchorCenter')}
            </button>
            {ZEBRA_OFFSET_AXES.map((axis, i) => (
              <NumberField
                key={axis}
                label={axis}
                title={t('cad.zebra.offsetTitle', { axis })}
                value={z.planeOffset[i]}
                step={offsetStep(z.planeOffset)}
                onChange={(v) => {
                  const off: [number, number, number] = [...z.planeOffset];
                  off[i] = v;
                  p.onZebra({ ...z, planeOffset: off });
                }}
              />
            ))}
            {p.units !== 'unknown' && <span className="cad-viewer-unit">{p.units}</span>}
          </>
        )}
        <span className="cad-viewer-sep" />
        <button
          type="button"
          className={`cad-viewer-chip${z.viewFixed ? ' active' : ''}`}
          aria-pressed={z.viewFixed}
          title={t('cad.zebra.fixedTitle')}
          onClick={() => p.onZebra({ ...z, viewFixed: !z.viewFixed })}
        >
          {z.viewFixed ? t('cad.zebra.fixedView') : t('cad.zebra.fixedModel')}
        </button>
        <button
          type="button"
          className={`cad-viewer-chip${z.showLights ? ' active' : ''}`}
          aria-pressed={z.showLights}
          title={t('cad.zebra.showLightsTitle')}
          onClick={() => p.onZebra({ ...z, showLights: !z.showLights })}
        >
          {t('cad.zebra.showLights')}
        </button>
        <button
          type="button"
          className={`cad-viewer-chip${z.gloss ? ' active' : ''}`}
          aria-pressed={z.gloss}
          title={t('cad.zebra.glossTitle')}
          onClick={() => p.onZebra({ ...z, gloss: !z.gloss })}
        >
          {t('cad.zebra.gloss')}
        </button>
      </div>
    );
  }
  return (
    <div className="cad-viewer-measure-bar cad-viewer-analysis-bar" role="group" aria-label={t(p.mode === 'curvature' ? 'cad.analysis.curvature' : 'cad.analysis.deviation')}>
      {p.mode === 'curvature' ? (
        <label className="cad-viewer-chip" title={t('cad.curvature.kindTitle')}>
          {t('cad.curvature.kind')}
          <select value={p.curvatureKind} aria-label={t('cad.curvature.kind')} onChange={(e) => p.onCurvatureKind(e.target.value as CurvatureKind)}>
            {CURVATURE_KINDS.map((k) => (
              <option key={k} value={k}>
                {t(`cad.curvature.${k}` as MessageKey)}
              </option>
            ))}
          </select>
        </label>
      ) : (
        <NumberField
          label={t('cad.deviation.tolerance')}
          title={t('cad.deviation.toleranceTitle')}
          value={p.tolerance}
          step={0.01}
          min={0}
          onChange={(v) => p.onTolerance(Math.max(0, v))}
        />
      )}
      <span className="cad-viewer-sep" />
      <NumberField label={t('cad.range.min')} value={p.range.min} step={rangeStep(p.range)} onChange={(v) => p.onRange(changeRange(p, 'min', v))} />
      <NumberField label={t('cad.range.max')} value={p.range.max} step={rangeStep(p.range)} onChange={(v) => p.onRange(changeRange(p, 'max', v))} />
      {p.linkable && (
        <button
          type="button"
          className={`cad-viewer-chip${p.linked ? ' active' : ''}`}
          aria-pressed={p.linked}
          title={t('cad.range.linkTitle')}
          onClick={() => {
            const next = !p.linked;
            p.onLinked(next);
            // 連動にしたら、今の範囲の大きい方の側に合わせて ± にそろえる。
            if (next) {
              const m = Math.max(Math.abs(p.range.min), Math.abs(p.range.max));
              if (p.range.min !== -m || p.range.max !== m) p.onRange({ min: -m, max: m, auto: p.range.auto });
            }
          }}
        >
          {t('cad.range.link')}
        </button>
      )}
      <button type="button" className={`cad-viewer-chip${p.range.auto ? ' active' : ''}`} title={t('cad.range.autoTitle')} onClick={p.onAutoRange}>
        {t('cad.range.auto')}
      </button>
      {p.units !== 'unknown' && <span className="cad-viewer-unit">{p.mode === 'curvature' ? `1/${p.units}` : p.units}</span>}
    </div>
  );
}

/**
 * 範囲の最小・最大の片方を変えた後の範囲。連動していれば、もう片方を 0 を挟んで同じ大きさにする
 * （最小に正の値・最大に負の値を入れたときは、その大きさで ± にそろえる）。
 */
function changeRange(p: { range: RangeState; linkable: boolean; linked: boolean }, side: 'min' | 'max', v: number): RangeState {
  if (p.linkable && p.linked) {
    const m = Math.abs(v);
    return { min: -m, max: m, auto: false };
  }
  return side === 'min' ? { ...p.range, min: v, auto: false } : { ...p.range, max: v, auto: false };
}

function rangeStep(r: RangeState): number {
  const span = Math.abs(r.max - r.min) || 1;
  return Number((10 ** Math.floor(Math.log10(span / 20))).toPrecision(1));
}

/** 色の凡例（3D 表示の右下に重ねる）。 */
export function ScalarLegend({ title, min, max, tolerance, scheme }: { title: string; min: number; max: number; tolerance: number; scheme: ScalarScheme }) {
  const mid = scheme === 'deviation' ? 0 : (min + max) / 2;
  return (
    <div className="cad-viewer-legend-box" aria-hidden="true">
      <div className="cad-viewer-legend-title">{title}</div>
      <div className="cad-viewer-legend-bar" style={{ background: legendGradient({ min, max, tolerance, scheme }) }} />
      <div className="cad-viewer-legend-ticks">
        <span>{fmtNum(min)}</span>
        <span>{scheme === 'deviation' && tolerance > 0 ? `±${fmtNum(tolerance)}` : fmtNum(mid)}</span>
        <span>{fmtNum(max)}</span>
      </div>
    </div>
  );
}

export type AlignState = { kind: 'idle' } | { kind: 'busy' } | { kind: 'error'; message: string };

interface AlignProps {
  canAlign: boolean;
  state: AlignState;
  info: CadAlignmentInfo | null;
  stats: CadDeviationStats | null;
  datums: Datums;
  selectedFaces: number;
  referenceVisible: boolean;
  onReferenceVisible: (v: boolean) => void;
  referenceOpacity: number;
  onReferenceOpacity: (v: number) => void;
  onSetDatum: (name: DatumName) => void;
  onClearDatums: () => void;
  onAlign: (method: 'bestfit' | 'faces' | 'datum' | 'reset' | 'initial') => void;
  referenceName: string;
}

/** 基準モデルの重ね表示と位置合わせの行、合わせた結果（RMS・偏差の統計）。 */
export function AlignRow(p: AlignProps) {
  const { t } = useI18n();
  const busy = p.state.kind === 'busy';
  const hasDatum = DATUM_NAMES.some((n) => p.datums[n].length > 0);
  const pct = (v: number | undefined) => (typeof v === 'number' ? `${(v * 100).toFixed(1)}%` : '—');
  const s = p.stats;
  return (
    <div className="cad-viewer-align">
      <div className="cad-viewer-measure-bar cad-viewer-analysis-bar" role="group" aria-label={t('cad.align')}>
        <span className="cad-viewer-chip cad-viewer-ref-name" title={p.referenceName}>
          {t('cad.align.reference')}: {p.referenceName.split(/[\\/]/).pop()}
        </span>
        <button
          type="button"
          className={`cad-viewer-chip${p.referenceVisible ? ' active' : ''}`}
          aria-pressed={p.referenceVisible}
          title={t('cad.align.overlayTitle')}
          onClick={() => p.onReferenceVisible(!p.referenceVisible)}
        >
          {t('cad.align.overlay')}
        </button>
        {p.referenceVisible && (
          <NumberField label={t('cad.align.opacity')} value={p.referenceOpacity} step={0.05} min={0.05} max={1} onChange={p.onReferenceOpacity} />
        )}
        {p.canAlign && (
          <>
            <span className="cad-viewer-sep" />
            <button type="button" className="cad-viewer-chip" disabled={busy} title={t('cad.align.bestfitTitle')} onClick={() => p.onAlign('bestfit')}>
              {t('cad.align.bestfit')}
            </button>
            <button
              type="button"
              className="cad-viewer-chip"
              disabled={busy || p.selectedFaces === 0}
              title={t('cad.align.facesTitle')}
              onClick={() => p.onAlign('faces')}
            >
              {t('cad.align.faces', { count: p.selectedFaces })}
            </button>
            <span className="cad-viewer-sep" />
            {DATUM_NAMES.map((n) => (
              <button
                key={n}
                type="button"
                className={`cad-viewer-chip${p.datums[n].length ? ' active' : ''}`}
                disabled={busy || p.selectedFaces === 0}
                title={t('cad.align.setDatumTitle', { name: n, dof: DATUM_DOF[n] })}
                onClick={() => p.onSetDatum(n)}
              >
                {n}
                {p.datums[n].length ? ` (${p.datums[n].length})` : ''}
              </button>
            ))}
            <button type="button" className="cad-viewer-chip" disabled={busy || !hasDatum} title={t('cad.align.datumTitle')} onClick={() => p.onAlign('datum')}>
              {t('cad.align.datum')}
            </button>
            <button type="button" className="cad-viewer-chip" disabled={busy || !hasDatum} onClick={p.onClearDatums}>
              {t('cad.align.clearDatums')}
            </button>
            <span className="cad-viewer-sep" />
            <button type="button" className="cad-viewer-chip" disabled={busy} title={t('cad.align.initialTitle')} onClick={() => p.onAlign('initial')}>
              {t('cad.align.initial')}
            </button>
            <button type="button" className="cad-viewer-chip" disabled={busy} title={t('cad.align.resetTitle')} onClick={() => p.onAlign('reset')}>
              {t('cad.align.reset')}
            </button>
          </>
        )}
      </div>
      <div className="cad-viewer-align-result" role="status">
        {busy ? (
          <>
            <span className="cad-viewer-spinner" aria-hidden="true" />
            {t('cad.align.running')}
          </>
        ) : p.state.kind === 'error' ? (
          <span className="cad-viewer-align-error">{t('cad.align.failed', { error: p.state.message })}</span>
        ) : (
          <>
            {p.info && (
              <span>
                {t(`cad.align.method.${p.info.method}` as MessageKey)}
                {typeof p.info.rms === 'number' ? `  RMS ${fmtNum(p.info.rms)}` : ''}
              </span>
            )}
            {s && (
              <span>
                {t('cad.align.stats', {
                  mean: fmtNum(s.mean ?? NaN),
                  std: fmtNum(s.std ?? NaN),
                  min: fmtNum(s.min ?? NaN),
                  max: fmtNum(s.max ?? NaN),
                  within: pct(s.within_tolerance_ratio)
                })}
              </span>
            )}
          </>
        )}
      </div>
    </div>
  );
}
