import AuthPasswordField from '../components/AuthPasswordField'
import { passwordInputInfo } from '../lib/authPasswordInput'
import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import api from '../lib/api'
import { Brand } from '../components/ui'
import { useI18n } from '../i18n'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { useAuthFormAction } from '../lib/useAuthFormAction'
import { registerFailure, type RegisterFailure } from '../lib/authFailure'

export default function Register() {
  const [email, setEmail] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<RegisterFailure | null>(null)
  const location = useLocation()
  const action = useAuthFormAction(location.key)
  const ime = useImeFormGuard(action.scope)
  const loading = action.kind !== null
  const [registered, setRegistered] = useState(false)
  useEffect(() => { setError(null); setRegistered(false) }, [action.scope])
  const { t } = useI18n()

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (registered || ime.isSubmitBlocked(e) || !passwordInputInfo(password).valid) return
    const request = action.begin('register')
    if (!request) return
    setError(null)
    try {
      await api.post('/auth/register', { email, username, password })
      if (!request.isCurrent()) return
      setPassword('')
      setRegistered(true)
    } catch (failure) {
      if (request.isCurrent()) setError(registerFailure(failure))
    } finally { action.finish(request) }
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <h1 className="auth-heading"><Brand /></h1>
        <p className="auth-copy">
          {t('auth.registerTitle')}
        </p>

        <p className="auth-copy">{t('auth.localAccountHelp')}</p>
        {registered ? <div className="stack">
          <p role="status" className="notice notice--success">{t('auth.registerSuccess')}</p>
          <Link to="/login?registered=1" className="button button--primary">{t('auth.continueLogin')}</Link>
        </div> : <form {...ime.handlers} onSubmit={handleSubmit} className="form-grid" aria-busy={loading}>
          {error && (
            <div role="alert" className="notice notice--danger">
              {t(error === 'unknown' ? 'auth.registerUnconfirmed' : error === 'duplicate' ? 'auth.failure.duplicate' : error === 'validation' ? 'auth.failure.validation' : 'auth.registerFailed')}
            </div>
          )}

          <label htmlFor="register-email" className="field">
              {t('auth.email')}
            <input
              id="register-email"
              name="email"
              autoComplete="email"
              disabled={loading}
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>

          <label htmlFor="register-username" className="field">
              {t('auth.username')}
            <input
              id="register-username"
              name="username"
              autoComplete="username"
              disabled={loading}
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </label>

          <AuthPasswordField id="register-password" scope={action.scope} value={password} onChange={setPassword}
            disabled={loading} autoComplete="new-password" longHint/>

          <button
            type="submit"
            disabled={loading}
            className="button button--primary"
          >
            {loading ? t('auth.registering') : t('auth.register')}
          </button>
        </form>}

        <p className="auth-copy">
          {t('auth.hasAccount')}{' '}
          <Link to="/login" className="text-link">
            {t('auth.login')}
          </Link>
        </p>
      </div>
    </div>
  )
}
