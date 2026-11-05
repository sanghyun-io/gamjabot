import { parseResultVersionNumber } from '../lib/parseResultVersionNumber'
import { useId, useRef, useState } from 'react'
import type { ResultSummary } from '../api/roomResults'
import { useI18n } from '../i18n'
import { useImeFormGuard } from '../lib/useImeFormGuard'
type Props = {
  resultId: string; version: number; rows: ResultSummary[]; latest: number | null; receivedAt: number;
  loading: boolean; more: boolean; moreFailed: boolean; canUse: () => boolean;
  onChoose: (version: number) => void; onRefresh: () => void; onMore: () => void;
}
export default function ResultVersionNavigator(props: Props) {
  const { t, formatDateTime } = useI18n(), [expanded, setExpanded] = useState(false)
  const [jump, setJump] = useState(''), [attempted, setAttempted] = useState(false)
  const jumpInput = useRef<HTMLInputElement>(null), jumpHelp = useId(), jumpError = useId()
  const ime = useImeFormGuard(`${props.resultId}:${props.version}`)
  const latest = props.latest !== null && Number.isSafeInteger(props.latest) && props.latest > 0 ? props.latest : null
  const requested = parseResultVersionNumber(jump)
  const jumpValid = latest !== null && Number.isSafeInteger(requested) && requested >= 1 && requested <= latest
  const jumpInvalid = attempted && latest !== null && !jumpValid
  const versions = new Map<number, ResultSummary>()
  for (const row of props.rows) if (row.result_id === props.resultId && Number.isSafeInteger(row.version) && row.version > 0) versions.set(row.version, row)
  const choose = (version: number) => { if (props.canUse() && Number.isSafeInteger(version) && version > 0 && version !== props.version) props.onChoose(version) }
  const canStep = props.latest !== null && props.version <= props.latest
  return <section className="stack stack--tight" aria-label={t('results.versionNav.title')}>
    <p className="metadata">{t('results.versionNav.selected', { version: props.version })}{props.latest !== null && <> · {t('results.versionNav.latest', { version: props.latest })}</>}</p>
    <div className="page-actions">
      <button type="button" className="button button--secondary button--compact" disabled={!canStep || props.version <= 1} onClick={() => choose(props.version - 1)}>{t('results.versionNav.previous')}</button>
      <button type="button" className="button button--secondary button--compact" disabled={!canStep || props.latest === null || props.version >= props.latest} onClick={() => choose(props.version + 1)}>{t('results.versionNav.next')}</button>
      {props.latest !== null && props.version !== props.latest && <button type="button" className="button button--secondary button--compact" onClick={() => choose(props.latest!)}>{t('results.versionNav.openLatest', { version: props.latest })}</button>}
    </div>
    {props.receivedAt > 0 && <p className="field-hint">{t('results.versionNav.received', { time: formatDateTime(props.receivedAt, { dateStyle: 'medium', timeStyle: 'medium' }) })}</p>}
    <details className="compact-details"><summary>{t('results.versionNav.jumpTitle')}</summary>
      <form noValidate className="stack stack--tight" {...ime.handlers} onSubmit={event => {
        event.preventDefault(); event.stopPropagation()
        if (ime.isSubmitBlocked(event) || !props.canUse() || latest === null) return
        if (!jumpValid) { setAttempted(true); jumpInput.current?.focus({ preventScroll: true }); return }
        choose(requested); setJump(''); setAttempted(false)
      }}>
        <label className="field">{t('results.versionNav.jumpLabel')}<input ref={jumpInput} type="text" inputMode="numeric" autoComplete="off" maxLength={24}
          value={jump} placeholder={`v${props.version}`} disabled={latest === null} aria-invalid={jumpInvalid || undefined}
          aria-describedby={`${jumpHelp}${jumpInvalid ? ` ${jumpError}` : ''}`}
          onChange={event => { if (props.canUse()) { setJump(event.target.value); setAttempted(false) } }}
          onKeyDown={event => { if (event.key === 'Escape' && jump && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) { event.preventDefault(); event.stopPropagation(); setJump(''); setAttempted(false) } }} /></label>
        <p id={jumpHelp} className="field-hint">{t(latest === null ? 'results.versionNav.jumpWaiting' : 'results.versionNav.jumpHelp', { latest: latest ?? '—', example: Math.min(latest ?? 1, 17) })}</p>
        {jumpInvalid && <p id={jumpError} role="alert" className="metadata metadata--danger">{t('results.versionNav.jumpInvalid', { latest: latest! })}</p>}
        <div className="page-actions"><button type="submit" className="button button--secondary button--compact" disabled={latest === null}>{t('results.versionNav.jumpAction')}</button></div>
      </form>
    </details>
    <details className="compact-details" onToggle={event => { if (props.canUse()) setExpanded(event.currentTarget.open) }}><summary>{t('results.versionNav.pick')}</summary>
      {expanded && <div className="stack stack--tight"><label className="field">{t('results.versionNav.pick')}<select value={props.version} onChange={event => { const version = Number(event.target.value); if (versions.has(version)) choose(version) }}>
        {!versions.has(props.version) && <option value={props.version}>{t('results.versionNav.outside', { version: props.version })}</option>}
        {[...versions.values()].sort((a,b) => b.version - a.version).map(row => <option key={row.version} value={row.version}>v{row.version} · {row.title} · {t(`results.state.${row.state}`)}</option>)}
      </select></label>
      <button type="button" className="button button--secondary button--compact" disabled={props.loading} onClick={() => { if (props.canUse()) props.onRefresh() }}>{t(props.loading ? 'common.loading' : 'results.versionNav.refresh')}</button>
      {props.more && <button type="button" className="button button--secondary button--compact" disabled={props.loading} onClick={() => { if (props.canUse()) props.onMore() }}>{t(props.loading ? 'common.loading' : props.moreFailed ? 'results.reads.nextVersions' : 'results.more')}</button>}
      <p className="field-hint">{t('results.versionNav.limit')}</p></div>}
    </details>
  </section>
}
