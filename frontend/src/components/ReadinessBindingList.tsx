import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import type { ConnectionReadiness } from '../api/readiness'
import { useI18n } from '../i18n'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'

type Binding = ConnectionReadiness['agent_bindings'][number]
const PAGE_SIZE = 12
export default function ReadinessBindingList({ connectionId, items, canUse, onViewAgents }: {
  connectionId: number; items: readonly Binding[]; canUse: () => boolean; onViewAgents: () => void
}) {
  const { t } = useI18n(), id = useId(), root = useRef<HTMLDivElement>(null), reading = useRef<HTMLUListElement>(null)
  const [query, setQuery] = useState(''), [state, setState] = useState<'all' | Binding['status']>('all'), [anchor, setAnchor] = useState<number | null>(null)
  const pageContext = useMemo(() => ({}), [items, query, state, t]), intent = useRef<{ context: object; id: number; origin: HTMLButtonElement } | null>(null)
  const current = () => canUse() && !!root.current?.isConnected && !!root.current.getClientRects().length && !root.current.closest('[hidden], [inert], [aria-hidden="true"]')
  const match = directoryMatcher(query)
  const found = items.filter(binding => (state === 'all' || binding.status === state) && match([
    String(binding.agent_id), `#${binding.agent_id}`, binding.agent_name, binding.model_id,
    t(`readiness.binding.${binding.status}`), binding.status === 'bound' ? t(`readiness.agentObservation.${binding.observation}`) : '',
  ], binding.agent_id))
  const start = anchor === null ? 0 : Math.max(0, found.findIndex(binding => binding.agent_id === anchor)), end = Math.min(start + PAGE_SIZE, found.length)
  const range = t('attention.pages.range', { from: found.length ? start + 1 : 0, to: end, total: found.length })
  const move = (index: number, origin: HTMLButtonElement) => {
    if (!current() || !found.length || !Number.isSafeInteger(index) || !origin.isConnected || origin.disabled) return
    const target = found[Math.max(0, Math.min(index, found.length - 1))]
    if (!target || target.agent_id === found[start]?.agent_id) return
    origin.focus({ preventScroll: true }); intent.current = { context: pageContext, id: target.agent_id, origin }; setAnchor(target.agent_id)
  }
  useLayoutEffect(() => {
    const value = intent.current; intent.current = null
    if (!value || value.context !== pageContext || value.id !== found[start]?.agent_id || !current()
      || document.visibilityState !== 'visible' || !document.hasFocus() || document.activeElement !== value.origin && document.activeElement !== document.body) return
    const target = reading.current
    if (!target?.isConnected || !target.getClientRects().length || target.closest('[hidden], [inert], [aria-hidden="true"]')) return
    const top = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).filter(node => node.getClientRects().length > 0
      && !node.closest('[hidden], [inert], [aria-hidden="true"]') && getComputedStyle(node).visibility !== 'hidden').at(-1)
    if (top && !top.contains(target)) return
    target.focus({ preventScroll: true }); target.scrollIntoView({ block: 'start', inline: 'nearest' })
  }, [anchor, pageContext, found[start]?.agent_id])
  return <div ref={root} className="stack stack--tight">
    <DirectorySearch exactId exactIdHelp={t('readiness.browse.bindingExactHelp')} value={query} onChange={value => {
      if (current()) { intent.current = null; setQuery(value); setAnchor(null) }
    }} label={t('readiness.browse.bindingFind')} hint={t('readiness.browse.bindingFind')} shown={found.length} total={items.length}
      known loading={false} controls={id} maxLength={200} preventSubmit/>
    <p className="field-hint">{t('readiness.browse.bindingFindScope')}</p>
    <label className="field">{t('commandArchive.state')}<select value={state} aria-controls={id} onChange={event => {
      if (current()) { intent.current = null; setState(event.target.value as typeof state); setAnchor(null) }
    }}><option value="all">{t('commandArchive.all')}</option>{(['bound', 'model_missing', 'inactive'] as const).map(value => <option key={value} value={value}>{t(`readiness.binding.${value}`)}</option>)}</select></label>
    <p className="metadata" role="status">{found.length ? range : t('directory.noMatches')}</p>
    <div className="page-actions">
      <button type="button" className="text-link" disabled={!found.length || start === 0} onClick={event => move(0, event.currentTarget)}>{t('attention.pages.first')}</button>
      <button type="button" className="text-link" disabled={start === 0} onClick={event => move(start - PAGE_SIZE, event.currentTarget)}>{t('attention.pages.previous')}</button>
      <button type="button" className="text-link" disabled={end >= found.length} onClick={event => move(start + PAGE_SIZE, event.currentTarget)}>{t('attention.pages.next')}</button>
      <button type="button" className="text-link" disabled={end >= found.length} onClick={event => move(Math.floor((found.length - 1) / PAGE_SIZE) * PAGE_SIZE, event.currentTarget)}>{t('attention.pages.last')}</button>
    </div>
    <p className="field-hint">{t('commandArchive.pageReadHelp')}</p>
    <ul ref={reading} id={id} tabIndex={found.length ? -1 : undefined} aria-label={range} className="save-history-reading-page">{found.slice(start, end).map(binding => <li key={binding.agent_id}>
      <span><strong>{binding.agent_name} · #{binding.agent_id}</strong><small>{binding.model_id || t('readiness.noModel')}</small></span>
      <span>{t(`readiness.binding.${binding.status}`)}{binding.status === 'bound' && <> · {t(`readiness.agentObservation.${binding.observation}`)}</>}</span>
    </li>)}</ul>
    {Number.isSafeInteger(connectionId) && connectionId > 0 && <Link className="text-link" to={`/agents?connection=${connectionId}`} onClick={event => {
      if (!current()) { event.preventDefault(); return }
      if (!event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && event.button === 0) onViewAgents()
    }}>{t('readiness.browse.bindingManage')}</Link>}
  </div>
}
