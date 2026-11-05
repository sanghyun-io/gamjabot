import type { ToolActivity } from '../api/tools'
import { useI18n, type Translate } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import { displayTarget, plainEffect, toolActionName } from './GrantPrompt'
import ServerTime from './ServerTime'
import ToolExecutionProvenance from './ToolExecutionProvenance'
import { executionContext } from '../lib/toolExecution'
import { useBotScope } from '../lib/useBotScope'
import McpSavedDraftViewer from './McpSavedDraftViewer'

const labels: Record<string, MessageKey> = {
  pending: 'activity.record.pending', waiting: 'activity.record.waiting', started: 'activity.record.started',
  completed: 'activity.record.completed', failed: 'activity.record.failed', denied: 'activity.record.denied',
  cancelled: 'activity.record.cancelled', uncertain: 'activity.record.uncertain',
}
export function toolActivityState(state: string, t: Translate) { return labels[state] ? t(labels[state]) : state }
export function toolActivitySearchFields(action: ToolActivity, agentName: string, t: Translate) {
  return [agentName, String(action.agent_id), toolActionName(action.family, t), action.family,
    displayTarget(action.target, t), plainEffect(action.expected_effect, t), toolActivityState(action.state, t), action.state, action.action_id]
}
// Read received activity only. Never reconstruct a review or invoke a tool here.
export default function ToolActivityRecord({ action, agentName, onInspect }: {
  action: ToolActivity; agentName: string; onInspect?: () => void
}) {
  const { t } = useI18n(), scope = useBotScope()
  const id = (value: unknown) => Number.isSafeInteger(value) && Number(value) > 0 ? `#${value}` : t('review.unavailable')
  return <div className="stack stack--tight" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <p><strong>{agentName}{Number.isSafeInteger(action.agent_id) && action.agent_id > 0 && <> · #{action.agent_id}</>}</strong> · {toolActionName(action.family, t)} · {toolActivityState(action.state, t)}</p>
    <p className="metadata">{t('activity.record.requested')} · <ServerTime value={action.requested_at}/></p>
    <p>{displayTarget(action.target, t)}</p><p>{plainEffect(action.expected_effect, t)}</p>
    {(action.family === 'screenshot' || action.family === 'browser_screenshot') && <><p>{t('approval.image.historyLabel')} · {t(action.send_image_to_model === true ? 'approval.image.historyAllowed' : action.send_image_to_model === false ? 'approval.image.historyNotSelected' : 'approval.image.historyUnknown')}</p><p className="metadata">{t('approval.image.historyHelp')}</p></>}
    {onInspect && <button type="button" className="text-link" aria-label={`${t('workState.inspectAction')} · ${agentName} · ${toolActionName(action.family, t)}`} onClick={onInspect}>{t('workState.inspectAction')}</button>}
    {action.family === 'mcp_tool_call' && action.owner_draft && Number.isSafeInteger(action.owner_draft.revision) && action.owner_draft.revision > 0 && typeof action.owner_draft.pending === 'boolean' && typeof action.owner_draft.applied === 'boolean' && <McpSavedDraftViewer key={JSON.stringify([scope.identity, action.room_id, action.action_id, action.state, action.owner_draft.revision, action.owner_draft.pending])} roomId={action.room_id} actionId={action.action_id} revision={action.owner_draft.revision} pending={action.owner_draft.pending} terminal={!['pending','waiting','approved','started'].includes(action.state)} />}
    <ToolExecutionProvenance context={executionContext(action, scope.ownerId)} canUse={scope.canUse} />
    <details className="compact-details"><summary>{t('review.audit')}</summary><dl className="technical approval__technical">
      <dt>{t('review.action')}</dt><dd>{action.action_id}</dd>
      <dt>{t('review.id')}</dt><dd>{action.review_id || t('review.unavailable')}</dd>
      <dt>{t('review.grant')}</dt><dd>{id(action.grant_id)}</dd>
      <dt>{t('review.approver')}</dt><dd>{id(action.approval_owner_id)}</dd>
      <dt>{t('review.approved')}</dt><dd><ServerTime value={action.approved_at}/></dd>
      <dt>{t('review.started')}</dt><dd><ServerTime value={action.started_at}/></dd>
      <dt>{t('review.finished')}</dt><dd><ServerTime value={action.completed_at}/></dd>
      <dt>{t('chat.detail.state')}</dt><dd>{action.state}</dd>
      <dt>{t('approval.reuseScope')}</dt><dd>{action.scope || t('review.unavailable')}</dd>
      <dt>{t('approval.actionKind')}</dt><dd>{action.family}</dd>
      <dt>{t('approval.rawTarget')}</dt><dd>{action.target}</dd>
      <dt>{t('approval.rawEffect')}</dt><dd>{action.expected_effect}</dd>
    </dl></details>
  </div>
}
