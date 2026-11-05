import { useI18n } from '../i18n'
import type { ValidToolExecution } from '../lib/toolExecution'
import HistoryEventLink from './HistoryEventLink'
export default function ToolExecutionProvenance({ context, roomName, canUse }: { context: ValidToolExecution; roomName?: string; canUse: () => boolean }) {
  const { t, locale } = useI18n()
  if (context.kind === 'slack') return <div className="notice notice--info stack stack--tight"><strong>{locale==='ko'?'Slack 요청에서 시작된 작업':'Action requested from Slack'}</strong><p>{locale==='ko'?'채널':'Channel'}: {context.binding.channel_id} · {locale==='ko'?'보낸 사람':'Sender'}: {context.binding.sender_id}</p><p>{locale==='ko'?'Slack 발신자는 감자봇 Owner가 아닙니다. 이 작업은 여기서 별도로 승인해야 합니다.':'The Slack sender is not the Gamja owner. This action needs separate approval here.'}</p><HistoryEventLink roomId={context.root.room_id} seq={context.root.seq} eventId={context.root.event_id} canUse={canUse}>{locale==='ko'?'원래 요청 보기':'View original request'}</HistoryEventLink></div>
  if (context.kind === 'routine') return <div className="stack stack--tight"><HistoryEventLink roomId={context.root.room_id} seq={context.root.seq} eventId={context.root.event_id} canUse={canUse}>{t(context.binding.origin === 'test' ? 'routine.sourceTest' : context.binding.origin === 'webhook' ? 'webhook.runOrigin' : 'routine.sourceScheduled')}</HistoryEventLink>{context.status === 'active' && <span className="metadata">{t('routine.approvalActive')}</span>}{context.status === 'revoked' && <p className="metadata" role="status">{t('toolScope.revoked')}</p>}</div>
  if (context.kind !== 'scoped') return null
  return <div className="stack stack--tight">
    <div className="metadata" style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 }}><HistoryEventLink roomId={context.root.room_id} seq={context.root.seq} eventId={context.root.event_id} canUse={canUse}>{t('toolScope.original')}</HistoryEventLink>
      <span>{t('toolScope.executionRoom', { name: roomName || t('chat.roomTitle', { id: String(context.binding.execution_room_id) }) })}</span></div>
    {context.status === 'revoked' && <p className="metadata" role="status">{t('toolScope.revoked')}</p>}
    {context.status === 'expired' && <p className="metadata" role="status">{t('toolScope.expired')}</p>}
    {context.revocation === 'pending' && <p className="metadata" role="status">{t('toolScope.stopPending')}</p>}
    {context.revocation === 'unknown' && <p className="metadata notice--warning" role="alert">{t('toolScope.stopUnknown')}</p>}
  </div>
}
