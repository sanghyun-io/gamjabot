import { useEffect, useRef, useState } from 'react'
import type { AuthOperationResponse } from '../../api/connections'
import { useI18n } from '../../i18n'
import { useAuthStore } from '../../stores/authStore'

export interface AuthOperationCopy {
  browserInstruction: string
  deviceInstruction: string
  userCode: string
  copy: string
  copied: string
  cancel: string
  cancelling: string
  pending: string
  finalizing: string
  succeeded: string
  cancelled: string
  expired: string
  failed: string
}

function DeviceCodeCopy({ identity, code, copy }: { identity: string; code: string; copy: AuthOperationCopy }) {
  const { t } = useI18n()
  const [phase, setPhase] = useState<'idle' | 'copying' | 'copied' | 'failed'>('idle')
  const mounted = useRef(false), pending = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const current = () => { const state = useAuthStore.getState(); return mounted.current && !!state.token && `${state.user?.id || 0}:${state.epoch}` === identity }
  const copyCode = async () => {
    if (!code || pending.current || !current()) return
    pending.current = true; setPhase('copying')
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable')
      // Explicit write only; never read, log, persist or automatically clear the clipboard.
      // Scope guards qualify feedback; an already-started browser clipboard write is not cancellable.
      await navigator.clipboard.writeText(code)
      if (current()) setPhase('copied')
    } catch { if (current()) setPhase('failed') }
    finally { pending.current = false }
  }
  return <div className="stack stack--tight">
    <p>{copy.userCode} <strong className="auth-operation__code">{code}</strong></p>
    <button type="button" className="button button--secondary button--compact" disabled={phase === 'copying' || !code} onClick={() => void copyCode()}>
      {phase === 'copying' ? t('connections.authCodeCopying') : phase === 'copied' ? copy.copied : copy.copy}
    </button>
    {phase === 'copied' && <p role="status">{copy.copied}</p>}
    {phase === 'failed' && <><p role="alert" className="notice notice--warning">{t('connections.authCodeCopyFailed')}</p>
      <label className="field">{t('connections.authCodeManual')}<input type="text" readOnly autoComplete="off" autoCapitalize="off" spellCheck={false} value={code} onFocus={event => event.currentTarget.select()} /></label>
    </>}
    <p className="field-hint">{t('connections.authCodeCopyHelp')}</p>
  </div>
}

export function AuthOperationStatus({ identity, operation, copy, cancelling, onCancel, onTerminalFocus }: {
  identity: string
  operation: AuthOperationResponse
  copy: AuthOperationCopy
  cancelling?: boolean
  onCancel: () => void
  onTerminalFocus?: () => void
}) {
  const { t, formatDateTime } = useI18n()
  const statusRef = useRef<HTMLParagraphElement>(null), rootRef = useRef<HTMLDivElement>(null)
  const terminalFocus = useRef(onTerminalFocus)
  terminalFocus.current = onTerminalFocus
  const terminal = ['succeeded', 'cancelled', 'expired', 'interrupted', 'failed'].includes(operation.state)
  const cancellable = !terminal && operation.state !== 'finalizing'
  useEffect(() => {
    // Poll responses and fresh callback objects must not keep moving keyboard focus.
    if (!terminal) { statusRef.current?.focus(); return }
    const active = document.activeElement
    if (active !== document.body && !rootRef.current?.contains(active)) return
    if (terminalFocus.current) terminalFocus.current()
    else statusRef.current?.focus()
  }, [operation.operation_id, terminal])
  const progress = !terminal ? operation : undefined
  const deviceCode = progress && 'user_code' in progress ? progress.user_code : undefined
  const expiry = progress && 'expires_at' in progress ? progress.expires_at : null
  const status = operation.state === 'succeeded' ? copy.succeeded
    : operation.state === 'cancelled' ? copy.cancelled
      : operation.state === 'expired' ? copy.expired
        : terminal ? copy.failed : operation.state === 'finalizing' ? copy.finalizing : copy.pending
  return <div ref={rootRef} className={`auth-operation notice ${operation.state === 'succeeded' ? 'notice--success' : terminal ? 'notice--warning' : 'notice--info'} stack stack--tight`}>
    <p ref={statusRef} role="status" tabIndex={-1}>{status}</p>
    {progress && 'browser_url' in progress && progress.browser_url && <p>{copy.browserInstruction} <a href={progress.browser_url} target="_blank" rel="noreferrer">{progress.browser_url}</a></p>}
    {progress && 'verification_uri' in progress && progress.verification_uri && <div className="stack stack--tight">
      <p>{copy.deviceInstruction} <a href={progress.verification_uri} target="_blank" rel="noreferrer">{progress.verification_uri}</a></p>
      {deviceCode && <DeviceCodeCopy key={JSON.stringify([identity, operation.operation_id, deviceCode, progress.verification_uri, expiry])} identity={identity} code={deviceCode} copy={copy} />}
    </div>}
    {expiry && !Number.isNaN(Date.parse(expiry)) && <p className="metadata">{t('connections.authExpiresAt')} <time dateTime={expiry}>{formatDateTime(expiry, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short' })}</time></p>}
    {cancellable && <button type="button" className="button button--secondary button--compact" disabled={cancelling} onClick={onCancel}>{cancelling ? copy.cancelling : copy.cancel}</button>}
  </div>
}
