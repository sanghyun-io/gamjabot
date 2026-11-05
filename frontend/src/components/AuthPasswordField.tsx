import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import { passwordInputInfo } from '../lib/authPasswordInput'
import { useAuthStore } from '../stores/authStore'

export default function AuthPasswordField({ id, scope, value, onChange, disabled, autoComplete, longHint = false }: {
  id: string; scope: string; value: string; onChange: (value: string) => void; disabled: boolean;
  autoComplete: 'current-password' | 'new-password'; longHint?: boolean
}) {
  const { t } = useI18n(), input = useRef<HTMLInputElement>(null)
  const ownerContext = useAuthStore(state => `${state.epoch}:${state.user?.id ?? 0}`)
  const current = () => { const auth = useAuthStore.getState(); return ownerContext === `${auth.epoch}:${auth.user?.id ?? 0}` && scope.endsWith(`:${ownerContext}`) }
  const [visibility, setVisibility] = useState({ scope, ownerContext, shown: false })
  const shown = visibility.scope === scope && visibility.ownerContext === ownerContext && visibility.shown && !disabled && current()
  const info = passwordInputInfo(value)
  const issue = info.malformed ? 'auth.passwordMalformed' : info.bytes !== null && info.bytes > 72 ? 'auth.passwordTooLong' : null
  useLayoutEffect(() => { input.current?.setCustomValidity(issue ? t(issue) : '') }, [issue, t])
  useEffect(() => {
    const hide = () => setVisibility({ scope, ownerContext, shown: false })
    const visibilityChanged = () => { if (document.visibilityState !== 'visible') hide() }
    window.addEventListener('blur', hide)
    document.addEventListener('visibilitychange', visibilityChanged)
    return () => { window.removeEventListener('blur', hide); document.removeEventListener('visibilitychange', visibilityChanged) }
  }, [scope, ownerContext])
  useEffect(() => { setVisibility({ scope, ownerContext, shown: false }) }, [scope, ownerContext, disabled])
  return <div className="field">
    <div className="split"><label htmlFor={id}>{t('auth.password')}</label>
      <button type="button" className="text-link" disabled={disabled} aria-controls={id} aria-pressed={shown}
        onClick={() => { if (!disabled && current() && document.visibilityState === 'visible') setVisibility(previous => ({ scope, ownerContext, shown: !(previous.scope === scope && previous.ownerContext === ownerContext && previous.shown) })) }}>{t(shown ? 'auth.hidePassword' : 'auth.showPassword')}</button>
    </div>
    <input ref={input} id={id} name="password" disabled={disabled} type={shown ? 'text' : 'password'} autoComplete={autoComplete}
      autoCapitalize="none" autoCorrect="off" spellCheck={false} value={value} onChange={event => onChange(event.target.value)} required
      aria-invalid={!!issue || undefined} aria-describedby={`${id}-hint ${id}-bytes${issue ? ` ${id}-error` : ''}`}/>
    <span id={`${id}-hint`} className="field-hint">{t(longHint ? 'auth.passwordHintLong' : 'auth.passwordHint')}</span>
    <span id={`${id}-bytes`} className="field-hint">{info.bytes === null ? '' : t('auth.passwordBytes', { count: info.bytes })}</span>
    {issue && <span id={`${id}-error`} className="field-hint notice--danger">{t(issue)}</span>}
    <span className="field-hint">{t('auth.passwordVisibilityHelp')}</span>
  </div>
}
