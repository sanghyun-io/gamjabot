import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ResultDetail } from '../api/roomResults'
import { useI18n } from '../i18n'
import { resultTextDiff } from '../lib/resultTextDiff'
import MatchNumberJump from './MatchNumberJump'
import DirectorySearch from './DirectorySearch'

function revealChange(row: HTMLTableRowElement | null, scroller: HTMLDivElement | null, phrase: HTMLElement | null) {
  if (!row?.isConnected || !scroller?.isConnected || !row.getClientRects().length || !scroller.getClientRects().length) return
  // Target the phrase rather than the full-width row so long lines reveal their match.
  const destination = phrase?.isConnected && row.contains(phrase) ? phrase : row
  destination.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  // Keep the target below the actual sticky heading rather than assuming its
  // height is the same in every language or window size.
  const top = scroller.getBoundingClientRect().top + scroller.clientTop + (scroller.querySelector('thead')?.getBoundingClientRect().height || 0) + 4
  const rowTop = row.getBoundingClientRect().top
  if (rowTop < top) scroller.scrollTop -= top - rowTop
}

export default function ResultVersionDiff({ before, after, canChange }: { before: ResultDetail; after: ResultDetail; canChange: () => boolean }) {
  const { t } = useI18n(), [all, setAll] = useState(false)
  const [search, setSearch] = useState(''), [caseSensitive, setCaseSensitive] = useState(true)
  const [toolsOpen, setToolsOpen] = useState(false)
  const toolsSummary = useRef<HTMLElement>(null)
  const textual = before.kind !== 'file' && after.kind !== 'file'
  const diff = useMemo(() => textual ? resultTextDiff(before.content, after.content) : null, [textual, before.content, after.content])
  const allChanges = useMemo(() => {
    const starts: number[] = []
    if (diff) diff.lines.forEach((line, index) => { if (line.kind !== 'same' && (index === 0 || diff.lines[index - 1].kind === 'same')) starts.push(index) })
    return starts
  }, [diff])
  const pattern = useMemo(() => search ? new RegExp(search.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), caseSensitive ? 'u' : 'iu') : null, [search, caseSensitive])
  const changes = useMemo(() => {
    if (!diff) return []
    return allChanges.flatMap(start => {
      if (!pattern) return [{ start, target: start }]
      for (let index = start; index < diff.lines.length && diff.lines[index].kind !== 'same'; index++) {
        if (pattern.test(diff.lines[index].text)) return [{ start, target: index }]
      }
      return []
    })
  }, [diff, allChanges, pattern])
  const context = useMemo(() => ({}), [diff, before.result_id, before.version, after.result_id, after.version, search, caseSensitive])
  const [selection, setSelection] = useState<{ context: object; index: number } | null>(null)
  const active = selection?.context === context ? selection.index : -1
  const activeRow = active >= 0 ? changes[active]?.target ?? -1 : -1
  const phraseTarget = useRef<HTMLElement>(null)
  const target = useRef<HTMLTableRowElement>(null), scroller = useRef<HTMLDivElement>(null), regionId = useId()
  const intent = useRef<{ context: object; index: number } | null>(null)
  useLayoutEffect(() => {
    const navigation = intent.current; intent.current = null
    if (navigation?.context === context && navigation.index === active && canChange()) revealChange(target.current, scroller.current, phraseTarget.current)
  }, [context, active, canChange])
  const goTo = (index: number) => {
    if (!canChange() || !Number.isSafeInteger(index) || index < 0 || index >= changes.length) return
    if (index === active) { revealChange(target.current, scroller.current, phraseTarget.current); return }
    intent.current = { context, index }; setSelection({ context, index })
  }
  const move = (direction: number) => {
    if (!changes.length) return
    goTo(active < 0 ? direction > 0 ? 0 : changes.length - 1 : (active + direction + changes.length) % changes.length)
  }
  const changeSearch = (value: string) => {
    if (!canChange()) return
    intent.current = null; setSearch(value.slice(0, 200))
  }
  const showLine = (text: string, selected: boolean) => {
    // One mark only: preserve exact text and avoid thousands of highlight nodes.
    const match = selected ? pattern?.exec(text) : null
    if (!match) return text
    return <>{text.slice(0, match.index)}<mark ref={phraseTarget}>{match[0]}</mark>{text.slice(match.index + match[0].length)}</>
  }
  const included = new Set<number>()
  if (diff) diff.lines.forEach((line, index) => { if (all || line.kind !== 'same') for (let i = Math.max(0, index - 3); i <= Math.min(diff.lines.length - 1, index + 3); i++) included.add(i) })
  return <section className="stack stack--tight result-diff" aria-label={t('results.diff.title', { before: before.version, after: after.version })}>
    <h4>{t('results.diff.title', { before: before.version, after: after.version })}</h4>
    {before.title !== after.title && <p>{t('results.title')}: <del>{before.title}</del> → <ins>{after.title}</ins></p>}
    {before.kind !== after.kind && <p>{t('results.kindLabel')}: {t(`results.kind.${before.kind}`)} → {t(`results.kind.${after.kind}`)}</p>}
    {!textual ? <p>{t('results.diff.file')}</p> : !diff ? <p role="status">{t('results.diff.large')}</p> : diff.added === 0 && diff.removed === 0 ? <p role="status">{t('results.diff.same')}</p> : <>
      <p>{t('results.diff.counts', { added: diff.added, removed: diff.removed })}</p>
      <div className="page-actions">
        <button type="button" className="button button--secondary button--compact" aria-controls={regionId} disabled={!changes.length} onClick={() => goTo(0)}>{t('results.diff.firstChange')}</button>
        <button type="button" className="button button--secondary button--compact" aria-controls={regionId} disabled={!changes.length} onClick={() => move(-1)}>{t('results.diff.previousChange')}</button>
        <button type="button" className="button button--secondary button--compact" aria-controls={regionId} disabled={!changes.length} onClick={() => move(1)}>{t('results.diff.nextChange')}</button>
        <button type="button" className="button button--secondary button--compact" aria-controls={regionId} disabled={!changes.length} onClick={() => goTo(changes.length - 1)}>{t('results.diff.lastChange')}</button>
      </div>
      <p className="metadata" role="status">{t(active < 0 ? 'results.diff.changeCount' : 'results.diff.changePosition', { count: changes.length, current: active + 1 })}</p>
      <details className="compact-details" onToggle={event => {
        if (event.target === event.currentTarget) setToolsOpen(event.currentTarget.open)
      }}>
        <summary ref={toolsSummary}>{t('results.diff.findTools')}{search && <span className="metadata"> · {t('results.diff.activePhrase', { query: search })}</span>}</summary>
        <div className="stack stack--tight">
      <DirectorySearch value={search} onChange={changeSearch}
        label={t('results.diff.findTitle')} hint={t('results.diff.findHint')} maxLength={200}
        shown={changes.length} total={allChanges.length} known loading={false} controls={regionId} preventSubmit
        onSearchSubmit={search && changes.length ? () => move(1) : undefined}
        onSearchPrevious={search && changes.length ? () => move(-1) : undefined}/>
      <label className="checkbox-row"><input type="checkbox" checked={caseSensitive} onChange={event => {
        if (canChange()) { intent.current = null; setCaseSensitive(event.target.checked) }
      }}/>{t('results.textFind.caseSensitive')}</label>
      <p className="field-hint">{t('results.diff.findScope')}</p>
      <p className="field-hint">{t('results.diff.phraseReveal')}</p>
      {search && !changes.length && <p role="status">{t('results.diff.findNone')}</p>}
      <MatchNumberJump context={context} count={changes.length} active={active} canUse={canChange} onJump={goTo} controls={regionId}
        labels={{ label: t('results.diff.jumpLabel'), action: t('results.diff.jumpAction'), help: t('results.diff.jumpHelp', { count: changes.length }) }}/>
      <p className="field-hint">{t('results.diff.changeHelp')}</p>
          <p className="field-hint">{t('results.diff.toolsRetained')}</p>
        </div>
      </details>
      {search && !toolsOpen && <div><button type="button" className="button button--ghost button--compact" onClick={() => {
        if (!canChange()) return
        const summary = toolsSummary.current
        if (document.visibilityState === 'visible' && document.hasFocus() && summary?.isConnected
          && summary.getClientRects().length && !summary.closest('[hidden], [inert], [aria-hidden="true"]')) summary.focus({ preventScroll: true })
        changeSearch('')
      }}>{t('directory.clear')}</button></div>}
      <label className="checkbox-label"><input type="checkbox" checked={all} onChange={event => { if (canChange()) setAll(event.target.checked) }}/>{t('results.diff.showAll')}</label>
      <div ref={scroller} id={regionId} className="result-diff__scroll" role="region" tabIndex={0} aria-label={t('results.diff.title', { before: before.version, after: after.version })}><table><thead><tr><th scope="col">v{before.version}</th><th scope="col">v{after.version}</th><th scope="col">{t('results.diff.lines')}</th></tr></thead><tbody>{diff.lines.map((line, index) => !included.has(index) ? (index === 0 || included.has(index - 1) ? <tr key={index}><td colSpan={3} className="metadata">{t('results.diff.unchanged')}</td></tr> : null) : <tr key={index} ref={index === activeRow ? target : undefined} aria-current={index === activeRow ? 'true' : undefined} className={`result-diff__${line.kind}${index === activeRow ? ' attention-target' : ''}`}><td>{line.beforeLine ?? '—'}</td><td>{line.afterLine ?? '—'}</td><td><span className="sr-only">{t(line.kind === 'same' ? 'results.diff.sameLine' : `results.diff.${line.kind}`)}</span><pre><span aria-hidden="true">{line.kind === 'added' ? '+ ' : line.kind === 'removed' ? '− ' : '  '}</span>{showLine(line.text, index === activeRow)}{!line.text.endsWith('\n') && <span className="metadata"> ⏎ {t('results.diff.noNewline')}</span>}{line.text.endsWith('\r\n') && <span className="metadata"> CRLF</span>}</pre></td></tr>)}</tbody></table></div>
    </>}
    <p className="field-hint">{t('results.diff.limit')}</p>
  </section>
}
