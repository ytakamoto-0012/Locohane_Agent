/**
 * 3D ビューア（CadViewerBar）の左のサイドバー。
 * - 表示: 面・エッジ・頂点の表示／非表示
 * - 色: 選んだ面・部品の色（RGB）の表示と変更
 * - STEP: AP と版・PMI の一覧・製品の属性の表示と編集（CadStepPanel）
 * - 構成ツリー（STEP/IGES のアセンブリ）／レイヤー（DXF）: 部品ごとの表示／非表示・色・レイヤーの設定。名前のクリックで部品を選ぶ
 * three.js は使わない（CadScene と違い、チャット画面の本体と一緒に読み込まれる）。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useI18n } from '../../i18n';
import type { MessageKey } from '../../i18n/messages';
import {
  effectiveColor,
  hexToRgb,
  rgbText,
  rgbToHex,
  sameSelection,
  type CadColorOverrides,
  type CadData,
  type CadItemInfo,
  type CadKind,
  type CadSelection
} from './cadData';
import { CadStepPanel, type CadAttributeEdits } from './CadStepPanel';

// 色の無い面（テーマの既定色で描く）を変えるときの、色の入力の初期値。
const DEFAULT_PICKER_COLOR = '#b9bfcb';

type KindVisible = { face: boolean; edge: boolean; vertex: boolean };

interface Props {
  data: CadData;
  kinds: CadKind[];
  selection: CadSelection[];
  kindVisible: KindVisible;
  onKindVisible: (kind: keyof KindVisible, visible: boolean) => void;
  hiddenParts: Set<number>;
  onHiddenParts: (hidden: Set<number>) => void;
  colors: CadColorOverrides;
  onColors: (colors: CadColorOverrides) => void;
  onTogglePart: (id: number) => void;
  onHoverPart: (id: number | null) => void;
  /** STEP の属性の編集（製品名 → 編集後の一覧）。 */
  attributeEdits: CadAttributeEdits;
  onAttributeEdits: (edits: CadAttributeEdits) => void;
  /** PMI のクリックで、それが指す要素を選び、そこへ寄せる。 */
  onSelectItems: (items: CadSelection[]) => void;
}

interface TreeNode {
  key: string;
  name: string;
  children: TreeNode[];
  part?: CadItemInfo;
}

/** 部品の path（"組立/サブ組立/部品"）からツリーを作る。同じ名前の部品（同じ部品の別の配置）も別の葉にする。 */
function buildTree(parts: CadItemInfo[], flat: boolean): TreeNode[] {
  const root: TreeNode = { key: 'root', name: '', children: [] };
  for (const part of parts) {
    const names = flat ? [part.name ?? `#${part.id}`] : (part.path ?? part.name ?? `#${part.id}`).split('/');
    let node = root;
    names.slice(0, -1).forEach((name, depth) => {
      let child = node.children.find((c) => !c.part && c.name === name);
      if (!child) {
        child = { key: `${node.key}/${depth}:${name}`, name, children: [] };
        node.children.push(child);
      }
      node = child;
    });
    node.children.push({ key: `p${part.id}`, name: names[names.length - 1] || `#${part.id}`, children: [], part });
  }
  return root.children;
}

function leafIds(node: TreeNode): number[] {
  return node.part ? [node.part.id] : node.children.flatMap(leafIds);
}

function TriCheckbox({ checked, mixed, onChange, label }: { checked: boolean; mixed: boolean; onChange: (v: boolean) => void; label: string }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = mixed;
  }, [mixed]);
  return <input ref={ref} type="checkbox" checked={checked} aria-label={label} onChange={(e) => onChange(e.target.checked)} />;
}

