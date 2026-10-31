import { useEffect, useRef, useState } from 'react';
import {
  ACCENT_PRESETS,
  applyAccent,
  applyScheme,
  getStoredAccentId,
  getStoredScheme,
  type ColorScheme
} from '../theme';
import { LANGUAGE_OPTIONS, useI18n, type MessageKey } from '../i18n';
import { Icon } from './Icon';

const SCHEME_OPTIONS: { id: ColorScheme; label: MessageKey; icon: 'sun' | 'moon' | 'monitor' }[] = [
  { id: 'light', label: 'theme.light', icon: 'sun' },
  { id: 'dark', label: 'theme.dark', icon: 'moon' },
  { id: 'system', label: 'theme.system', icon: 'monitor' }
];

export function ThemePicker() {
  const { lang, setLang, t } = useI18n();
  const [open, setOpen] = useState(false);
  const [accent, setAccent] = useState(getStoredAccentId());
  const [scheme, setScheme] = useState<ColorScheme>(getStoredScheme());
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    applyAccent(accent);
    applyScheme(scheme);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 「自動」選択中はOS側のライト/ダーク切り替えにも追従させる。
  useEffect(() => {
    if (scheme !== 'system') return;
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => applyScheme('system');
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, [scheme]);

  useEffect(() => {
    if (!open) return;
    const onClickOutside = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onClickOutside);
    return () => document.removeEventListener('mousedown', onClickOutside);
  }, [open]);

  const selectAccent = (id: string) => {
    setAccent(id);
    applyAccent(id);
  };

  const selectScheme = (id: ColorScheme) => {
    setScheme(id);
    applyScheme(id);
  };

  return (
    <div className="theme-picker" ref={ref}>
      <button
        type="button"
        className="theme-picker-trigger"
        title={t('theme.title')}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name="palette" />
      </button>
      {open ? (
        <div className="theme-picker-popover">
          <div className="theme-picker-section">
            <div className="theme-picker-section-title">{t('theme.scheme')}</div>
            <div className="scheme-options">
              {SCHEME_OPTIONS.map((opt) => (
                <button
                  key={opt.id}
                  type="button"
                  className={`scheme-option ${opt.id === scheme ? 'scheme-option--active' : ''}`}
                  onClick={() => selectScheme(opt.id)}
                >
                  <Icon name={opt.icon} size={14} />
                  {t(opt.label)}
                </button>
              ))}
            </div>
          </div>
          <div className="theme-picker-section">
            <div className="theme-picker-section-title">{t('theme.accent')}</div>
            <div className="theme-swatches">
              {ACCENT_PRESETS.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  className={`theme-swatch ${p.id === accent ? 'theme-swatch--active' : ''}`}
                  style={{ background: p.accent }}
                  title={t(`accent.${p.id}` as MessageKey)}
                  onClick={() => selectAccent(p.id)}
                />
              ))}
            </div>
          </div>
          <div className="theme-picker-section">
            <div className="theme-picker-section-title">{t('theme.language')}</div>
            <div className="scheme-options">
              {LANGUAGE_OPTIONS.map((opt) => (
                <button
                  key={opt.id}
                  type="button"
                  className={`scheme-option ${opt.id === lang ? 'scheme-option--active' : ''}`}
                  onClick={() => setLang(opt.id)}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
