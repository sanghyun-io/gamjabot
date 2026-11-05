import { useId, useMemo, useState } from 'react'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import { useI18n } from '../i18n'
import type { HandoffEndpoint } from '../lib/handoffEditorState'

const PAGE_SIZE = 50
export type HandoffSourceChoice = { id: string; seq: number; label: string; author: string; text: string; excerpt: string }
export default function HandoffSourcePicker({ label, choices, selected, disabled, onChange }: {
  label: string; choices: HandoffSourceChoice[]; selected: HandoffEndpoint | undefined; disabled: boolean; onChange: (id: string) => void
}) {
  const { t } = useI18n(), id = useId(), [query, setQuery] = useState(''), [anchor, setAnchor] = useState<string | null>(null)
  const matches = useMemo(() => {
    if (!query.trim()) return choices
    const matches = directoryMatcher(query)
    return choices.filter(choice => matches([String(choice.seq), `#${choice.seq}`, choice.author, choice.text]))
  }, [choices, query])
  const chosen = selected && choices.find(choice => choice.id === selected.eventId && choice.seq === selected.seq)
  const found = anchor ? matches.findIndex(choice => choice.id === anchor) : 0
  const start = found >= 0 ? found : 0, end = Math.min(start + PAGE_SIZE, matches.length)
  const page = matches.slice(start, end)
  const pinned = !!chosen && !page.includes(chosen)
  const move = (index: number) => setAnchor(matches[Math.max(0, Math.min(index, matches.length - 1))]?.id || null)
  const search = (value: string) => { setQuery(value); setAnchor(null) }
  return <div className="stack stack--tight">
    <DirectorySearch value={query} onChange={search} label={`${label} · ${t('chat.handoff.sourceSearch.title')}`} hint={t('chat.handoff.sourceSearch.hint')}
      shown={matches.length} total={choices.length} known loading={false} controls={id} preventSubmit disabled={disabled}/>
    {query && !matches.length && <p role="status" className="field-hint">{t('chat.handoff.sourceSearch.noMatches')}</p>}
    <p id={`${id}-pages`} className="field-hint">{t('chat.handoff.sourcePages.help')}</p>
    {(matches.length > PAGE_SIZE || start > 0) && <>
      <div className="page-actions">
        <button type="button" className="button button--secondary button--compact" disabled={disabled || start === 0} aria-controls={id} onClick={() => move(0)}>{t('chat.handoff.pages.first')}</button>
        <button type="button" className="button button--secondary button--compact" disabled={disabled || start === 0} aria-controls={id} onClick={() => move(start - PAGE_SIZE)}>{t('chat.handoff.sourcePages.previous')}</button>
        <button type="button" className="button button--secondary button--compact" disabled={disabled || end >= matches.length} aria-controls={id} onClick={() => move(start + PAGE_SIZE)}>{t('chat.handoff.sourcePages.next')}</button>
        <button type="button" className="button button--secondary button--compact" disabled={disabled || end >= matches.length} aria-controls={id} onClick={() => move(Math.floor((matches.length - 1) / PAGE_SIZE) * PAGE_SIZE)}>{t('chat.handoff.pages.last')}</button>
      </div>
      <p role="status" className="metadata">{t('chat.handoff.pages.range', { from: matches.length ? start + 1 : 0, to: end, total: matches.length })}</p>
    </>}
    {pinned && <p className="field-hint">{t('chat.handoff.sourceSearch.pinned')}</p>}
    <label className="field" htmlFor={id}>{label}<select id={id} disabled={disabled} aria-describedby={`${id}-pages`} value={selected?.eventId || ''} onChange={event => onChange(event.target.value)}>
      <option value="" disabled>{t('chat.handoff.chooseRange')}</option>
      {selected && !chosen && <option value={selected.eventId}>#{selected.seq} · {t('chat.handoff.sourceMissing')}</option>}
      {pinned && chosen && <option value={chosen.id}>{chosen.label}</option>}
      {page.map(choice => <option key={choice.id} value={choice.id}>{choice.label}</option>)}
    </select></label>
  </div>
}
