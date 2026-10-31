import type { IStep } from '@chainlit/react-client';
import { useI18n } from '../i18n';
import { TOKEN_USAGE_PREFIX } from '../utils/messageTree';

/** app.py の _token_usage_level と一致させる（"warn"=オレンジ太字, "alert"=赤太字）。 */
type TokenUsageLevel = 'warn' | 'alert' | null;

interface TokenUsageRow {
  label: string;
  input: number;
  output: number;
  total: number;
  level?: TokenUsageLevel;
  /** app.py の _format_token_usage と一致させる（"call"=出力元ごとの直近1回分, "total"=累計）。 */
  group?: 'call' | 'total';
}

interface TokenUsagePayload {
  rows: TokenUsageRow[];
}

const fmt = (n: number) => n.toLocaleString('ja-JP');

// group を持たない旧形式（再開した古いスレッド）は level の有無で判定する。
const isCallRow = (row: TokenUsageRow) => (row.group ? row.group === 'call' : 'level' in row);

// 2つの表の列幅を揃えるため、両方に同じ colgroup を付ける。
const Cols = () => (
  <colgroup>
    <col className="token-usage-col-label" />
    <col />
    <col />
    <col />
  </colgroup>
);

const Rows = ({ rows }: { rows: TokenUsageRow[] }) => (
  <tbody>
    {rows.map((row, i) => (
      <tr
        key={`${i}-${row.label}`}
        className={row.level ? `token-usage-row-${row.level}` : undefined}
      >
        <td>{row.label}</td>
        <td>{fmt(row.input)}</td>
        <td>{fmt(row.output)}</td>
        <td>{fmt(row.total)}</td>
      </tr>
    ))}
  </tbody>
);

export function TokenUsageCard({ step }: { step: IStep | undefined }) {
  const { t } = useI18n();
  if (!step || typeof step.output !== 'string') return null;

  let payload: TokenUsagePayload;
  try {
    payload = JSON.parse(step.output.slice(TOKEN_USAGE_PREFIX.length));
  } catch {
    return null;
  }
  if (!payload.rows?.length) return null;

  // 出力元ごとの行は並列数に応じて増えるため、カードの高さを保ったままスクロールさせる。
  // 累計の行は常に見えるよう、スクロール領域の外に置く。
  const callRows = payload.rows.filter(isCallRow);
  const totalRows = payload.rows.filter((row) => !isCallRow(row));

  return (
    <div className="token-usage-card">
      <div className="token-usage-scroll">
        <table className="token-usage-table">
          <Cols />
          <thead>
            <tr>
              <th></th>
              <th>{t('usage.input')}</th>
              <th>{t('usage.output')}</th>
              <th>{t('usage.total')}</th>
            </tr>
          </thead>
          <Rows rows={callRows} />
        </table>
      </div>
      {totalRows.length > 0 && (
        <table className="token-usage-table token-usage-totals">
          <Cols />
          <Rows rows={totalRows} />
        </table>
      )}
    </div>
  );
}
