import { Fragment } from 'react'
import type { ExecutionBinding } from '../api/tools'
import { executionBindingKey } from '../lib/toolExecution'
import { useI18n } from '../i18n'
import { reviewTimeOptions } from '../lib/reviewTime'
import { formatProfileOrigins } from '../api/vaultProfileHandoff'

const object = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
const id = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0
const label = (value: unknown): value is string => typeof value === 'string' && value.length > 0
  && value.length <= 200 && !/[\u0000-\u001f\u007f]/.test(value)
const httpsOrigin = (value: unknown): value is string => {
  if (typeof value !== 'string' || value.length > 2048 || /[\u0000-\u001f\u007f]/.test(value)) return false
  try { const u = new URL(value); return u.protocol === 'https:' && !u.username && !u.password && u.origin === value } catch { return false }
}
const uuid = (value: unknown): value is string => typeof value === 'string'
  && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value)
const stamp = (value: unknown): value is string => typeof value === 'string' && value.length <= 40
  && /^\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value))
const statuses = ['verified', 'absent', 'unavailable', 'unsupported', 'configured'] as const
type Status = typeof statuses[number]
const roles = ['path', 'source', 'destination', 'executable', 'cwd', 'owned_process', 'browser_context', 'desktop_channel', 'mcp_server', 'vault_profile', 'voice_memo']

