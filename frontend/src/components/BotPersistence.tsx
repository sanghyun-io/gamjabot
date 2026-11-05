import { useQuery } from '@tanstack/react-query'
import type { RoomEvent } from '../api/events'
import { botPersistenceApi } from '../api/botPersistence'
import { botPersistenceMetadata, matchingPersistence } from '../lib/botPersistence'
import { useBotScope } from '../lib/useBotScope'
import { useI18n } from '../i18n'

export default function BotPersistence({ event, personalAgentId, canUse, onOpen }: { event: RoomEvent; personalAgentId: number | null; canUse: () => boolean; onOpen: (seq: number, id: string) => void }) {
  const { t } = useI18n(), scope = useBotScope(), meta = botPersistenceMetadata(event)
  const read = useQuery({ queryKey: ['bot-persistence', scope.identity, event.room_id, meta?.agent_id, meta?.receipt_id], retry: false,
    enabled: !!meta && personalAgentId === meta.agent_id && scope.ownerId > 0,
    queryFn: async () => {
      if (!meta || !scope.canUse() || !canUse()) throw new Error('Persistence unavailable')
      const result = await botPersistenceApi.read(meta.agent_id, meta.receipt_id, event.room_id)
      if (!scope.canUse() || !matchingPersistence(event, result)) throw new Error('Persistence result changed')
      return result
    } })
  if (!meta) return null
  const receipt = read.isSuccess && personalAgentId === meta.agent_id && matchingPersistence(event, read.data) ? read.data : null
  return <section className="bot-creation stack stack--tight" aria-label={t('botPersistence.record')}>
    {receipt ? <><div style={{ overflowWrap: 'anywhere' }}><strong>{t(receipt.operation === 'add' ? 'botPersistence.saved' : receipt.operation === 'edit' ? 'botPersistence.updated' : receipt.operation === 'delete' ? 'botPersistence.forgotten' : 'botPersistence.role')}</strong>{' · '}{receipt.content.length > 120 ? `${receipt.content.slice(0,120)}…` : receipt.content}</div>
      <details className="compact-details"><summary>{t('botPersistence.details')}</summary><p>{receipt.operation !== 'role' && t('botPersistence.scope')}{receipt.history_retained && <> · {t('botPersistence.history')}</>}</p>{receipt.content.length > 120 && <p className="bot-message__content">{receipt.content}</p>}<button type="button" className="text-link" onClick={() => { if (scope.canUse() && canUse()) onOpen(receipt.root_ref.seq, receipt.root_ref.event_id) }}>{t('botPersistence.source')}</button></details></>
      : <><span role="status">{t(read.isFetching ? 'botPersistence.checking' : 'botPersistence.unknown')}</span>{!read.isFetching && personalAgentId === meta.agent_id && <button type="button" className="text-link" onClick={() => { if (scope.canUse() && canUse()) void read.refetch({ cancelRefetch: false }) }}>{t('botPersistence.readAgain')}</button>}</>}
  </section>
}
