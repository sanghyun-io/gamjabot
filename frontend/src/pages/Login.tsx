import { clearSessionOnly, markSessionOnly, type SessionOnlyStore } from '../lib/sessionOnlyLogin'
import AuthPasswordField from '../components/AuthPasswordField'
import { passwordInputInfo } from '../lib/authPasswordInput'
import { useEffect, useId, useState } from 'react'
import { useNavigate, Link, useSearchParams, useLocation } from 'react-router-dom'
import { useAuthStore } from '../stores/authStore'
import api from '../lib/api'
import { Brand } from '../components/ui'
import { useI18n } from '../i18n'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { useAuthFormAction } from '../lib/useAuthFormAction'
import { loginFailure, type LoginFailure } from '../lib/authFailure'

export default function Login({ onRestore, notice = '', sessionOnlyStore }: { onRestore?: (isCurrent: () => boolean) => Promise<void>; notice?: string; sessionOnlyStore?: SessionOnlyStore }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [rememberDevice, setRememberDevice] = useState(false)
  const [sessionOnly, setSessionOnly] = useState(false)
  const [storageError, setStorageError] = useState(false)
  const sessionHintId = useId()
  const [error, setError] = useState<LoginFailure | 'restore' | null>(null)
  const location = useLocation()
  const action = useAuthFormAction(location.key)
  const ime = useImeFormGuard(action.scope)
  const loading = action.kind === 'login', restoring = action.kind === 'restore', busy = action.kind !== null
  useEffect(() => { setError(null) }, [action.scope])
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { setAuth } = useAuthStore()
  const { t, locale } = useI18n()
  const copy = (ko: string, en: string) => locale === 'ko' ? ko : en

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (ime.isSubmitBlocked(e) || !passwordInputInfo(password).valid) return
    const request = action.begin('login')
    if (!request) return
    setError(null); setStorageError(false)
    if (sessionOnly && !markSessionOnly(sessionOnlyStore)) { setStorageError(true); action.finish(request); return }
    let stage: 'login' | 'profile' = 'login'
    try {
      const response = await api.post(sessionOnly ? '/auth/login/session-only' : '/auth/login', sessionOnly
        ? { email, password, remember_device: false, preserve_remembered_login: false, preserve_saved_login_for_session: true }
        : { email, password, remember_device: rememberDevice })
      if (!request.isCurrent()) return
      const accessToken = response.data.access_token
      if (typeof accessToken !== 'string' || !accessToken) throw new Error('Login response unavailable')
      stage = 'profile'
      const profile = await api.get('/auth/me', { headers: { Authorization: `Bearer ${accessToken}` } })
      if (!request.isCurrent()) return
      if (!Number.isSafeInteger(profile.data?.id) || profile.data.id < 1) throw new Error('Login profile unavailable')
      setPassword('')
      if (!sessionOnly) clearSessionOnly(sessionOnlyStore)
      setAuth(accessToken, profile.data, { sessionOnly })
      navigate('/')
    } catch (failure) {
      if (request.isCurrent()) setError(loginFailure(failure, stage, rememberDevice, sessionOnly))
    } finally { action.finish(request) }
  }

  const restoreSavedLogin = async () => {
    if (!onRestore) return
    const request = action.begin('restore')
    if (!request) return
    setError(null)
    try { await onRestore(request.isCurrent) }
    catch { if (request.isCurrent()) setError('restore') }
    finally { action.finish(request) }
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <h1 className="auth-heading"><Brand /></h1>
        <p className="auth-copy">
          {t('auth.loginTitle')}
        </p>
        <p className="auth-copy">{t('auth.localAccountHelp')}</p>

        <form {...ime.handlers} onSubmit={handleSubmit} className="form-grid" aria-busy={busy}>
          {searchParams.get('registered') === '1' && <p role="status" className="notice notice--success">{t('auth.registerHandoff')}</p>}
          {notice && <p role="status" className="notice notice--warning">{notice}{' '}<Link to="/reports" className="text-link">{t('app.reportProblem')}</Link></p>}
          {storageError && <p role="alert" className="notice notice--danger">{copy('현재 화면에서 임시 로그인 상태를 안전하게 유지할 수 없어 요청을 보내지 않았습니다.', 'This window cannot safely keep the temporary sign-in state, so no request was sent.')}</p>}
          {error && <div role="alert" className="notice notice--danger stack stack--tight">
            <p>{error === 'sessionOnlyUnsupported' ? copy('현재 앱 서비스에서는 이 로그인 방식을 사용할 수 없습니다.', 'This sign-in option is unavailable in the current app service.') : error === 'savedLoginMismatch' ? copy('입력한 계정이 저장된 로그인과 다릅니다. 같은 계정으로 로그인해 주세요.', 'This account differs from the saved login. Sign in with the same account.') : error === 'savedLoginChanged' ? copy('저장된 로그인을 확인할 수 없거나 변경되었습니다. 다시 확인해 주세요.', 'The saved login could not be confirmed or has changed. Check it again.') : t(error === 'restore' ? 'auth.savedLoginUnavailable' : error === 'unknown' ? 'auth.loginUnconfirmed'
              : error === 'credentials' ? 'auth.failure.credentials' : error === 'rememberLocked' ? 'auth.failure.rememberLocked'
              : error === 'rememberUnavailable' ? 'auth.failure.rememberUnavailable' : 'auth.loginFailed')}</p>
            {(error === 'rememberLocked' || error === 'rememberUnavailable') && <>
              <p className="field-hint">{t('auth.failure.rememberHelp')}</p>
              {rememberDevice && <button type="button" className="button button--secondary button--compact" disabled={busy} onClick={() => {
                if (!busy) setRememberDevice(false)
              }}>{t('auth.failure.disableRemember')}</button>}
              <Link to="/reports" className="text-link">{t('app.reportProblem')}</Link>
            </>}
          </div>}

          <label htmlFor="login-email" className="field">
              {t('auth.email')}
            <input
              id="login-email"
              name="email"
              autoComplete="username"
              disabled={busy}
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>

          <AuthPasswordField id="login-password" scope={action.scope} value={password} onChange={setPassword}
            disabled={busy} autoComplete="current-password"/>

          <label className="check-row">
            <input type="checkbox" disabled={busy} checked={rememberDevice} aria-describedby={!rememberDevice && !sessionOnly ? sessionHintId : undefined} onChange={(event) => { setRememberDevice(event.target.checked); if (event.target.checked) setSessionOnly(false) }} />
            <span>{t('auth.remember')} <span className="field-hint">{t('auth.rememberHint')}</span></span>
          </label>
          {!rememberDevice && !sessionOnly && <p id={sessionHintId} className="field-hint">{t('auth.sessionExpiryHint')}</p>}
          <label className="check-row">
            <input type="checkbox" disabled={busy} checked={sessionOnly} onChange={(event) => { setSessionOnly(event.target.checked); if (event.target.checked) setRememberDevice(false) }}/>
            <span>{copy('기존 저장 로그인은 유지하고 이번 화면에서만 로그인', 'Keep the saved login and sign in only for this window')}</span>
          </label>
          {sessionOnly && <p className="field-hint">{copy('같은 계정만 사용할 수 있습니다. 이 화면을 새로 열거나 세션이 만료되면 다시 로그인해야 합니다. 저장된 로그인은 유지되며 앱을 다시 시작하면 복원이 시도될 수 있습니다. 명시적으로 로그아웃하면 저장된 로그인도 해제됩니다.', 'Use the same account. Reloading this window or session expiry requires signing in again. The saved login is retained and may be restored after restarting the app. Explicit sign-out also removes the saved login.')}</p>}


          <button
            type="submit"
            disabled={busy}
            className="button button--primary"
          >
            {loading ? t('auth.loggingIn') : t('auth.login')}
          </button>
          {onRestore && <button type="button" disabled={busy} onClick={restoreSavedLogin}
            className="button button--secondary">
            {restoring ? t('auth.loggingIn') : t('auth.continueSaved')}
          </button>}
        </form>

        <p className="auth-copy">
          {t('auth.noAccount')}{' '}
          <Link to="/register" className="text-link">
            {t('auth.register')}
          </Link>
        </p>
        {!notice && <p className="auth-copy"><Link to="/reports" className="text-link">{t('app.reportProblem')}</Link></p>}
      </div>
    </div>
  )
}