// Project only the existing server snapshot's non-secret display fields.
// Never render the full object: runtime epochs, parent chains and unknown
// future fields are neither useful card details nor client-side authority.
export default function ActionReviewDetails({ review, reviewId, actionId, roomId, agentId, ownerId, executionBinding, vaultSummary }:
  { review: unknown; reviewId: string; actionId: string; roomId: number; agentId: number; ownerId: number | null; executionBinding?: ExecutionBinding | null;
    vaultSummary?: { target:string; scopeTarget:string; effect:string; digest:string; rootId:string|null } }) {
  const { t, formatDateTime } = useI18n()
  const value = object(review), action = object(value?.action), authority = object(value?.authority)
  const proposer = object(authority?.proposer), external = object(value?.external_context)
  const targets = Array.isArray(value?.targets) && value.targets.length <= 2 ? value.targets : null
  const valid = (value?.version === 1 && !executionBinding || value?.version === 2 && !!executionBinding && executionBindingKey(action?.execution_binding) === executionBindingKey(executionBinding)) && uuid(reviewId) && stamp(value.captured_at)
    && action?.action_id === actionId && action.room_id === roomId && action.agent_id === agentId
    && id(ownerId) && authority?.owner_id === ownerId && authority.room_id === roomId && authority.agent_id === agentId
    && proposer?.owner_id === ownerId && proposer.room_id === roomId && proposer.agent_id === agentId
    && id(proposer.connection_id) && label(proposer.provider_id) && label(proposer.model_id)
    && targets !== null && targets.every((target) => {
      const item = object(target)
      return item && typeof item.role === 'string' && roles.includes(item.role) && statuses.includes(item.status as Status)
        && (item.path === undefined || typeof item.path === 'string' && item.path.length <= 8192 && !/[\u0000-\u001f\u007f]/.test(item.path))
        && (item.role !== 'vault_profile' || item.status === 'verified'
          && label(item.profile_label) && typeof item.account_label === 'string' && item.account_label.length <= 200 && !/[\u0000-\u001f\u007f]/.test(item.account_label)
          && typeof item.username === 'string' && item.username.length <= 512 && !/[\u0000-\u001f\u007f]/.test(item.username)
          && httpsOrigin(item.origin)
          && typeof item.profile_id === 'string' && /^[0-9a-f]{32}$/.test(item.profile_id)
          && typeof item.item_id === 'string' && /^[0-9a-f]{32}$/.test(item.item_id)
          && Number.isSafeInteger(item.profile_revision) && Number(item.profile_revision) >= 0
          && Number.isSafeInteger(item.item_revision) && Number(item.item_revision) > 0
          && Number.isSafeInteger(item.secret_revision) && Number(item.secret_revision) > 0
          && !!formatProfileOrigins(item.profile_origins)?.includes(item.origin as string))
    }) && external && ['not_applicable', 'unsupported'].includes(String(external.account))
    && ['not_applicable', 'unsupported'].includes(String(external.target_version))
  if (!valid || !proposer || !authority || !targets || !value || !external) return <p className="section-copy">{t('review.unavailable')}</p>
  if (vaultSummary) {
    const item = targets.length === 1 ? object(targets[0]) : null
    if (!item || item.role !== 'vault_profile' || action?.family !== 'request_vault_login'
      || action.target !== vaultSummary.target || action.scope_target !== vaultSummary.scopeTarget
      || action.expected_effect !== vaultSummary.effect || action.payload_digest !== vaultSummary.digest
      || action.trigger_event_id !== vaultSummary.rootId) return <p className="section-copy">{t('review.unavailable')}</p>
    return <dl className="metadata"><dt>{t('vaultHandoff.site')}</dt><dd>{item.origin as string}</dd>
      <dt>{t('vaultHandoff.account')}</dt><dd>{item.username as string}{item.account_label && item.account_label!==item.username ? ` · ${item.account_label}` : ''}</dd>
      <dt>{t('vaultHandoff.profile')}</dt><dd>{item.profile_label as string}</dd></dl>
  }
  const revision = (input: unknown) => Number.isSafeInteger(input) && Number(input) >= 0 ? String(input) : t('review.unavailable')
  return <section className="stack stack--tight" aria-label={t('review.title')}>
    <h5>{t('review.title')}</h5>
    <dl className="technical approval__technical">
      <dt>{t('review.owner')}</dt><dd>{t('review.identity', { owner: Number(ownerId), room: roomId, agent: agentId })}</dd>
      <dt>{t('review.os')}</dt><dd>{authority.os_user_status === 'verified' && Number.isSafeInteger(authority.euid) && Number(authority.euid) >= 0 ? String(authority.euid) : t('review.unsupported')}</dd>
      <dt>{t('review.connection')}</dt><dd>{proposer.provider_id as string} · #{String(proposer.connection_id)}</dd>
      <dt>{t('review.model')}</dt><dd>{proposer.model_id as string}</dd>
      <dt>{t('review.revisions')}</dt><dd>{revision(proposer.config_revision)} / {revision(proposer.auth_grant_revision)}</dd>
      <dt>{t('review.id')}</dt><dd>{reviewId}</dd>
      <dt>{t('review.captured')}</dt><dd><time dateTime={value.captured_at as string}>{formatDateTime(value.captured_at as string, reviewTimeOptions)}</time></dd>
      <dt>{t('review.targets')}</dt><dd><ul>{targets.map((target, index) => {
        const item = object(target)!
        return <Fragment key={index}><li>{item.role === 'mcp_server' ? t('mcp.reviewTarget') : item.role === 'vault_profile' ? t('vaultHandoff.title') : item.role as string} · {t(`review.${item.status as Status}`)}{item.path ? <div>{item.path as string}</div> : null}{item.role === 'vault_profile' && <div>{t('vaultHandoff.site')}: {item.origin as string}<br/>{t('vaultHandoff.account')}: {item.username as string}{item.account_label && item.account_label!==item.username ? ` · ${item.account_label}` : ''}<br/>{t('vaultHandoff.profile')}: {item.profile_label as string}<br/>{t('vaultHandoff.allowedSites')}: {formatProfileOrigins(item.profile_origins)?.join(', ')}</div>}{item.role === 'browser_context' && item.profile === 'persistent_room' && <div>{t('browserProfiles.title')} · {label(item.profile_label) ? item.profile_label as string : ''} · r{revision(item.profile_revision)}{label(item.profile_id) && <div><code>{item.profile_id as string}</code></div>}</div>}</li></Fragment>
      })}</ul></dd>
      <dt>{t('review.external')}</dt><dd>{external.account === 'not_applicable' && external.target_version === 'not_applicable' ? t('review.notApplicable') : t('review.unsupported')}</dd>
    </dl>
    <p className="section-copy">{t('review.limit')}</p>
  </section>
}
