import type { RoomEvent } from '../api/events'
import type { BotBatch } from '../api/botBatches'
import type { BotMessageRef } from '../api/botMessages'
import { botBatchMetadata, matchingBotBatch, isBatchMemberMessage, botBatchMemberStatus } from '../lib/botBatch'
import { useI18n } from '../i18n'
import HistoryEventLink from './HistoryEventLink'
import { StatusText } from './ui'
// A branch shows only its exact member. The original Owner message owns the collective summary.
export default function BotBatchEvent({ event, batches, current = false, fetching = false, failed = false, onRead, canUse, onOpen }: {
  event: RoomEvent; batches: BotBatch[]; current?: boolean; fetching?: boolean; failed?: boolean; onRead?: () => void; canUse: () => boolean; onOpen: (ref: BotMessageRef) => void
}) {
  const { t } = useI18n(), meta = botBatchMetadata(event)
  if (!meta || meta.phase.startsWith('branch_') && event.payload.bot_message != null && !isBatchMemberMessage(event)) return null
  const batch = matchingBotBatch(event, batches), member = batch && meta.member_index !== undefined ? batch.members[meta.member_index] : null
  const incoming = meta.phase === 'branch_delivery', response = meta.phase === 'branch_response'
  const result = member?.resolved_result_ref || member?.result_ref
  const showResult = !!result && (result.room_id !== event.room_id || result.event_id !== event.event_id || result.seq !== event.seq)
  const resultLabel = result?.kind === 'failure' ? 'botBatches.openFailure' : member?.state === 'completed' ? 'botBatches.openResult' : member?.state === 'running' ? 'botBatches.openEarly' : 'botBatches.openRecord'
  const title = incoming ? 'botMessages.received' : response ? 'teammates.response' : meta.phase === 'return_delivery' ? 'botMessages.returnDelivery' : meta.phase === 'return' ? 'teammates.return' : 'botBatches.event'
  return <section className="bot-batch-event stack stack--tight" aria-label={t(title)}>
    {incoming && member && <p className="bot-message__content">{member.content}</p>}
    {meta.phase.startsWith('branch_') && <StatusText tone={!current || !member ? 'warning' : member.state === 'failed' ? 'error' : member.state === 'running' ? 'busy' : 'neutral'}>{t(current && member ? botBatchMemberStatus(member) : 'teammates.statusUnknown')}</StatusText>}
    <div className="bot-batch-event__links metadata"><span>{t(title)}{batch && <> · {response && member ? member.recipient_name : batch.sender_name}{member && <> → {response ? batch.sender_name : member.recipient_name}</>}</>}</span>
      {batch ? <HistoryEventLink roomId={batch.root_ref.room_id} seq={batch.root_ref.seq} eventId={batch.root_ref.event_id} canUse={canUse}
        onNavigate={(seq, eventId) => onOpen({ ...batch.root_ref, seq, event_id: eventId })}>{t('botBatches.openGroup')}</HistoryEventLink>
        : !meta.phase.startsWith('branch_') && <span>{t('teammates.statusUnknown')}</span>}
      {showResult && result && member && <HistoryEventLink roomId={result.room_id} seq={result.seq} eventId={result.event_id} canUse={canUse}
        onNavigate={(seq, eventId) => onOpen({ ...result, seq, event_id: eventId })}>{t(resultLabel, { name: member.recipient_name })}</HistoryEventLink>}
    </div>
    {meta.phase.startsWith('branch_') && (!current || !member) && <p className="metadata" role="status">{t('botMessages.unconfirmed')}{failed && onRead && <>{' '}<button type="button" className="text-link" disabled={fetching} onClick={() => { if (canUse()) onRead() }}>{t('common.retry')}</button></>}</p>}
  </section>
}
