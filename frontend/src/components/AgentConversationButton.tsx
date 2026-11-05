import { useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { MessageSquare } from 'lucide-react'
import { botsApi } from '../api/bots'
import { useBotScope } from '../lib/useBotScope'
import { useI18n } from '../i18n'

// A later explicit Bot choice wins over an earlier, slower resolve response.
let openingIntent = 0
type ClosedConversation = { roomId: number; canReplace: boolean }
export default function AgentConversationButton({ agentId, name, disabled = false, primary = false }: {
  agentId: number; name: string; disabled?: boolean; primary?: boolean
}) {
  const { t } = useI18n(), navigate = useNavigate(), scope = useBotScope()
  const lock = useRef(false), [busy, setBusy] = useState(false)
  const [error, setError] = useState<'failed' | 'unavailable' | null>(null)
  const [closed, setClosed] = useState<ClosedConversation | null>(null)
  const open = async (replacement?: number) => {
    if (disabled || lock.current || !scope.canUse()) return
    const intent = ++openingIntent
    lock.current = true; setBusy(true); setError(null)
    try {
      const result = await botsApi.conversation(agentId, replacement)
      if (!scope.canUse() || intent !== openingIntent) return
      navigate(`/chat/${result.room_id}`)
    } catch (failure) {
      if (!scope.canUse() || intent !== openingIntent) return
      const detail = (failure as { response?: { data?: { detail?: { code?: string; reason?: string; room_id?: number } } } }).response?.data?.detail
      if (detail?.code === 'bot_conversation_unavailable' && Number.isSafeInteger(detail.room_id) && (detail.room_id || 0) > 0) {
        setClosed({ roomId: detail.room_id!, canReplace: detail.reason === 'room_inactive' })
      } else { setClosed(null); setError(detail?.code === 'bot_unavailable' ? 'unavailable' : 'failed') }
    } finally { lock.current = false; if (scope.canUse()) setBusy(false) }
  }
  return <div className="bot-conversation-action">
    <button type="button" className={`button button--${primary ? 'primary' : 'secondary'} button--compact`}
      aria-label={t('bots.openNamed', { name })} disabled={disabled || busy} onClick={() => { void open() }}>
      <MessageSquare size={16} aria-hidden="true" />{t(busy ? 'bots.opening' : error === 'failed' ? 'bots.retryOpen' : 'bots.open')}
    </button>
    {error && <p className="metadata" role="alert">{t(error === 'unavailable' ? 'bots.unavailable' : 'bots.openFailed')}</p>}
    {closed && <div className="stack stack--tight bot-conversation-action__recovery" role="status">
      <p className="metadata">{t(closed.canReplace ? 'bots.closedConversation' : 'bots.closedProject')}</p>
      <div className="inline-actions"><Link className="text-link" to={`/chat/${closed.roomId}`}>{t('bots.viewHistory')}</Link>
        {closed.canReplace && <button type="button" className="text-link" disabled={busy} onClick={() => { void open(closed.roomId) }}>{t('bots.newConversation')}</button>}
      </div>
    </div>}
  </div>
}