export function CadSidebar(props: Props) {
  const { t } = useI18n();
  const { data } = props;
  const parts = useMemo(() => data.parts ?? [], [data]);
  const isDxf = data.source.format === 'DXF';
  const tree = useMemo(() => buildTree(parts, isDxf), [parts, isDxf]);
  const kindRows: { kind: keyof KindVisible; has: boolean }[] = [
    { kind: 'face', has: data.faces.length > 0 },
    { kind: 'edge', has: data.edgeList.length > 0 },
    { kind: 'vertex', has: data.vertexList.length > 0 }
  ];

  const setHidden = (ids: number[], hidden: boolean) => {
    const next = new Set(props.hiddenParts);
    ids.forEach((id) => (hidden ? next.add(id) : next.delete(id)));
    props.onHiddenParts(next);
  };

  return (
    <aside className="cad-sidebar" aria-label={t('cad.sidebar')}>
      <section>
        <h4>{t('cad.side.display')}</h4>
        {kindRows
          .filter((r) => r.has)
          .map((r) => (
            <label key={r.kind} className="cad-sidebar-row">
              <input type="checkbox" checked={props.kindVisible[r.kind]} onChange={(e) => props.onKindVisible(r.kind, e.target.checked)} />
              {t(`cad.kind.${r.kind}` as MessageKey)}
            </label>
          ))}
      </section>
      <ColorSection {...props} />
      <CadStepPanel
        data={data}
        selection={props.selection}
        attributeEdits={props.attributeEdits}
        onAttributeEdits={props.onAttributeEdits}
        onSelectItems={props.onSelectItems}
      />
      {tree.length > 0 && (
        <section>
          <h4>
            {t(isDxf ? 'cad.side.layers' : 'cad.side.tree')}
            <span className="cad-sidebar-actions">
              <button type="button" onClick={() => setHidden(parts.map((p) => p.id), false)}>
                {t('cad.side.showAll')}
              </button>
              <button type="button" onClick={() => setHidden(parts.map((p) => p.id), true)}>
                {t('cad.side.hideAll')}
              </button>
            </span>
          </h4>
          <ul className="cad-tree" role="tree">
            {tree.map((node) => (
              <TreeRow key={node.key} node={node} depth={0} sidebar={props} setHidden={setHidden} />
            ))}
          </ul>
        </section>
      )}
    </aside>
  );
}

