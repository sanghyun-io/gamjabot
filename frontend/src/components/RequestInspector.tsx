import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ArrowRight, X } from 'lucide-react'
import { requestInspectorApi, type RequestInspectorResponse } from '../api/requestInspector'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import TargetedRetry from './TargetedRetry'
import type { RequestRetryResult } from '../api/requestRetries'
import RequestResult from './RequestResult'
import type { FinalizeResult } from '../api/requestFinalize'
import ServerTime from './ServerTime'
import { useDialogFocus } from '../lib/useDialogFocus'
import { directoryViewGeneration } from '../lib/directoryViewMemory'

interface Props {
  roomId: number
  triggerEventId: string | null
  ownerId: number
  authEpoch: number
  onClose: () => void
  readRequest?: typeof requestInspectorApi.get
  onNavigate: (href: string) => void
  onViewSource?: (seq: number, eventId: string) => void
  onRetryCreated?: (result: RequestRetryResult) => void
  onFinalizeCreated?: (result: FinalizeResult, canNavigate?: () => boolean) => void
}

export default function RequestInspector({ roomId, triggerEventId, ownerId, authEpoch, onClose, onNavigate, onViewSource, onRetryCreated = () => {}, onFinalizeCreated = () => {}, readRequest = requestInspectorApi.get }: Props) {
  const [data, setData] = useState<RequestInspectorResponse | null>(null)
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const dialog = useRef<HTMLDivElement>(null)
  const backdrop = useRef<HTMLDivElement>(null)
  const generation = useRef(0)
  const viewGeneration = useRef(directoryViewGeneration()).current
  const { t, formatNumber, locale } = useI18n()
  const copy = (ko: string, en: string) => locale === 'ko' ? ko : en

  const load = async () => {
    if (!triggerEventId) return
    const sent = ++generation.current
    const identity = `${ownerId}:${authEpoch}:${roomId}:${triggerEventId}`
    setState('loading'); setData(null)
    try {
      const result = await readRequest(roomId, triggerEventId)
      const auth = useAuthStore.getState()
      if (sent !== generation.current || identity !== `${auth.user?.id || 0}:${auth.epoch}:${roomId}:${triggerEventId}`) return
      setData(result); setState('ready')
    } catch {
      const auth = useAuthStore.getState()
      if (sent === generation.current && identity === `${auth.user?.id || 0}:${auth.epoch}:${roomId}:${triggerEventId}`) setState('error')
    }
  }

  useEffect(() => { void load(); return () => { generation.current += 1 } }, [roomId, triggerEventId, ownerId, authEpoch]) // eslint-disable-line react-hooks/exhaustive-deps
  useLayoutEffect(() => {
    const prior = document.body.style.overflow
    const background = [...document.body.children].filter((node) => node !== backdrop.current).map((node) => ({
      node, inert: node.hasAttribute('inert'), hidden: node.getAttribute('aria-hidden'),
    }))
    for (const item of background) { item.node.setAttribute('inert', ''); item.node.setAttribute('aria-hidden', 'true') }
    document.body.style.overflow = 'hidden'
    // Restore the background before the shared hook returns focus to the trigger.
    return () => {
      document.body.style.overflow = prior
      for (const item of background) {
        if (!item.inert) item.node.removeAttribute('inert')
        if (item.hidden === null) item.node.removeAttribute('aria-hidden')
        else item.node.setAttribute('aria-hidden', item.hidden)
      }
    }
  }, [])
  useDialogFocus(dialog, onClose)

  const canNavigate = () => {
    const auth = useAuthStore.getState(), root = dialog.current
    if (!auth.token || auth.user?.id !== ownerId || auth.epoch !== authEpoch || directoryViewGeneration() !== viewGeneration
        || state !== 'ready' || data?.room_id !== roomId || data.request.event_id !== triggerEventId
        || document.visibilityState !== 'visible' || !document.hasFocus() || !root?.isConnected
        || !root.getClientRects().length || root.closest('[hidden], [inert], [aria-hidden="true"]')) return false
    const modals = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).filter(node =>
      node.getClientRects().length > 0 && !node.closest('[hidden], [inert], [aria-hidden="true"]') && getComputedStyle(node).visibility !== 'hidden')
    return modals.at(-1) === root
  }
  const eventValid = (seq: number, eventId: string) => Number.isSafeInteger(seq) && seq > 0 && typeof eventId === 'string'
    && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(eventId)
  const go = (href: string) => { if (canNavigate()) { onClose(); onNavigate(href) } }
  const readEvent = (seq: number, eventId: string) => {
    if (!canNavigate() || !eventValid(seq, eventId)) return
    if (onViewSource) onViewSource(seq, eventId)
    else go(`/chat/${roomId}?${new URLSearchParams({ seq: String(seq), event: eventId }).toString()}`)
  }
  // Older servers and invalid metadata do not establish whether a check happened.
  const checks = data?.collaboration_checks
  const checkCount = data?.collaboration_check_count
  const checksValid = Array.isArray(checks) && checks.length <= 128 && Number.isSafeInteger(checkCount)
    && (checkCount as number) >= checks.length && typeof data?.collaboration_checks_truncated === 'boolean'
    && checks.every(item => item && (item.kind === 'teammates' || item.kind === 'bots')
      && (item.state === 'failed' ? item.shown_count === null && item.has_more === null
        : item.state === 'completed' && Number.isSafeInteger(item.shown_count) && (item.shown_count as number) >= 0 && typeof item.has_more === 'boolean'))
  return createPortal(<div ref={backdrop} className="modal-backdrop"><div ref={dialog} role="dialog" aria-modal="true" aria-labelledby="request-inspector-title" className="modal-panel request-inspector stack">
    <header className="request-inspector__header"><div><h2 id="request-inspector-title">{t('inspector.title')}</h2>
      <p className="section-copy">{t('inspector.description')}</p></div>
      <button data-dialog-initial type="button" className="icon-button" aria-label={t('common.close')} onClick={onClose}><X aria-hidden="true" /></button></header>
    {state === 'loading' && <p role="status">{t('inspector.loading')}</p>}
    {state === 'error' && <div className="stack stack--tight"><p role="alert" className="notice notice--warning">{t('inspector.loadFailure')}</p>
      <button type="button" className="button button--secondary" onClick={() => void load()}>{t('common.retry')}</button></div>}
    {state === 'ready' && data && <div className="request-inspector__body stack">
      <section className="stack stack--tight"><div className="request-inspector__request"><div><strong>{data.request.actor_name_snapshot}</strong>
        <p>{data.request.preview}</p><ServerTime className="metadata" value={data.request.created_at}/></div>
        <button type="button" className="button button--secondary button--compact" disabled={!eventValid(data.request.seq, data.request.event_id)} onClick={() => readEvent(data.request.seq, data.request.event_id)}>{t('inspector.goToRequest')}<ArrowRight size={15} aria-hidden="true" /></button></div>
        <p className="status-text">{t(data.completion.state === 'completed' && data.completion.outcome_status === 'failed' ? 'chat.state.failed' : data.completion.state === 'completed' && data.completion.outcome_status === 'partial' ? 'chat.activity.partial' : data.completion.state === 'completed' && data.completion.outcome_status !== 'succeeded' ? 'inspector.phase.unknown' : `inspector.phase.${data.completion.state}`)}</p>
        <p>{data.completion.outcome_status === 'succeeded' && !data.completion.result_available
          ? t('inspector.completion.noResult')
          : t(`inspector.completion.outcome.${data.completion.outcome_status}`)}</p>
        <p className="metadata">{t('inspector.completion.rounds', { count: data.completion.completed_rounds })}
          {data.completion.pending_agent_count > 0 && <> · {t('inspector.completion.pendingAgents', { count: data.completion.pending_agent_count })}</>}</p>
        <p>{t('inspector.agents', { count: data.execution.selected_agent_count })}</p>
        <ul className="request-inspector__agents">{data.execution.selected_agents.map((agent) => <li key={agent.agent_id}>
          <span>{agent.name_snapshot || t('inspector.agentFallback', { id: agent.agent_id })}</span>
          <span className="metadata">{agent.live_state ? t(`inspector.live.${agent.live_state}`) : t('inspector.live.notCurrent')}</span>
        </li>)}</ul>
      </section>
      <section className="stack stack--tight"><h3>{copy('동료 확인', 'Colleague checks')}</h3>
        {!checksValid ? <p className="section-copy">{copy('확인 기록을 사용할 수 없습니다.', 'Check history is unavailable.')}</p>
          : !checks?.length ? <p className="section-copy">{copy('표시할 확인 기록이 없습니다.', 'There are no check records to show.')}</p>
          : checks?.map((check, index) => <article key={`${check.event_id}:${check.seq}:${index}`} className="request-inspector__row"><div>
            <strong>{check.kind === 'teammates' ? copy('동료 목록 확인', 'Checked colleagues') : copy('Bot 목록 확인', 'Checked Bots')}</strong>
            <p>{check.state === 'failed' ? copy('확인하지 못했습니다.', 'The check failed.')
              : <>{copy('확인 완료', 'Check completed')} · {formatNumber(check.shown_count as number)}{copy('개 표시', ' shown')}{check.has_more && <> · {copy('더 있음', 'More available')}</>}</>}</p>
          </div></article>)}
        {checksValid && data.collaboration_checks_truncated && <p className="notice notice--warning">{copy('일부 기록만 표시합니다.', 'Only some records are shown.')}</p>}
      </section>
      <section className="stack stack--tight"><h3>{t('inspector.outcomes', { count: data.outcome_count })}</h3>
        {data.outcomes.length === 0 && <p className="section-copy">{t('inspector.noOutcomes')}</p>}
        {data.outcomes.map((outcome) => <article key={outcome.event_id} className="request-inspector__row"><div>{outcome.actor_type === 'system' ? <><strong>{t('inspector.replySystemFailure')}</strong>
          <p>{t('inspector.replySystemTarget', { name: outcome.agent_name_snapshot || t('inspector.agentFallback', { id: outcome.agent_id }) })}</p></> : <><strong>{outcome.agent_name_snapshot || t('inspector.agentFallback', { id: outcome.agent_id })}</strong>
          <p>{t(outcome.summary_code === 'failure_teammate_result_incomplete' ? 'chat.failure.teammateResultIncomplete' : `inspector.outcome.${outcome.summary_code}`)} · {t('inspector.round', { round: outcome.round })}</p></>}</div>
          <button type="button" className="text-link" disabled={!eventValid(outcome.seq, outcome.event_id)} onClick={() => readEvent(outcome.seq, outcome.event_id)}>{t('inspector.goToEvent')}</button>
          {!data.read_only && data.completion.state === 'completed' && data.completion.outcome_status === 'succeeded' && outcome.actor_type !== 'system' && outcome.kind === 'message' && outcome.phase === 'succeeded' && eventValid(outcome.seq, outcome.event_id) && eventValid(data.request.seq, data.request.event_id) && <button type="button" className="text-link" onClick={() => go(`/skills?${new URLSearchParams({room:String(roomId),request:data.request.event_id,result:outcome.event_id,agent:String(outcome.agent_id)})}`)}>{t('skills.openDraft')}</button>}
          <details><summary>{t('inspector.technical')}</summary><dl>
            <dt>{t('inspector.phaseLabel')}</dt><dd>{outcome.phase}</dd>
            <dt>{t('inspector.historicalModel')}</dt><dd>{outcome.model_use ? `${outcome.model_use.provider_kind} · ${outcome.model_use.model_id}` : t('inspector.unavailable')}</dd>
            {outcome.model_use && <><dt>{t('inspector.sourceThrough')}</dt><dd>{formatNumber(outcome.model_use.watermark_seq)}</dd></>}
          </dl></details>
          {outcome.kind === 'failure' && !data.read_only && <TargetedRetry roomId={roomId} failureEventId={outcome.event_id}
            ownerId={ownerId} authEpoch={authEpoch} teammateRecovery={outcome.summary_code === 'failure_teammate_result_incomplete'} onViewSource={onViewSource} onCreated={(result) => { onRetryCreated(result); void load() }} />}
        </article>)}
        {data.outcomes_truncated && <p className="notice notice--warning">{t('inspector.truncated')}</p>}
      </section>
      <section className="stack stack--tight"><h3>{t('inspector.actions', { count: data.action_count })}</h3>
        {data.actions.length === 0 && <p className="section-copy">{t('inspector.noActions')}</p>}
        {data.actions.map((action) => <article key={action.action_id} className="request-inspector__row"><div><strong>{t(`inspector.action.${action.summary_code}`)}</strong>
          <p>{t(`inspector.actionPhase.${action.phase}`)}</p></div><button type="button" className="text-link" onClick={() => go(action.href)}>{t('inspector.goToEvent')}</button>
          <details><summary>{t('inspector.technical')}</summary><dl><dt>{t('inspector.phaseLabel')}</dt><dd>{action.state}</dd>
            <dt>{t('inspector.requestedAt')}</dt><dd><ServerTime value={action.requested_at}/></dd></dl></details></article>)}
        {data.actions_truncated && <p className="notice notice--warning">{t('inspector.truncated')}</p>}
      </section>
      {!data.read_only && ['completed', 'cancelled', 'interrupted'].includes(data.completion.state) && <RequestResult
        roomId={roomId} sourceRequestId={data.request.event_id} ownerId={ownerId} authEpoch={authEpoch}
        selectedAgents={data.execution.selected_agents.map(({ agent_id, name_snapshot }) => ({ agent_id, name_snapshot }))}
        onViewSource={onViewSource} onNavigate={go} onCreated={onFinalizeCreated} />}
      <details><summary>{t('inspector.policy')}</summary>{data.execution.policy ? <dl><dt>{t('chat.policy.rounds')}</dt><dd>{formatNumber(data.execution.policy.max_rounds)}</dd>
        <dt>{t('chat.policy.tools')}</dt><dd>{formatNumber(data.execution.policy.max_tool_calls)}</dd><dt>{t('chat.policy.executor')}</dt><dd>{formatNumber(data.execution.policy.max_executor_seconds)}</dd></dl>
        : <p>{t('inspector.policyUnknown')}</p>}</details>
      {data.read_only && <p className="metadata">{t('inspector.readOnly')}</p>}
    </div>}
  </div></div>, document.body)
}
