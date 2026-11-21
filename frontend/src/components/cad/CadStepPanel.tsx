/**
 * 3D ビューアのサイドバーの STEP の欄（元が STEP のときだけ出す）。
 * - STEP: AP と版・作成システム（ヘッダー）、読み込みで気づいた点
 * - PMI: 寸法・幾何公差・データムの一覧。クリックでその PMI が指す面・エッジ・部品を選び、そこへ寄せる
 * - 属性: 選んだ部品の製品（部品が 1 つならその製品）の属性（品番・材質等）の表示と編集。編集は確定・保存で返す
 */
import { useMemo, useState } from 'react';
import { useI18n } from '../../i18n';
import { partOf, pmiTargets, type CadAttribute, type CadData, type CadPmiItem, type CadSelection } from './cadData';

/** 製品名 → 編集後の属性の一覧（編集した製品だけ）。 */
export type CadAttributeEdits = Record<string, CadAttribute[]>;

interface Props {
  data: CadData;
  selection: CadSelection[];
  attributeEdits: CadAttributeEdits;
  onAttributeEdits: (edits: CadAttributeEdits) => void;
  onSelectItems: (items: CadSelection[]) => void;
}

// サイドバーに並べる PMI の数の上限（多いときは先頭だけ）。
const MAX_PMI_ROWS = 300;
// 打ち終わった形の数（打っている途中の "1." "-" "1e" 等は含まない）。これに合えばすぐ値にする。
const COMPLETE_NUMBER = /^-?\d+(\.\d+)?$/;

/**
 * 数の属性の値の入力欄。打っている途中の文字（"12." や "-"）は欄の中だけに持ち、数として読める形になったら値にする
 * （打つたびに数へ直すと "12." の点が消えて 12.5 が打てず、空にすると型が外れて文字列になっていた）。
 * 欄を離れたとき（Enter でも）に、数として読めればその数、空なら元の値に戻し、数でない文字なら型を外して文字列の属性にする。
 */
function NumberAttrInput({ row, placeholder, onCommit }: { row: CadAttribute; placeholder: string; onCommit: (next: CadAttribute) => void }) {
  const [draft, setDraft] = useState<string | null>(null);
  const shown = draft ?? String(row.value);
  const commit = (text: string) => {
    setDraft(null);
    const trimmed = text.trim();
    if (trimmed === '') return;
    const num = Number(trimmed);
    if (Number.isFinite(num)) {
      if (num !== row.value) onCommit({ ...row, value: num });
      return;
    }
    const { type: _type, ...rest } = row;
    onCommit({ ...rest, value: text });
  };
  return (
    <input
      type="text"
      inputMode="decimal"
      value={shown}
      placeholder={placeholder}
      aria-label={placeholder}
      title={row.type ?? ''}
      onChange={(e) => {
        const text = e.target.value;
        if (COMPLETE_NUMBER.test(text.trim())) {
          const num = Number(text.trim());
          if (num !== row.value) onCommit({ ...row, value: num });
          // 打った文字のまま見せる（"1.50" を 1.5 に書き換えない）。
          setDraft(String(num) === text ? null : text);
          return;
        }
        setDraft(text);
      }}
      onBlur={(e) => commit(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
      }}
    />
  );
}