function TreeRow({
  node,
  depth,
  sidebar,
  setHidden
}: {
  node: TreeNode;
  depth: number;
  sidebar: Props;
  setHidden: (ids: number[], hidden: boolean) => void;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(depth < 2);
  const ids = useMemo(() => leafIds(node), [node]);
  const hiddenCount = ids.filter((id) => sidebar.hiddenParts.has(id)).length;
  const visibleBox = (
    <TriCheckbox
      checked={hiddenCount < ids.length}
      mixed={hiddenCount > 0 && hiddenCount < ids.length}
      onChange={(v) => setHidden(ids, !v)}
      label={t('cad.side.visible')}
    />
  );
  const indent = { paddingLeft: `${depth * 12}px` };

  if (!node.part) {
    return (
      <li role="treeitem" aria-expanded={open}>
        <div className="cad-tree-row" style={indent}>
          <button type="button" className="cad-tree-toggle" onClick={() => setOpen((x) => !x)}>
            {open ? '▾' : '▸'}
          </button>
          {visibleBox}
          <span className="cad-tree-name" title={node.name}>
            {node.name}
          </span>
          <span className="cad-tree-count">{ids.length}</span>
        </div>
        {open && (
          <ul role="group">
            {node.children.map((c) => (
              <TreeRow key={c.key} node={c} depth={depth + 1} sidebar={sidebar} setHidden={setHidden} />
            ))}
          </ul>
        )}
      </li>
    );
  }

  const part = node.part;
  const color = effectiveColor(sidebar.data, sidebar.colors, { kind: 'part', id: part.id });
  const selected = sidebar.selection.some((s) => sameSelection(s, { kind: 'part', id: part.id }));
  const canSelect = sidebar.kinds.includes('part');
  const layer = part.layer;
  const setPartColor = (hex: string) => {
    const next = new Map(sidebar.colors.parts);
    next.set(part.id, hex);
    sidebar.onColors({ faces: sidebar.colors.faces, parts: next });
  };
  const layerInfo = layer
    ? [
        layer.linetype,
        layer.off ? t('cad.side.layerOff') : null,
        layer.frozen ? t('cad.side.layerFrozen') : null,
        layer.locked ? t('cad.side.layerLocked') : null
      ].filter(Boolean)
    : [];
  return (
    <li role="treeitem" aria-selected={selected}>
      <div
        className={`cad-tree-row${selected ? ' selected' : ''}`}
        style={indent}
        onMouseEnter={() => sidebar.onHoverPart(part.id)}
        onMouseLeave={() => sidebar.onHoverPart(null)}
      >
        <span className="cad-tree-toggle" />
        {visibleBox}
        <input
          type="color"
          className="cad-swatch"
          value={color ?? DEFAULT_PICKER_COLOR}
          title={color ? rgbText(color) : t('cad.side.noColor')}
          aria-label={t('cad.side.color')}
          onChange={(e) => setPartColor(e.target.value)}
        />
        <button
          type="button"
          className="cad-tree-name"
          disabled={!canSelect}
          title={[part.path ?? part.name, color ? rgbText(color) : '', ...layerInfo].filter(Boolean).join('  ')}
          onClick={() => sidebar.onTogglePart(part.id)}
        >
          {node.name}
        </button>
        {layerInfo.length > 0 && <span className="cad-tree-count">{layerInfo.join(' · ')}</span>}
      </div>
    </li>
  );
}

/** 選んだ面・部品の色（RGB）の表示と変更。 */
function ColorSection({ data, selection, colors, onColors }: Props) {
  const { t } = useI18n();
  const targets = selection.filter((s) => s.kind === 'face' || s.kind === 'part');
  const current = targets.map((s) => effectiveColor(data, colors, s));
  const first = current.find((c) => c !== null) ?? null;
  const mixed = new Set(current).size > 1;
  const [rgb, setRgb] = useState<[number, number, number]>(() => hexToRgb(first ?? DEFAULT_PICKER_COLOR)!);
  const firstKey = `${targets.map((s) => `${s.kind}${s.id}`).join(',')}|${first}`;
  useEffect(() => {
    setRgb(hexToRgb(first ?? DEFAULT_PICKER_COLOR)!);
    // 選択（か、その色）が変わったときだけ入力欄を合わせる。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [firstKey]);

  const apply = (hex: string | null) => {
    const faces = new Map(colors.faces);
    const parts = new Map(colors.parts);
    for (const s of targets) {
      const map = s.kind === 'face' ? faces : parts;
      if (hex) map.set(s.id, hex);
      else map.delete(s.id);
    }
    onColors({ faces, parts });
  };
  const setChannel = (i: number, value: string) => {
    const v = Math.max(0, Math.min(255, Math.round(Number(value) || 0)));
    const next: [number, number, number] = [...rgb];
    next[i] = v;
    setRgb(next);
    apply(rgbToHex(next));
  };
  const changed = targets.some((s) => (s.kind === 'face' ? colors.faces : colors.parts).has(s.id));

  return (
    <section>
      <h4>{t('cad.side.color')}</h4>
      {targets.length === 0 ? (
        <div className="cad-sidebar-hint">{t('cad.side.colorHint')}</div>
      ) : (
        <>
          <div className="cad-sidebar-hint">
            {t('cad.side.colorTargets', { count: targets.length })}
            {mixed ? ` ${t('cad.side.colorMixed')}` : ''}
            {!first ? ` ${t('cad.side.noColor')}` : ''}
          </div>
          <div className="cad-color-edit">
            <input
              type="color"
              className="cad-swatch"
              value={rgbToHex(rgb)}
              aria-label={t('cad.side.color')}
              onChange={(e) => {
                const next = hexToRgb(e.target.value);
                if (next) setRgb(next);
                apply(e.target.value);
              }}
            />
            {(['R', 'G', 'B'] as const).map((ch, i) => (
              <label key={ch} className="cad-rgb">
                {ch}
                <input type="number" min={0} max={255} value={rgb[i]} onChange={(e) => setChannel(i, e.target.value)} />
              </label>
            ))}
          </div>
          {changed && (
            <button type="button" className="cad-viewer-clear" onClick={() => apply(null)}>
              {t('cad.side.colorReset')}
            </button>
          )}
        </>
      )}
    </section>
  );
}
