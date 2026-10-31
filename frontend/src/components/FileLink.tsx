import { useRef, useState, type MouseEvent } from 'react';
import { useI18n } from '../i18n';
import { Icon } from './Icon';

/** ホーム画面に追加した Web アプリ（スタンドアロン表示）として開かれているか。 */
function isStandalone(): boolean {
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  );
}

type ShareState = 'idle' | 'loading' | 'ready' | 'error';

// 共有シートは MIME タイプから開けるアプリ（Numbers・Excel 等）を候補に出すため、
// サーバーが汎用の型（application/octet-stream 等）で返したときは拡張子から補う。
const MIME_BY_EXTENSION: Record<string, string> = {
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  xlsm: 'application/vnd.ms-excel.sheet.macroEnabled.12',
  xls: 'application/vnd.ms-excel',
  csv: 'text/csv',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  doc: 'application/msword',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  ppt: 'application/vnd.ms-powerpoint',
  pdf: 'application/pdf',
  txt: 'text/plain',
  md: 'text/markdown',
  json: 'application/json',
  zip: 'application/zip',
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg'
};

const GENERIC_MIME_TYPES = new Set(['', 'application/octet-stream', 'binary/octet-stream', 'application/x-download']);

function mimeTypeFor(name: string, served: string): string {
  const servedType = served.split(';')[0].trim().toLowerCase();
  if (!GENERIC_MIME_TYPES.has(servedType)) return served;
  const ext = name.split('.').pop()?.toLowerCase() ?? '';
  return MIME_BY_EXTENSION[ext] ?? 'application/octet-stream';
}

/** 添付ファイルのダウンロードリンク。
 *
 * iOS のホーム画面に追加した Web アプリには戻るボタンもタブも無く、ファイルへ
 * 遷移すると画面がプレビュー（「Numbers で開く」等）に置き換わってチャットへ
 * 戻れなくなる（target="_blank" でも同じ画面内で開くことがある）。そのため
 * スタンドアロン表示では遷移せず、ファイルを取得して共有シートを出す。
 * 共有シートはタップ操作の直後でないと開けない（取得を待つ間に期限が切れると
 * NotAllowedError になる）ため、そのときは取得済みのファイルを持っておき、
 * もう一度タップしてもらう。 */
export function FileLink({ href, name }: { href: string | undefined; name: string }) {
  const { t } = useI18n();
  const [state, setState] = useState<ShareState>('idle');
  const fileRef = useRef<File | null>(null);

  const share = async (file: File) => {
    try {
      await navigator.share({ files: [file] });
      setState('idle');
    } catch (err) {
      const errName = (err as DOMException)?.name;
      if (errName === 'AbortError') {
        // 利用者が共有シートを閉じただけ
        setState('idle');
      } else if (errName === 'NotAllowedError') {
        setState('ready');
      } else {
        setState('error');
      }
    }
  };

  const handleClick = async (event: MouseEvent<HTMLAnchorElement>) => {
    if (!href || !isStandalone() || typeof navigator.share !== 'function') return;
    event.preventDefault();
    if (state === 'loading') return;
    if (fileRef.current) {
      await share(fileRef.current);
      return;
    }
    setState('loading');
    try {
      const res = await fetch(href, { credentials: 'include' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const file = new File([blob], name, { type: mimeTypeFor(name, blob.type) });
      if (navigator.canShare && !navigator.canShare({ files: [file] })) {
        setState('error');
        return;
      }
      fileRef.current = file;
      await share(file);
    } catch {
      setState('error');
    }
  };

  const label =
    state === 'loading'
      ? t('file.preparing', { name })
      : state === 'ready'
        ? t('file.tapAgain', { name })
        : state === 'error'
          ? t('file.failed', { name })
          : name;

  return (
    // スタンドアロン表示以外（PC・Safari のタブ）は従来どおりリンクとして開く。
    // iOS Safari は download 属性があっても Office 文書等を同じタブでプレビュー表示し、
    // チャット画面へ戻れなくなるため別タブで開く（PC のブラウザは download 属性で保存する）。
    <a
      href={href}
      download={name}
      target="_blank"
      rel="noopener noreferrer"
      className="message-file-link"
      onClick={handleClick}
    >
      <Icon name="paperclip" size={13} />
      {label}
    </a>
  );
}
