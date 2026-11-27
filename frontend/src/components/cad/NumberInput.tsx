/**
 * 3D ビューアの数の入力欄。打っている途中の文字（「-」「0.」「1e」や範囲外の値）を書き換えない。
 * 打っている間は、数として読めて範囲内の値だけをその場で反映し、範囲への丸めは欄を離れたとき・Enter のときに行う。
 * Esc で打ちかけの文字を捨てて今の値に戻す。
 */
import { useState, type InputHTMLAttributes } from 'react';

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'value' | 'onChange' | 'min' | 'max'> & {
  value: number;
  min?: number;
  max?: number;
  onChange: (v: number) => void;
  /** Enter で確定したときに、確定した値で呼ぶ。 */
  onEnter?: (v: number) => void;
};

function parse(text: string): number | null {
  if (text.trim() === '') return null;
  const v = Number(text);
  return Number.isFinite(v) ? v : null;
}

export function NumberInput({ value, min, max, onChange, onEnter, onBlur, onKeyDown, ...rest }: Props) {
  /** 打ちかけの文字。null なら value をそのまま表示する。 */
  const [draft, setDraft] = useState<string | null>(null);
  const clamp = (v: number) => Math.min(max ?? v, Math.max(min ?? v, v));
  const inRange = (v: number) => (min === undefined || v >= min) && (max === undefined || v <= max);

  /** 打ちかけの値を範囲に丸めて確定する。確定した値（数として読めなければ今の値）を返す。 */
  const commit = (): number => {
    const v = draft === null ? null : parse(draft);
    setDraft(null);
    if (v === null) return value;
    const c = clamp(v);
    if (c !== value) onChange(c);
    return c;
  };

  return (
    <input
      {...rest}
      type="number"
      value={draft ?? (Number.isFinite(value) ? Number(value.toPrecision(6)) : '')}
      min={min}
      max={max}
      onChange={(e) => {
        const text = e.target.value;
        setDraft(text);
        const v = parse(text);
        if (v !== null && inRange(v)) onChange(v);
      }}
      onBlur={(e) => {
        commit();
        onBlur?.(e);
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          const v = commit();
          onEnter?.(v);
        } else if (e.key === 'Escape') {
          setDraft(null);
        }
        onKeyDown?.(e);
      }}
    />
  );
}
