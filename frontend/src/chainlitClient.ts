import { ChainlitAPI } from '@chainlit/react-client';

// devサーバー(別ポート)からは既定で稼働中のChainlitバックエンド(既定ポート8000)へ接続する。
// ビルド後は public/build に配置され同一オリジンで動くため、window.location.origin を使う
// （ChainlitAPI は内部で new URL() を使うため、絶対URLでなければならない）。
export const BACKEND_URL = import.meta.env.DEV ? 'http://localhost:8000' : window.location.origin;

export const chainlitApi = new ChainlitAPI(BACKEND_URL, 'webapp');
