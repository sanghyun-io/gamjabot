import type { RoomEvent } from '../api/events'
import { useI18n } from '../i18n'
import { peerReplies, replyExcerpt } from '../lib/peerReply'
import { AgentCharacter } from './AgentCharacter'

export default function PeerReply({ answer, events, loading, onOpen }: {
  answer: RoomEvent; events: RoomEvent[]; loading: boolean
  onOpen: (seq: number, eventId: string) => void
}) {
  const { t } = useI18n()
  const metadata = peerReplies(answer, events)
  if (!metadata?.references.length) return null
  const card = ({ id, source }: typeof metadata.references[number]) => !source
    ? <p key={id} className="peer-reply__missing" role="status">{t(loading ? 'chat.reply.loading' : 'chat.reply.unavailable')}</p>
    : <button key={id} type="button" className="peer-reply__card"
      aria-label={t('chat.reply.open', { name: source.actor_name_snapshot, seq: source.seq })}
      onClick={() => onOpen(source.seq, source.event_id)}>
      <AgentCharacter agentId={source.actor_id} />
      <span className="peer-reply__preview"><strong>{source.actor_name_snapshot}</strong>
        <span>{replyExcerpt(String(source.payload.content)) || t('chat.reply.empty')}</span></span>
    </button>
  return <div className="peer-reply" aria-label={t('chat.reply.label')}>
    <span className="peer-reply__label">{t('chat.reply.label')}</span>
    {card(metadata.references[0])}
    {metadata.references.length > 1 && <details className="peer-reply__more">
      <summary>{t('chat.reply.more', { count: metadata.references.length - 1 })}</summary>
      {metadata.references.slice(1).map(card)}
    </details>}
    <details className="peer-reply__details"><summary>{t('chat.reply.details')}</summary>
      <p>{t('chat.reply.declared')}</p><p>{t('chat.reply.provided', { count: metadata.providedCount })}</p>
    </details>
  </div>
}
