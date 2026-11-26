import { useEffect, useState } from 'react';
import { BACKEND_URL } from '../chainlitClient';

/** /locohane/ui-config の応答（app.py の _serve_ui_config）。ログイン前でも取れる表示用の設定。 */
export interface UiConfig {
  default_language?: string;
  /** 会話欄で AI の回答の上に出す名前（config.ini [persona] name。未設定なら空で、名前の行を出さない）。 */
  assistant_name?: string;
}

/** 起動中は変わらない値のため、1回だけ取りに行き、使う所で同じ結果を共有する。 */
let uiConfigPromise: Promise<UiConfig> | null = null;

export function fetchUiConfig(): Promise<UiConfig> {
  if (!uiConfigPromise) {
    uiConfigPromise = fetch(`${BACKEND_URL}/locohane/ui-config`)
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(res.statusText))))
      .catch((e) => {
        uiConfigPromise = null; // 失敗したら次に使うときに取り直す
        throw e;
      });
  }
  return uiConfigPromise;
}

/** AI の名前（取れるまで・取れなければ空文字。空なら名前の行を出さない）。 */
export function useAssistantName(): string {
  const [name, setName] = useState('');
  useEffect(() => {
    let alive = true;
    fetchUiConfig()
      .then((config) => {
        if (alive && typeof config.assistant_name === 'string') setName(config.assistant_name.trim());
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  return name;
}
