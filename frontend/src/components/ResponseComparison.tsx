import { useId, useMemo, useRef, useState } from 'react'
import { Columns2 } from 'lucide-react'
import type { RoomEvent } from '../api/events'
import { useI18n } from '../i18n'
import { buildResponseComparisons } from '../lib/responseComparison'
import { closeOtherRoomPanels } from '../lib/roomPanels'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'

function preview(value: string) {
  const compact = value.replace(/\s+/g, ' ').trim()
  return compact.length > 64 ? `${compact.slice(0, 63)}…` : compact
}

export default function ResponseComparison({ events, historyState, onNavigateSeq }: {
  events: RoomEvent[]
  historyState: 'loading' | 'complete' | 'error'
  onNavigateSeq: (seq: number) => void
}) {
  const { t } = useI18n()
  const [expanded, setExpanded] = useState(false)
  const [requestQuery, setRequestQuery] = useState('')
  const requestSelectId = useId()
  // Composer changes need not regroup every received event. Closed panels
  // retain choice/query state; the parent keeps the canonical conversation.
  const comparisons = useMemo(() => expanded && historyState === 'complete' ? buildResponseComparisons(events) : [], [expanded, historyState, events])
  const [selectedRequestId, setSelectedRequestId] = useState<string | null>(null)
  const [selectedRoundNumber, setSelectedRoundNumber] = useState<number | null>(null)
  const panel = useRef<HTMLDetailsElement>(null)
  const request = comparisons.find((item) => item.triggerEventId === selectedRequestId) || comparisons[0]
  const round = request?.rounds.find((item) => item.round === selectedRoundNumber) || request?.rounds.at(-1)

  const requestSearch = useMemo(() => {
    const match = directoryMatcher(requestQuery)
    const items = requestQuery.trim() ? comparisons.filter(item => match([
      String(item.requestSeq), item.requestContent,
      ...item.rounds.flatMap(round => round.responses.map(response => response.actorName)),
    ])) : comparisons
    return { items, ids: new Set(items.map(item => item.triggerEventId)) }
  }, [comparisons, requestQuery])
  const requestPinned = !!request && !requestSearch.ids.has(request.triggerEventId)
  const requestChoices = request && requestPinned ? [request, ...requestSearch.items] : requestSearch.items

  const goToEvent = (seq: number) => {
    if (panel.current) panel.current.open = false
    setExpanded(false)
    onNavigateSeq(seq)
  }

  return <details ref={panel} name="room-panel" className="response-comparison compact-details"
    onToggle={event => {
      if (event.target !== event.currentTarget) return
      closeOtherRoomPanels(event.currentTarget)
      setExpanded(event.currentTarget.open)
    }}>
    <summary><Columns2 size={16} aria-hidden="true" />{t('chat.comparison.open')}</summary>
    {expanded && <div className="response-comparison__body">
      {historyState === 'loading' && <p role="status" className="metadata">{t('chat.comparison.loading')}</p>}
      {historyState === 'error' && <p role="alert" className="notice notice--warning">{t('chat.comparison.historyUnavailable')}</p>}
      {historyState === 'complete' && !request && <p className="section-copy">{t('chat.comparison.empty')}</p>}
      {historyState === 'complete' && request && round && <>
        <DirectorySearch value={requestQuery} onChange={setRequestQuery} label={t('chat.comparison.findRequests')}
          hint={t('chat.comparison.findHint')} shown={requestSearch.items.length} total={comparisons.length}
          loading={false} known controls={requestSelectId} preventSubmit/>
        <p id={`${requestSelectId}-help`} className="field-hint">{t('chat.comparison.requestSearchHelp')}</p>
        {requestPinned && <p id={`${requestSelectId}-pinned`} className="field-hint">{t('chat.comparison.pinnedRequestNotice')}</p>}
        {requestQuery.trim() && requestSearch.items.length === 0 && <p role="status">{t('chat.comparison.noRequestMatches')}</p>}
        <div className="response-comparison__controls">
          <label className="field">{t('chat.comparison.request')}<select id={requestSelectId} value={request.triggerEventId} aria-describedby={`${requestSelectId}-help${requestPinned ? ` ${requestSelectId}-pinned` : ''}`}
            onChange={(event) => { setSelectedRequestId(event.target.value); setSelectedRoundNumber(null) }}>
            {requestChoices.map((item) => <option key={item.triggerEventId} value={item.triggerEventId}>
              {requestPinned && item.triggerEventId === request.triggerEventId
                ? t('chat.comparison.pinnedRequestOption', { request: t('chat.comparison.requestOption', { seq: item.requestSeq, title: preview(item.requestContent) }) })
                : t('chat.comparison.requestOption', { seq: item.requestSeq, title: preview(item.requestContent) })}
            </option>)}</select></label>
          <label className="field">{t('chat.comparison.round')}<select value={round.round}
            onChange={(event) => setSelectedRoundNumber(Number(event.target.value))}>
            {request.rounds.map((item) => <option key={item.round} value={item.round}>{t('chat.comparison.roundOption', { round: item.round })}</option>)}
          </select></label>
        </div>
        <section className="response-comparison__request" aria-label={t('chat.comparison.request')}>
          <div className="response-comparison__heading"><strong>{t('chat.comparison.request')}</strong>
            <button type="button" className="text-link" aria-label={t('chat.comparison.goRequest', { seq: request.requestSeq })}
              onClick={() => goToEvent(request.requestSeq)}>#{request.requestSeq}</button></div>
          <p>{request.requestContent}</p>
        </section>
        <p className="metadata response-comparison__count">{t('chat.comparison.responseCount', { count: round.responses.length, round: round.round })}</p>
        <div className="response-comparison__grid">
          {round.responses.map((response) => <article className="response-card" key={response.eventId}>
            <header><strong>{response.actorName}</strong><button type="button" className="text-link"
              aria-label={t('chat.comparison.goResponse', { seq: response.seq })} onClick={() => goToEvent(response.seq)}>#{response.seq}</button></header>
            <p>{response.content}</p>
            <details className="compact-details response-card__model"><summary>{t('chat.comparison.modelDetails')}</summary>
              <dl><dt>{t('chat.comparison.provider')}</dt><dd>{response.modelUse.providerKind}</dd>
                <dt>{t('chat.comparison.model')}</dt><dd>{response.modelUse.modelId}</dd>
                <dt>{t('chat.comparison.connection')}</dt><dd>#{response.modelUse.connectionId}</dd>
                <dt>{t('chat.comparison.contextThrough')}</dt><dd>#{response.modelUse.watermarkSeq}</dd></dl>
            </details>
          </article>)}
        </div>
      </>}
    </div>}
  </details>
}