export function CadStepPanel({ data, selection, attributeEdits, onAttributeEdits, onSelectItems }: Props) {
  const { t } = useI18n();
  const step = data.step;
  const pmi = useMemo<CadPmiItem[]>(
    () => [...(step?.pmi?.dimensions ?? []), ...(step?.pmi?.tolerances ?? []), ...(step?.pmi?.datums ?? [])],
    [step]
  );
  const products = useMemo(() => step?.products ?? [], [step]);
  // 属性を見せる製品: 選んだ部品の製品。部品を選んでいなければ、製品が 1 つならそれ。
  const productName = useMemo(() => {
    for (const s of selection) {
      // 要素の種類ごとの一覧から部品を引く（頂点の ID をエッジの一覧で引くと、別の部品の製品になる）。
      const partId = partOf(data, s);
      const name = partId !== null ? data.parts?.[partId]?.product : undefined;
      if (typeof name === 'string') return name;
    }
    return products.length === 1 ? (products[0].name ?? null) : null;
  }, [selection, data, products]);
  if (!step) return null;
  const header = step.header ?? {};
  const product = products.find((p) => p.name === productName);
  const rows: CadAttribute[] = productName ? (attributeEdits[productName] ?? product?.attributes ?? []) : [];
  const edited = productName !== null && attributeEdits[productName] !== undefined;
  const setRows = (next: CadAttribute[]) => {
    if (!productName) return;
    onAttributeEdits({ ...attributeEdits, [productName]: next });
  };
  const resetRows = () => {
    if (!productName) return;
    const next = { ...attributeEdits };
    delete next[productName];
    onAttributeEdits(next);
  };

  return (
    <>
      <section>
        <h4>{t('cad.side.step')}</h4>
        <div className="cad-sidebar-hint">
          {[header.edition ?? header.ap, header.originating_system].filter(Boolean).join(' · ') || header.schema?.[0]}
        </div>
        {(step.warnings ?? []).map((w) => (
          <div key={w} className="cad-sidebar-hint cad-sidebar-warning">
            {w}
          </div>
        ))}
      </section>
      {pmi.length > 0 && (
        <section>
          <h4>
            {t('cad.side.pmi')} <span className="cad-tree-count">{pmi.length}</span>
          </h4>
          <div className="cad-sidebar-hint">{t('cad.side.pmiHint')}</div>
          <ul className="cad-pmi-list">
            {pmi.slice(0, MAX_PMI_ROWS).map((item) => {
              const targets = pmiTargets(item);
              return (
                <li key={item.id}>
                  <button type="button" className="cad-tree-name" disabled={!targets.length} title={item.id} onClick={() => onSelectItems(targets)}>
                    {item.text}
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {products.length > 0 && (
        <section>
          <h4>
            {t('cad.side.attributes')}
            {productName && (
              <span className="cad-sidebar-actions">
                <button type="button" onClick={() => setRows([...rows, { name: '', value: '' }])}>
                  {t('cad.side.attrAdd')}
                </button>
                {edited && (
                  <button type="button" onClick={resetRows}>
                    {t('cad.side.attrReset')}
                  </button>
                )}
              </span>
            )}
          </h4>
          {!productName ? (
            <div className="cad-sidebar-hint">{t('cad.side.attrHint')}</div>
          ) : (
            <>
              <div className="cad-sidebar-hint">
                {t('cad.side.attrProduct')}: {productName}
                {product?.product_id && product.product_id !== productName ? ` (${product.product_id})` : ''}
              </div>
              {(product?.material ?? []).length > 0 && (
                <div className="cad-sidebar-hint">
                  {t('cad.side.material')}: {(product?.material ?? []).map((m) => `${m.name}${m.value !== null && m.value !== '' ? ` ${m.value}` : ''}`).join(', ')}
                </div>
              )}
              {rows.length === 0 && <div className="cad-sidebar-hint">{t('cad.side.attrNone')}</div>}
              <div className="cad-attr-table">
                {rows.map((row, i) => (
                  <div key={i} className="cad-attr-row">
                    <input
                      type="text"
                      value={row.name}
                      placeholder={t('cad.side.attrName')}
                      aria-label={t('cad.side.attrName')}
                      onChange={(e) => setRows(rows.map((r, k) => (k === i ? { ...r, name: e.target.value } : r)))}
                    />
                    {typeof row.value === 'number' ? (
                      <NumberAttrInput
                        row={row}
                        placeholder={t('cad.side.attrValue')}
                        onCommit={(next) => setRows(rows.map((r, k) => (k === i ? next : r)))}
                      />
                    ) : (
                      <input
                        type="text"
                        value={row.value === null || row.value === undefined ? '' : String(row.value)}
                        placeholder={t('cad.side.attrValue')}
                        aria-label={t('cad.side.attrValue')}
                        title={row.type ?? ''}
                        onChange={(e) => setRows(rows.map((r, k) => (k === i ? { ...r, value: e.target.value } : r)))}
                      />
                    )}
                    <button type="button" aria-label={t('cad.side.attrDelete')} title={t('cad.side.attrDelete')} onClick={() => setRows(rows.filter((_, k) => k !== i))}>
                      ×
                    </button>
                  </div>
                ))}
              </div>
            </>
          )}
        </section>
      )}
    </>
  );
}
