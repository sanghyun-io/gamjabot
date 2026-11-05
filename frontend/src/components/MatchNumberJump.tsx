import { useId, useRef, useState } from 'react'
import { useI18n } from '../i18n'

// Local navigation only: context changes discard the entered destination.
// No match-offset array, API read, body rewrite or automatic scrolling.
export default function MatchNumberJump({ context, count, active, canUse, onJump, labels, controls }: {
  context: object; count: number; active: number; canUse: () => boolean; onJump: (index: number) => void; labels?: { label: string; action: string; help: string }; controls?: string
}) {
  const { t } = useI18n(), helpId = useId()
  const [draft, setDraft] = useState<{ context: object; text: string } | null>(null)
  const composing = useRef(false), imeAfter = useRef(0)
  const value = draft?.context === context ? draft.text : ''
  const digits = value.trim().replace(/[０-９]/g, digit => String(digit.charCodeAt(0) - 0xff10))
  const number = /^[0-9]+$/.test(digits) ? Number(digits) : NaN
  const allowed = Number.isSafeInteger(number) && number >= 1 && number <= count
  const jump = () => {
    if (allowed && canUse() && document.visibilityState === 'visible' && document.hasFocus()) onJump(number - 1)
  }
  return <div className="stack stack--tight">
    <div className="page-actions">
      <label className="field">{labels?.label ?? t('results.textFind.jumpLabel')}
        <input type="text" inputMode="numeric" autoComplete="off" maxLength={24} value={value}
          placeholder={String(active >= 0 ? active + 1 : 1)} aria-describedby={helpId} aria-controls={controls}
          style={{ width: '10ch', maxWidth: '100%' }}
          onChange={event => { if (canUse()) setDraft({ context, text: event.target.value }) }}
          onCompositionStart={() => { composing.current = true }}
          onCompositionEnd={() => { composing.current = false; imeAfter.current = performance.now() + 120 }}
          onKeyDown={event => {
            const ime = composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229 || performance.now() < imeAfter.current
            if (ime && (event.key === 'Escape' || event.key === 'Enter')) { event.stopPropagation(); return }
            if (event.key !== 'Enter') return
            event.preventDefault(); event.stopPropagation()
            if (!event.repeat && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey) jump()
          }}/>
      </label>
      <button type="button" className="button button--secondary button--compact" disabled={!allowed} aria-controls={controls} onClick={jump}>{labels?.action ?? t('results.textFind.jumpAction')}</button>
    </div>
    <p id={helpId} className="field-hint">{labels?.help ?? t('results.textFind.jumpHelp', { count })}</p>
  </div>
}
