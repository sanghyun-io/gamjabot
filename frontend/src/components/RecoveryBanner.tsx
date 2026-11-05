import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { eventsApi, type RoomEvent } from '../api/events'
import type { ToolActivity } from '../api/tools'
import { useI18n } from '../i18n'
import { subscribeContinuations, roomContinuations, beginContinuation, settleContinuation } from '../lib/roomContinuationWorkspace'

type RecoveryProps = {stopRevision?: number; blockedByStop?: boolean; events: readonly RoomEvent[]; ownerId: number; identity: string; generation: number; failureKey: string | null; roomId: number; uncertain: ToolActivity[]; failure: string | null; onContinue: () => void; onReviewConfiguration?: () => void; onViewFailure?: () => void; canContinue?: () => boolean; onViewContinuation?: (seq: number, eventId: string) => void}

export default function RecoveryBanner({ roomId, uncertain, failure, onContinue, onReviewConfiguration, onViewFailure, canContinue, identity, generation, failureKey, onViewContinuation, ownerId, events, blockedByStop = false, stopRevision = 0 }: RecoveryProps) {
  const inspectionScope = JSON.stringify([identity, roomId, failureKey, stopRevision, uncertain.map((item) => item.action_id).sort()])
  return <ScopedRecoveryBanner key={inspectionScope} stopRevision={stopRevision} blockedByStop={blockedByStop} events={events} ownerId={ownerId} identity={identity} generation={generation} failureKey={failureKey} roomId={roomId} uncertain={uncertain} failure={failure} onContinue={onContinue} onReviewConfiguration={onReviewConfiguration} onViewFailure={onViewFailure} canContinue={canContinue} onViewContinuation={onViewContinuation} />
}

