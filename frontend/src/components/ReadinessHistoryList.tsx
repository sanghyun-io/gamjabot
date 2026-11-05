import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ReadinessOutcome, ReadinessTestRecord } from '../api/readiness'
import { useI18n } from '../i18n'
import { reviewTimestamp, reviewTimeOptions } from '../lib/reviewTime'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import ServerTime from './ServerTime'

export default function ReadinessHistoryList({ items, basisMatches, canUse, outcomeLabel }: {
  items: readonly ReadinessTestRecord[]; basisMatches: boolean; canUse: () => boolean; outcomeLabel: (code: ReadinessOutcome) => string
}) {
  const { t, formatDateTime, formatNumber } = useI18n(), id = useId()
  const [query, setQuery] = useState(''), [filter, setFilter] = useState<'all' | 'success' | 'failure'>('all'), [anchor, setAnchor] = useState<number | null>(null)
  const readingRoot = useRef<HTMLDivElement>(null), pageIntent = useRef<{ context: object; id: number; origin: HTMLButtonElement } | null>(null)
  const pageContext = useMemo(() => ({}), [items, basisMatches, query, filter, t, outcomeLabel])
  const basisLabel = (record: ReadinessTestRecord) => t(!basisMatches ? 'readiness.browse.differentBasis' : record.config_current ? 'readiness.currentConfig' : 'readiness.previousConfig')
  const match = directoryMatcher(query)
  const found = items.filter(record => {
    if (filter === 'success' && !record.ok || filter === 'failure' && record.ok) return false
    if (!query.trim()) return true
    const time = reviewTimestamp(record.tested_at)
    return match([String(record.id), `#${record.id}`, record.model_id, record.code, outcomeLabel(record.code), basisLabel(record),
      time, time ? formatDateTime(time, reviewTimeOptions) : '', String(record.duration_ms)])
  })
  const start = anchor === null ? 0 : Math.max(0, found.findIndex(record => record.id === anchor)), end = Math.min(start + 5, found.length)
  const range = t('commandArchive.range', { from: found.length ? start + 1 : 0, to: end, total: found.length })
  const move = (index: number, origin: HTMLButtonElement) => {
    if (!canUse() || !found.length || !Number.isSafeInteger(index) || !origin.isConnected || origin.disabled) return
    const target = found[Math.max(0, Math.min(index, found.length - 1))]
    if (!target || target.id === found[start]?.id) return
    origin.focus({ preventScroll: true }); pageIntent.current = { context: pageContext, id: target.id, origin }; setAnchor(target.id)
  }
  useLayoutEffect(() => {
    const intent = pageIntent.current; pageIntent.current = null
    if (!intent || intent.context !== pageContext || intent.id !== found[start]?.id || !canUse()
      || document.visibilityState !== 'visible' || !document.hasFocus() || document.activeElement !== intent.origin && document.activeElement !== document.body) return
    const target = readingRoot.current
    if (!target?.isConnected || !target.getClientRects().length || target.closest('[hidden], [inert], [aria-hidden="true"]')) return
    const top = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).filter(node => node.getClientRects().length > 0
      && !node.closest('[hidden], [inert], [aria-hidden="true"]') && getComputedStyle(node).visibility !== 'hidden').at(-1)
    if (top && !top.contains(target)) return
    target.focus({ preventScroll: true }); target.scrollIntoView({ block: 'start', inline: 'nearest' })
  }, [anchor, pageContext, found[start]?.id])
  return <div className="stack stack--tight">
    <DirectorySearch value={query} onChange={value => { if (canUse()) { pageIntent.current = null; setQuery(value); setAnchor(null) } }}
      label={t('readiness.browse.historyFind')} hint={t('readiness.browse.historyFind')} shown={found.length} total={items.length}
      known loading={false} controls={id} maxLength={200} preventSubmit/>
    <p className="field-hint">{t('readiness.browse.historyFindScope')}</p>
    <label className="field">{t('commandArchive.state')}<select value={filter} aria-controls={id} onChange={event => {
      if (canUse()) { pageIntent.current = null; setFilter(event.target.value as typeof filter); setAnchor(null) }
    }}><option value="all">{t('commandArchive.all')}</option><option value="success">{t('commandArchive.succeeded')}</option><option value="failure">{t('commandArchive.failed')}</option></select></label>
    <p role="status" className="metadata">{found.length ? range : t('directory.noMatches')}</p>
    <div className="page-actions">
      <button type="button" className="text-link" disabled={!found.length || start === 0} onClick={event => move(0, event.currentTarget)}>{t('commandArchive.newest')}</button>
      <button type="button" className="text-link" disabled={start === 0} onClick={event => move(start - 5, event.currentTarget)}>{t('commandArchive.newer')}</button>
      <button type="button" className="text-link" disabled={end >= found.length} onClick={event => move(start + 5, event.currentTarget)}>{t('commandArchive.older')}</button>
      <button type="button" className="text-link" disabled={end >= found.length} onClick={event => move(Math.max(0, found.length - 5), event.currentTarget)}>{t('commandArchive.oldest')}</button>
    </div>
    <p className="field-hint">{t('commandArchive.pageReadHelp')}</p>
    <div ref={readingRoot} id={id} role="region" tabIndex={found.length ? -1 : undefined} aria-label={range} className="save-history-reading-page">
      <ol start={start + 1}>{found.slice(start, end).map(record => <li key={record.id}><span>{outcomeLabel(record.code)}</span>
        <span>#{record.id} · {record.model_id || t('readiness.noModel')} · <ServerTime value={record.tested_at}/> · {formatNumber(record.duration_ms)} ms · {basisLabel(record)}</span>
      </li>)}</ol>
    </div>
  </div>
}
