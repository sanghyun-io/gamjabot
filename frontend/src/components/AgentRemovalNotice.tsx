import { useEffect, useRef, useState } from 'react'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'

export type AgentRemovalOutcome = {
  id: number; name: string; identity: string; attempt: number
  phase: 'pending' | 'success' | 'rejected' | 'unknown'
}

export default function AgentRemovalNotice({ outcome, onInspect, onDismiss }: {
  outcome: AgentRemovalOutcome | null; onInspect: () => Promise<boolean>; onDismiss: () => void
}) {
  const { t } = useI18n()
  const [reading, setReading] = useState(false)
  const [observed, setObserved] = useState<'present' | 'absent' | 'error' | null>(null)
  const pending = useRef(false), generation = useRef(0)
  const current = () => outcome?.identity === `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`
  useEffect(() => {
    generation.current++; pending.current = false; setReading(false); setObserved(null)
    return () => { generation.current++ }
  }, [outcome?.identity, outcome?.attempt, outcome?.phase])
  if (!outcome || !current()) return null
  const inspect = async () => {
    if (pending.current || !current()) return
    const run = ++generation.current
    pending.current = true; setReading(true); setObserved(null)
    try {
      const present = await onInspect()
      if (run === generation.current && current()) setObserved(present ? 'present' : 'absent')
    } catch { if (run === generation.current && current()) setObserved('error') }
    finally { if (run === generation.current && current()) { pending.current = false; setReading(false) } }
  }
  const message = { pending: 'agents.removingNamed', success: 'agents.removedNamed', rejected: 'agents.removeRejected', unknown: 'agents.removeUnknown' } as const
  return <div className={`notice stack stack--tight ${outcome.phase === 'success' ? 'notice--success' : outcome.phase === 'pending' ? 'notice--info' : 'notice--warning'}`}>
    <p role={outcome.phase === 'unknown' || outcome.phase === 'rejected' ? 'alert' : 'status'}>{t(message[outcome.phase], { name: outcome.name })}</p>
    {(outcome.phase === 'unknown' || outcome.phase === 'rejected') && <>
      <button type="button" className="button button--secondary button--compact" disabled={reading} onClick={() => void inspect()}>{t(reading ? 'common.loading' : 'agents.inspectRemoval')}</button>
      {observed && <p role={observed === 'error' ? 'alert' : 'status'}>{t(observed === 'error' ? 'common.error' : observed === 'present' ? 'agents.removalStillListed' : 'agents.removalNotListed')}</p>}
    </>}
    {outcome.phase !== 'pending' && <button type="button" className="button button--ghost button--compact" onClick={onDismiss}>{t('common.close')}</button>}
  </div>
}