function ScopedRecoveryBanner({ roomId, uncertain, failure, onContinue, onReviewConfiguration, onViewFailure, canContinue, identity, generation, failureKey, onViewContinuation, ownerId, events, blockedByStop = false, stopRevision = 0 }: RecoveryProps) {
  const [inspected, setInspected] = useState(false)
  const attempts = useSyncExternalStore(subscribeContinuations, () => roomContinuations(identity, roomId))
  const attempt = attempts.at(-1)
  const receivedMessages = useMemo(() => {
    const found = new Map<string, RoomEvent | null>()
    for (const event of events) {
      const key = event.idempotency_key
      if (event.room_id !== roomId || event.actor_type !== 'user' || event.actor_id !== ownerId || event.kind !== 'message' ||
          typeof key !== 'string' || !key || key.length > 200 || !Number.isSafeInteger(event.seq) || event.seq <= 0 ||
          typeof event.event_id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(event.event_id)) continue
      if (found.has(key) && found.get(key)?.event_id !== event.event_id) found.set(key, null)
      else if (!found.has(key)) found.set(key, event)
    }
    return found
  }, [events, roomId, ownerId])
  const receivedFor = (row: typeof attempts[number]) => {
    const event = receivedMessages.get(row.requestKey)
    return event && event.payload.content === row.content && (event.payload.output_kind ?? 'auto') === 'auto' ? event : null
  }
  const scope = JSON.stringify([failureKey, stopRevision, uncertain.map(item => item.action_id).sort()])
  const pending = attempt?.phase === 'sending'
  const error = attempt?.phase === 'unknown'
  const blocked = attempt?.phase === 'blocked' && attempt.scope === scope
  const accepted = attempt?.phase === 'accepted' && attempt.scope === scope
  const [resendReviewed, setResendReviewed] = useState(false)
  const sendLock = useRef(false)
  const mounted = useRef(true)
  const current = useRef(canContinue)
  current.current = canContinue
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const active = () => mounted.current && (current.current?.() ?? true)
  const { t, locale } = useI18n()
  const submit = async () => {
    if (!active() || sendLock.current || (uncertain.length > 0 && !inspected)) return
    const sent = beginContinuation(identity, generation, roomId, scope, resendReviewed)
    if (!sent) return
    sendLock.current = true
    setResendReviewed(false)
    try {
      const event = await eventsApi.send(roomId, sent.content, sent.requestKey)
      if (event?.room_id !== roomId || event.kind !== 'message' || event.actor_type !== 'user' || event.actor_id !== ownerId || event.idempotency_key !== sent.requestKey ||
          event.payload?.content !== sent.content || (event.payload?.output_kind ?? 'auto') !== 'auto' ||
          !Number.isSafeInteger(event.seq) || event.seq <= 0 || typeof event.event_id !== 'string' || !event.event_id) throw new Error('Unrecognized continuation receipt')
      settleContinuation(identity, generation, sent, 'accepted', { seq: event.seq, eventId: event.event_id })
      if (active()) { setInspected(false); onContinue() }
    } catch (failure) {
      const status = (failure as { response?: { status?: number } })?.response?.status
      settleContinuation(identity, generation, sent, status === 409 || status === 422 ? 'blocked' : 'unknown')
    } finally { sendLock.current = false }
  }
  const history = attempts.length > 0 && <details className="compact-details">
    <summary>{t('recovery.requestHistory')} · {attempts.length} · {t(attempt?.phase === 'sending' ? 'recovery.continuePending' : attempt?.phase === 'accepted' ? 'recovery.continueAccepted' : attempt?.phase === 'blocked' ? 'recovery.originalKeyBlocked' : 'recovery.historyUnknown')}</summary>
    <p className="field-hint">{t('recovery.navigationState')}</p>
    <p className="field-hint">{t('recovery.historyHelp')}</p>
    <ol className="stack stack--tight">{[...attempts].reverse().map(row => {
      const observed = row.phase === 'accepted' ? null : receivedFor(row)
      return <li key={row.id}>
        <time dateTime={row.startedAt}>{new Date(row.startedAt).toLocaleString(locale)}</time>
        {' · '}{t(row.phase === 'sending' ? 'recovery.continuePending' : row.phase === 'accepted' ? 'recovery.continueAccepted' : row.phase === 'blocked' ? 'recovery.originalKeyBlocked' : 'recovery.historyUnknown')}
        {row.phase === 'blocked' && <p className="field-hint">{t('recovery.originalKeyBlockedHelp')}</p>}
        {row.replayOf && <span className="metadata"> · {t('recovery.originalKeyAttempt')}</span>}
        {row.phase === 'accepted' && row.receipt && onViewContinuation && <div><button type="button" className="text-link" onClick={() => {
          if (active() && row.receipt) onViewContinuation(row.receipt.seq, row.receipt.eventId)
        }}>{t('recovery.openAccepted')} · #{row.receipt.seq}</button></div>}
        {observed && <div className="stack stack--tight"><p className="field-hint">{t('recovery.receivedMessageHelp')}</p>
          {onViewContinuation && <div><button type="button" className="text-link" onClick={() => {
            if (active()) onViewContinuation(observed.seq, observed.event_id)
          }}>{t('recovery.openReceivedMessage')} · #{observed.seq}</button></div>}
        </div>}
      </li>
    })}</ol>
  </details>
  if (!uncertain.length && !failure) return history ? <section className="notice stack stack--tight" aria-label={t('recovery.requestHistory')}>
    {(pending || error) && <div className="stack stack--tight">
      <p role={error ? 'alert' : 'status'}>{t(pending ? 'recovery.continuePendingHelp' : 'recovery.sameKeyUnknown')}</p>
      {error && <><label className="check-row"><input type="checkbox" checked={resendReviewed} disabled={blockedByStop || pending} onChange={event => setResendReviewed(event.target.checked)}/>{t('recovery.resendOriginalReview')}</label>
        <div><button type="button" className="button button--secondary button--compact" disabled={blockedByStop || !resendReviewed} onClick={submit}>{t('recovery.resendOriginal')}</button></div></>}
    </div>}
    {blockedByStop && <p role="status" className="field-hint">{t('chat.stopRequest.waitBeforeSend')}</p>}
    {history}
  </section> : null
  // A recorded failure is already explained at its place in the conversation.
  // The optional new continuation stays available without duplicating a second
  // error report above the transcript. Uncertain effects and unknown sends never collapse.
  const optionalContinuation = !uncertain.length && !pending && !error && !accepted && !blocked && !blockedByStop
  const recoveryContent = <>
    {!optionalContinuation && <h2 className="section-title">{t('recovery.title')}</h2>}
    {failure && <p>{failure}</p>}
    {failure && onViewFailure && <div><button type="button" className="text-link" onClick={onViewFailure}>{t('recovery.viewFailure')}</button></div>}
    {failure && onReviewConfiguration && <div className="stack stack--tight">
      <p className="field-hint">{t('recovery.configurationHelp')}</p>
      <button type="button" className="button button--secondary button--compact" disabled={pending} onClick={onReviewConfiguration}>{t('chat.ui.reviewConnection')}</button>
    </div>}
    {uncertain.length > 0 && <>
      <p>{t('recovery.warning')}</p>
      <details className="compact-details"><summary>{t('recovery.targets')} · {t('recovery.targetCount', { count: uncertain.length })}</summary>
        <ul>{uncertain.map((item) => <li key={item.action_id}>{item.family} · {item.target} · {item.expected_effect}</li>)}</ul>
        <p className="section-copy">{t('recovery.noRetry')}</p>
      </details>
      <label className="check-row"><input type="checkbox" checked={inspected} disabled={blockedByStop || pending} onChange={(event) => setInspected(event.target.checked)} />{t('recovery.confirm')}</label>
    </>}
    <p className="field-hint">{t('recovery.keyedEffect')}</p>
    {attempt && (pending || error || accepted) && <p className="field-hint">{t('recovery.navigationState')}</p>}
    {error && <div className="stack stack--tight">
      <p role="alert">{t('recovery.sameKeyUnknown')}</p>
      <label className="check-row"><input type="checkbox" checked={resendReviewed} disabled={blockedByStop || pending} onChange={event => setResendReviewed(event.target.checked)} />{t('recovery.resendOriginalReview')}</label>
    </div>}
    <button type="button" aria-label={t(pending ? 'recovery.continuePending' : blocked ? 'recovery.originalKeyBlocked' : error ? 'recovery.resendOriginal' : 'recovery.continueAria')} className="button button--secondary" disabled={blockedByStop || pending || accepted || blocked || (uncertain.length > 0 && !inspected) || (error && !resendReviewed)} onClick={submit}>{t(pending ? 'recovery.continuePending' : accepted ? 'recovery.continueAccepted' : blocked ? 'recovery.originalKeyBlocked' : error ? 'recovery.resendOriginal' : 'recovery.continue')}</button>
    {pending && <p role="status">{t('recovery.continuePendingHelp')}</p>}
    {accepted && <p role="status">{t('recovery.continueAcceptedHelp')}</p>}
    {blockedByStop && <p role="status" className="field-hint">{t('chat.stopRequest.waitBeforeSend')}</p>}
    {history}
  </>
  return optionalContinuation
    ? <details className="compact-details recovery-banner recovery-banner--optional"><summary>{t('recovery.continue')}</summary><div className="stack stack--tight">{recoveryContent}</div></details>
    : <section role="alert" className="notice notice--danger stack stack--tight recovery-banner">{recoveryContent}</section>
}
