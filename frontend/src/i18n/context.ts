import { createContext, useContext } from 'react';
import type { Lang, Translate } from './messages';

export interface I18nContextValue {
  lang: Lang;
  // 言語が確定したか（localStorage に無ければ /locohane/ui-config の既定値を待つ）。
  // App は確定してから connect する（userEnv で言語をバックエンドへ渡すため）。
  ready: boolean;
  setLang: (lang: Lang) => void;
  t: Translate;
}

export const I18nContext = createContext<I18nContextValue | null>(null);

export function useI18n(): I18nContextValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error('useI18n must be used inside LanguageProvider');
  return value;
}
