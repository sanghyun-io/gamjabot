import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { FeatureCheckRecord, FeatureCheckTarget } from '../../lib/localFeatureChecks'
import type { MessageKey } from '../../i18n/locales/en'
import { useI18n } from '../../i18n'
import DirectorySearch, { directoryMatcher } from '../DirectorySearch'
import { reviewTimestamp, reviewTimeOptions } from '../../lib/reviewTime'
import { FeatureReportDetails } from './FeatureTable'

type HistoryRecord = Pick<FeatureCheckRecord, 'id' | 'status' | 'notice' | 'report'> & { target?: FeatureCheckTarget }
type TargetContext = Pick<FeatureCheckTarget, 'modelId' | 'baseUrl'> & { connectionId?: number }
type Filter = 'all' | 'pending' | 'unknown' | 'done' | 'not_sent'
const PAGE_SIZE = 5
const matchesFilter = (record: HistoryRecord, filter: Filter) => filter === 'all' || (filter === 'pending' ? record.status === 'preparing' || record.status === 'checking' : record.status === filter)
export default function FeatureCheckHistory({ checks, context }: { checks: readonly HistoryRecord[]; context?: TargetContext }) {
 const { t, formatDateTime } = useI18n(), listId = useId()
 const [expanded, setExpanded] = useState(false), [filter, setFilter] = useState<Filter>('all'), [anchor, setAnchor] = useState<string | null>(null)
 const [query, setQuery] = useState('')
 const readingRoot = useRef<HTMLDivElement>(null), pageIntent = useRef<{ context: object; id: string; origin: HTMLButtonElement } | null>(null)
 const pageContext = useMemo(() => ({}), [checks, context, query, filter, t])
 const statusKey = (record: HistoryRecord): MessageKey => record.notice || (record.status === 'done' ? 'localModels.session.reportOriginal' : record.status === 'preparing' || record.status === 'checking' ? 'localModels.checking' : record.status === 'unknown' ? 'localModels.session.checkUnknown' : 'localModels.featureSession.deferred')
 const allRows = useMemo(() => checks.map((record, index) => ({ record, ordinal: index + 1 })).reverse(), [checks])
 const rows = useMemo(() => {
  const match = directoryMatcher(query)
  return allRows.filter(({ record, ordinal }) => {
   if (!matchesFilter(record, filter)) return false
   if (!expanded || !query.trim()) return true
   const target = record.target || context
   const dates = [record.report?.observed_at, record.report?.tested_at].flatMap(value => {
    const time = reviewTimestamp(value)
    return time ? [time, formatDateTime(time, reviewTimeOptions)] : []
   })
   return match([String(ordinal), `#${ordinal}`, target?.connectionId === undefined ? '' : String(target.connectionId),
    target?.modelId, target?.baseUrl, t(statusKey(record)), ...dates])
  })
 }, [allRows, expanded, filter, query, context, t, formatDateTime])
 const found = anchor ? rows.findIndex(row => row.record.id === anchor) : 0
 const start = found >= 0 ? found : 0, end = Math.min(start + PAGE_SIZE, rows.length)
 const shown = new Set(rows.slice(start, end).map(row => row.record.id))
 const changeFilter = (value: Filter) => { pageIntent.current = null; setFilter(value); setAnchor(null) }
 const move = (index: number, origin: HTMLButtonElement) => {
  if (!expanded || !rows.length || !Number.isSafeInteger(index) || !origin.isConnected || origin.disabled) return
  const target = rows[Math.max(0, Math.min(index, rows.length - 1))]
  if (!target || target.record.id === rows[start]?.record.id) return
  origin.focus({ preventScroll: true }); pageIntent.current = { context: pageContext, id: target.record.id, origin }; setAnchor(target.record.id)
 }
 useLayoutEffect(() => {
  const intent = pageIntent.current; pageIntent.current = null
  if (!intent || intent.context !== pageContext || intent.id !== rows[start]?.record.id || !expanded
    || document.visibilityState !== 'visible' || !document.hasFocus() || document.activeElement !== intent.origin && document.activeElement !== document.body) return
  const target = readingRoot.current
  if (!target?.isConnected || !target.getClientRects().length || target.closest('[hidden], [inert], [aria-hidden="true"]')) return
  const details = target.closest('details'); if (!details?.open) return
  const top = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).filter(node => node.getClientRects().length > 0
    && !node.closest('[hidden], [inert], [aria-hidden="true"]') && getComputedStyle(node).visibility !== 'hidden').at(-1)
  if (top && !top.contains(target)) return
  target.focus({ preventScroll: true }); target.scrollIntoView({ block: 'start', inline: 'nearest' })
 }, [anchor, pageContext, expanded, rows[start]?.record.id])
 return <details className="compact-details" onToggle={event => {
  if (event.target !== event.currentTarget) return
  if (!event.currentTarget.open) pageIntent.current = null
  setExpanded(event.currentTarget.open)
  if (event.currentTarget.open && anchor === null) setAnchor(rows[0]?.record.id || null)
 }}>
  <summary>{t('localModels.history.summary', { count: checks.length })}</summary>
  {expanded && <div className="stack stack--tight">
   <p className="field-hint">{t('localModels.history.help')}</p>
   <DirectorySearch value={query} onChange={value => { pageIntent.current = null; setQuery(value); setAnchor(null) }}
    label={t('localModels.history.search')} hint={t('localModels.history.search')} shown={rows.length} total={checks.length}
    known loading={false} controls={listId} maxLength={200} preventSubmit/>
   <p className="field-hint">{t('localModels.history.searchScope')}</p>
   <label className="field">{t('localModels.history.filter')}<select value={filter} onChange={event => changeFilter(event.target.value as Filter)} aria-controls={listId}>
    <option value="all">{t('localModels.history.all')}</option><option value="done">{t('localModels.history.done')}</option>
    <option value="unknown">{t('localModels.history.unknown')}</option><option value="pending">{t('localModels.history.pending')}</option><option value="not_sent">{t('localModels.history.notSent')}</option>
   </select></label>
   <div className="page-actions">
    <button type="button" className="button button--secondary button--compact" disabled={start === 0} aria-controls={listId} onClick={event => move(0, event.currentTarget)}>{t('localModels.history.latest')}</button>
    <button type="button" className="button button--secondary button--compact" disabled={start === 0} aria-controls={listId} onClick={event => move(start - PAGE_SIZE, event.currentTarget)}>{t('localModels.history.newer')}</button>
    <button type="button" className="button button--secondary button--compact" disabled={end >= rows.length} aria-controls={listId} onClick={event => move(start + PAGE_SIZE, event.currentTarget)}>{t('localModels.history.older')}</button>
    <button type="button" className="button button--secondary button--compact" disabled={end >= rows.length} aria-controls={listId} onClick={event => move(Math.floor((rows.length - 1) / PAGE_SIZE) * PAGE_SIZE, event.currentTarget)}>{t('localModels.history.oldest')}</button>
   </div>
   <p role="status" className="metadata">{t('localModels.history.range', { from: rows.length ? start + 1 : 0, to: end, total: rows.length })}</p>
   {!rows.length && <p>{t('localModels.history.empty')}</p>}
   <p className="field-hint">{t('commandArchive.pageReadHelp')}</p>
   <div ref={readingRoot} id={listId} role="region" tabIndex={rows.length ? -1 : undefined} aria-label={t('localModels.history.range', { from: rows.length ? start + 1 : 0, to: end, total: rows.length })} className="stack save-history-reading-page">{allRows.map(({ record, ordinal }) => {
    const target = record.target || context
    const status = statusKey(record)
    return <section key={record.id} hidden={!shown.has(record.id)} style={{ display: shown.has(record.id) ? undefined : 'none' }} className="stack stack--tight">
     <strong>#{ordinal} · {t(status)}</strong>
     {target && <p className="metadata" style={{ overflowWrap: 'anywhere' }}>{target.connectionId !== undefined && `#${target.connectionId} · `}{target.modelId} · {target.baseUrl}</p>}
     {record.report && <FeatureReportDetails report={record.report}/>}
    </section>
   })}</div>
  </div>}
 </details>
}
