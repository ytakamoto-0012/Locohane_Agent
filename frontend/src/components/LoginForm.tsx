import { useContext, useState } from 'react';
import { ChainlitContext, useAuth } from '@chainlit/react-client';
import { LANGUAGE_OPTIONS, useI18n } from '../i18n';

/**
 * config.ini の [auth] enabled=true のときに表示するログインフォーム。
 * password欄は required にしない（[auth] require_password=false の場合、
 * バックエンドの password_auth_callback がパスワードの内容を問わず通すため、
 * 空欄のまま送信できる必要がある）。
 */
export function LoginForm() {
  const chainlitApi = useContext(ChainlitContext);
  const { setUserFromAPI } = useAuth();
  const { lang, setLang, t } = useI18n();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const formData = new FormData();
      formData.append('username', username);
      // chainlit(FastAPI の OAuth2PasswordRequestForm)は空文字列の password
      // フィールドを「未指定」として 422 で弾くため、空欄のままでも送れるよう
      // ダミー値で埋める（[auth] require_password=false 運用では
      // password_auth_callback 側で値そのものは見ないため問題ない）。
      formData.append('password', password || ' ');
      await chainlitApi.passwordAuth(formData);
      await setUserFromAPI();
    } catch {
      setError(t('login.invalid'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={handleSubmit}>
        <h1 className="login-title">{t('login.title')}</h1>
        {error && <div className="login-error">{error}</div>}
        <label className="login-field">
          <span className="login-label">{t('login.username')}</span>
          <input
            className="login-input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
          />
        </label>
        <label className="login-field">
          <span className="login-label">{t('login.password')}</span>
          <input
            className="login-input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <button className="login-submit" type="submit" disabled={submitting}>
          {submitting ? t('login.submitting') : t('login.submit')}
        </button>
        <div className="login-language">
          {LANGUAGE_OPTIONS.map((opt) => (
            <button
              key={opt.id}
              type="button"
              className={opt.id === lang ? 'login-language--active' : undefined}
              onClick={() => setLang(opt.id)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </form>
    </div>
  );
}
