/**
 * ブラウザが Locohane と同じPCから開かれているか（localhost / 127.0.0.1 / ::1 で接続しているか）。
 *
 * 同じPCならブラウザのPCとサーバーPCのパスは同じなので、作業フォルダの
 * 「サーバーPCから見たパス」という注意書きは出さない（WorkDirButton / WorkDirCard）。
 */
export function isLoopbackHost(hostname: string = window.location.hostname): boolean {
  const host = hostname.replace(/^\[|\]$/g, '').toLowerCase();
  return host === 'localhost' || host === '::1' || host.startsWith('127.');
}
