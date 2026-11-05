import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import type { RoomEvent } from '../api/events'
import { botCreationsApi, type BotCreationReceipt } from '../api/botCreations'
import { botCreationMetadata, matchingBotCreation } from '../lib/botCreation'
import { useBotScope } from '../lib/useBotScope'
import { useI18n } from '../i18n'
import { StatusText } from './ui'
export default function BotCreation({ event, receipts, receivedAt, current, fetching, canUse }: {
  event: RoomEvent; receipts: BotCreationReceipt[]; receivedAt: number; current: boolean; fetching: boolean; canUse: () => boolean
}) {
  const { t } = useI18n(), scope = useBotScope(), meta = botCreationMetadata(event)
  const lookup = useQuery({ queryKey: ['bot-creation', scope.identity, event.room_id, meta?.creation_id, meta?.root_request_event_id, meta?.source_agent_id],
    enabled: false, retry: false, queryFn: () => {
      if (!meta || !scope.canUse()) throw new Error('Bot creation unavailable')
      return botCreationsApi.read(meta.creation_id, event.room_id, meta.root_request_event_id)
    } })
  if (!meta) return null
  const lookupCurrent = lookup.isSuccess && !lookup.isError && lookup.dataUpdatedAt > receivedAt
  const receipt = matchingBotCreation(event, lookupCurrent && lookup.data ? [lookup.data, ...receipts] : receipts)
  const busy = lookup.isFetching || !receipt && fetching
  const available = !!receipt?.readback.available && (current || lookupCurrent)
  return <section className="bot-creation stack stack--tight" aria-label={t('botCreation.record')}>
    {receipt ? <>
      <div className="bot-creation__heading"><strong>{receipt.created_agent.name}</strong><StatusText tone="neutral">{t('botCreation.created')}</StatusText></div>
      <p>{receipt.created_agent.description}</p>
      <p className="metadata">{t('botCreation.model', { model: receipt.created_agent.model_id })}</p>
      <details className="compact-details"><summary>{t('botCreation.role')}</summary><p className="bot-message__content">{receipt.created_agent.system_prompt}</p></details>
      {!available && <p className="metadata" role="status">{t('botCreation.changed')}</p>}
      <div className="bot-creation__actions">{available && <Link className="button button--secondary button--compact" to={receipt.conversation.href}
        aria-label={t('bots.openNamed', { name: receipt.created_agent.name })} onClick={event => { if (!canUse() || !scope.canUse()) event.preventDefault() }}>{t('bots.open')}</Link>}
        {!available && <button type="button" className="text-link" disabled={busy} onClick={() => { if (canUse() && scope.canUse()) void lookup.refetch({ cancelRefetch: false }) }}>{t('botCreation.readAgain')}</button>}</div>
    </> : <>
      <StatusText tone={busy ? 'busy' : 'warning'}>{t(busy ? 'botCreation.checking' : 'botCreation.unknown')}</StatusText>
      {!busy && <><p className="metadata">{t('botCreation.readOnlyRecovery')}</p><button type="button" className="text-link" onClick={() => { if (canUse() && scope.canUse()) void lookup.refetch({ cancelRefetch: false }) }}>{t('botCreation.readAgain')}</button></>}
    </>}
  </section>
}
