import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { BACKEND_URL } from '../chainlitClient';
import { I18nContext } from './context';
import { MESSAGES, STORAGE_KEY, format, getBrowserLang, getStoredLang, ja, normalizeLang, type Lang, type Translate } from './messages';

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [stored] = useState(getStoredLang);
  const [lang, setLangState] = useState<Lang>(stored ?? getBrowserLang());
  const [ready, setReady] = useState(stored !== null);

  // まだ言語を選んでいない利用者は、config.ini の既定値（auto ならブラウザの言語）にする。
  useEffect(() => {
    if (stored !== null) return;
    fetch(`${BACKEND_URL}/locohane/ui-config`)
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then((config) => {
        const lang = normalizeLang(config?.default_language);
        if (lang) setLangState(lang);
      })
      .catch(() => {})
      .finally(() => setReady(true));
  }, [stored]);

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  const setLang = useCallback((next: Lang) => {
    localStorage.setItem(STORAGE_KEY, next);
    setLangState(next);
  }, []);

  const t = useCallback<Translate>((key, vars) => format(MESSAGES[lang][key] ?? ja[key], vars), [lang]);

  const value = useMemo(() => ({ lang, ready, setLang, t }), [lang, ready, setLang, t]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}
