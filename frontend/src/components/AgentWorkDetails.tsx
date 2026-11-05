import { useI18n } from '../i18n'
import type { WorkState, WorkEventRef } from '../lib/workState'

export default function AgentWorkDetails({ state, onNavigate, onAction }: {
  state: WorkState; onNavigate?: (seq: number, eventId: string) => void; onAction?: (id: string) => void
}) {
  const { t } = useI18n()
  if (state.scope === 'none') return null
  const links: Array<[WorkEventRef | null | undefined, 'workState.request' | 'workState.cause' | 'workState.result']> = [
    [state.request, 'workState.request'], [state.event, 'workState.cause'], [state.result, 'workState.result'],
  ]
  return <div className="agent-work-details" aria-label={t(state.scope === 'previous' ? 'workState.previous' : 'workState.current')}>
    {state.scope === 'current' && <span className="metadata">{t('workState.current')}</span>}
    <div className="inline-actions agent-work-details__links">
      {onNavigate && links.map(([ref, key]) => ref && <button type="button" className="text-link" key={key}
        onClick={() => onNavigate(ref.seq, ref.event_id)}>{t(key)}</button>)}
      {state.phase === 'approval_waiting' && state.action_id && onAction && <button type="button" className="text-link"
        onClick={() => onAction(state.action_id!)}>{t('workState.approval')}</button>}
    </div>
    <details className="work-technical"><summary>{t('common.technicalDetails')}</summary>
      <pre className="technical">{JSON.stringify({ trigger_event_id: state.trigger_event_id,
        generation: state.generation, round: state.round, updated_at: state.updated_at,
        peer_agent_ids: state.peer_agent_ids }, null, 2)}</pre>
    </details>
  </div>
}
