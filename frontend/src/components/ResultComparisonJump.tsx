import { parseResultVersionNumber } from '../lib/parseResultVersionNumber'
import { useId, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import { useImeFormGuard } from '../lib/useImeFormGuard'

type Props = { resultId: string; version: number; latest: number | null; canUse: () => boolean; onChoose: (version: number) => void }
export default function ResultComparisonJump({ resultId, version, latest, canUse, onChoose }: Props) {
  const { t } = useI18n(), [value, setValue] = useState(''), [attempted, setAttempted] = useState(false)
  const input = useRef<HTMLInputElement>(null), help = useId(), error = useId()
  const ime = useImeFormGuard(`${resultId}:${version}`)
  const bound = latest !== null && Number.isSafeInteger(latest) && latest > 0 ? latest : null
  const requested = parseResultVersionNumber(value)
  const valid = bound !== null && Number.isSafeInteger(requested) && requested >= 1 && requested <= bound && requested !== version
  const invalid = attempted && bound !== null && !valid
  return <details className="compact-details"><summary>{t('results.comparison.jumpTitle')}</summary>
    <form noValidate className="stack stack--tight" {...ime.handlers} onSubmit={event => {
      event.preventDefault(); event.stopPropagation()
      if (ime.isSubmitBlocked(event) || !canUse() || bound === null) return
      if (!valid) { setAttempted(true); input.current?.focus({ preventScroll: true }); return }
      onChoose(requested); setValue(''); setAttempted(false)
    }}>
      <label className="field">{t('results.comparison.jumpLabel')}<input ref={input} type="text" inputMode="numeric" autoComplete="off" maxLength={24}
        value={value} disabled={bound === null} aria-invalid={invalid || undefined} aria-describedby={`${help}${invalid ? ` ${error}` : ''}`}
        onChange={event => { if (canUse()) { setValue(event.target.value); setAttempted(false) } }}/></label>
      <p id={help} className="field-hint">{t(bound === null ? 'results.comparison.jumpWaiting' : 'results.comparison.jumpHelp', { latest: bound ?? '—', selected: version })}</p>
      {invalid && <p id={error} role="alert" className="metadata metadata--danger">{t('results.comparison.jumpInvalid', { latest: bound!, selected: version })}</p>}
      <div><button type="submit" className="button button--secondary button--compact" disabled={bound === null}>{t('results.comparison.jumpAction')}</button></div>
    </form>
  </details>
}
