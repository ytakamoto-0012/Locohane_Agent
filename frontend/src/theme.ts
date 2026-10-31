export interface AccentPreset {
  id: string;
  accent: string;
  accentSoft: string;
}

export const ACCENT_PRESETS: AccentPreset[] = [
  { id: 'purple', accent: '#6d5ef8', accentSoft: 'rgba(109, 94, 248, 0.14)' },
  { id: 'blue', accent: '#3b82f6', accentSoft: 'rgba(59, 130, 246, 0.14)' },
  { id: 'green', accent: '#10b981', accentSoft: 'rgba(16, 185, 129, 0.14)' },
  { id: 'orange', accent: '#f97316', accentSoft: 'rgba(249, 115, 22, 0.14)' },
  { id: 'pink', accent: '#ec4899', accentSoft: 'rgba(236, 72, 153, 0.14)' },
  { id: 'teal', accent: '#0ea5a4', accentSoft: 'rgba(14, 165, 164, 0.14)' }
];

const STORAGE_KEY = 'la-accent-preset';

export function getStoredAccentId(): string {
  return localStorage.getItem(STORAGE_KEY) || ACCENT_PRESETS[0].id;
}

export function applyAccent(id: string): void {
  const preset = ACCENT_PRESETS.find((p) => p.id === id) || ACCENT_PRESETS[0];
  document.documentElement.style.setProperty('--accent', preset.accent);
  document.documentElement.style.setProperty('--accent-soft', preset.accentSoft);
  localStorage.setItem(STORAGE_KEY, preset.id);
}

export type ColorScheme = 'system' | 'light' | 'dark';

const SCHEME_KEY = 'la-color-scheme';
const darkMediaQuery = () => window.matchMedia('(prefers-color-scheme: dark)');

export function getStoredScheme(): ColorScheme {
  const raw = localStorage.getItem(SCHEME_KEY);
  return raw === 'light' || raw === 'dark' || raw === 'system' ? raw : 'system';
}

function resolveScheme(scheme: ColorScheme): 'light' | 'dark' {
  return scheme === 'system' ? (darkMediaQuery().matches ? 'dark' : 'light') : scheme;
}

export function applyScheme(scheme: ColorScheme): void {
  document.documentElement.dataset.theme = resolveScheme(scheme);
  localStorage.setItem(SCHEME_KEY, scheme);
}
