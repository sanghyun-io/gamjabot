import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { toolsApi } from '../api/tools'
import type { Agent } from '../api/agents'
import type { BotMessageRef } from '../api/botMessages'
import { useBotScope } from '../lib/useBotScope'
import { executionContext } from '../lib/toolExecution'
import { useI18n } from '../i18n'
import { displayTarget, toolActionName } from './GrantPrompt'
// One projection for the entire original Owner request, not one per teammate card.
export default function RootToolActions({ messageId, rootRef, agents, pending, canUse }: {
  messageId: string; rootRef: BotMessageRef; agents: Agent[]; pending: boolean; canUse: () => boolean
}) {
  const { t } = useI18n(), scope = useBotScope()
  const query = useQuery({ queryKey: ['root-tool-actions', scope.ownerId, scope.epoch, rootRef.room_id, rootRef.event_id], retry: false,
    enabled: rootRef.actor_id === scope.ownerId, queryFn: () => toolsApi.rootActions(messageId, rootRef),
    refetchInterval: state => pending || state.state.data?.actions.some(action => ['pending', 'waiting', 'approved', 'started'].includes(action.state) || action.revocation_state === 'pending' || action.revocation_state === 'unknown') ? 2000 : false })
  const rows = query.data?.actions || [], waiting = rows.filter(action => action.state === 'waiting')
  const unknown = rows.some(action => action.revocation_state === 'unknown'), stopping = rows.some(action => action.revocation_state === 'pending')
  if (!waiting.length && !unknown && !stopping && !query.isError) return null
  const allowed = () => scope.canUse() && canUse()
  return <section className="root-tool-actions stack stack--tight" aria-label={t('toolScope.rootActions')}>
    {query.isError && <p className="metadata" role="status">{t('toolScope.readFailure')}{' '}<button type="button" className="text-link" disabled={query.isFetching} onClick={() => { if (allowed()) void query.refetch({ cancelRefetch: false }) }}>{t('common.retry')}</button></p>}
    {waiting.length > 0 && <><strong className="metadata">{t('toolScope.approvalNeeded')}</strong><ul className="root-tool-actions__list">{waiting.map(action => {
      const context = executionContext(action, scope.ownerId), name = agents.find(agent => agent.id === action.agent_id)?.name || t('members.fallbackName', { id: action.agent_id })
      return <li key={action.action_id}><span><strong>{name}</strong> · {toolActionName(action.family, t)} · {displayTarget(action.target, t)}</span>
        {context.kind === 'scoped' && context.status === 'active' && context.revocation !== 'pending' && context.revocation !== 'unknown'
          && <Link className="text-link" to={`/chat/${action.room_id}?${new URLSearchParams({ action: action.action_id })}`} onClick={event => { if (!allowed()) event.preventDefault() }}>{t('toolScope.reviewAction')}</Link>}</li>
    })}</ul></>}
    {unknown ? <p className="metadata notice--warning" role="alert">{t('toolScope.stopUnknown')}</p> : stopping && <p className="metadata" role="status">{t('toolScope.stopPending')}</p>}
  </section>
}
