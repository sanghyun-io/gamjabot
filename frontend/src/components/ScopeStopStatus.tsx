import { useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { scopeStopsApi, type ScopeStopReceipt } from '../api/scopeStops'
import type { RoomStopState } from '../lib/roomStopWorkspace'
import { useBotScope } from '../lib/useBotScope'
import { useI18n } from '../i18n'
export default function ScopeStopStatus({ roomId, requestId, stopId, state, canUse }: {
  roomId: number; requestId: string | null; stopId: string | null; state: RoomStopState; canUse: () => boolean
}) {
  const { t } = useI18n(), scope = useBotScope(), selected = useRef<ScopeStopReceipt | null>(null)
  const [result, setResult] = useState<ScopeStopReceipt | null>(null), [busy, setBusy] = useState(false), [failure, setFailure] = useState<'unknown' | 'changed' | null>(null)
  const locked = useRef(false)
  const query = useQuery({ queryKey: ['scope-stop-receipt', scope.ownerId, scope.epoch, roomId, requestId, stopId], retry: false,
    enabled: state !== 'sending' && (!requestId || !!stopId), refetchInterval: data => data.state.data?.state === 'pending' ? 1000 : false, queryFn: async () => {
      const receipt = selected.current ? await scopeStopsApi.read(selected.current.stop_id, roomId) : stopId ? await scopeStopsApi.read(stopId, roomId) : await scopeStopsApi.latest(roomId)
      if (!scope.canUse()) throw new Error('Stop receipt view changed')
      // A fresh view may select a historical receipt once. An in-flight cancel without its header never guesses its receipt.
      if (receipt && !selected.current && (stopId || !requestId)) { const frozen = structuredClone(receipt); frozen.revocations.forEach(Object.freeze); Object.freeze(frozen.revocations); selected.current = Object.freeze(frozen) }
      return receipt
    } })
  const shown = result || (query.data?.stop_id === selected.current?.stop_id ? query.data : selected.current) || query.data
  const confirm = async () => {
    const original = selected.current
    if (!scope.canUse() || !canUse() || !original || locked.current || failure === 'changed') return
    locked.current = true; setBusy(true); setFailure(null)
    try { const next = await scopeStopsApi.confirm(original); if (scope.canUse()) setResult(next) }
    catch (error) { if (scope.canUse()) setFailure((error as { response?: { status?: number } })?.response?.status === 409 ? 'changed' : 'unknown') }
    finally { locked.current = false; if (scope.canUse()) setBusy(false) }
  }
  if (requestId && !stopId || !shown && !query.isError) return null
  return <section className="stack stack--tight" style={{ flexShrink: 0 }} aria-label={t('toolScope.stopStatus')}>
    {shown && <p className="metadata" role={failure || shown.state === 'unknown' ? 'alert' : 'status'}>{t(failure === 'changed' ? 'toolScope.stopChanged' : failure || shown.state === 'unknown' ? 'toolScope.stopUnknown' : shown.state === 'pending' ? 'toolScope.stopPending' : 'toolScope.stopSettled')}</p>}
    {query.isError && <p className="metadata" role="status">{t('toolScope.stopReadFailure')}{' '}<button type="button" className="text-link" disabled={query.isFetching} onClick={() => { if (scope.canUse() && canUse()) void query.refetch({ cancelRefetch: false }) }}>{t('common.retry')}</button></p>}
    {selected.current && shown?.state !== 'settled' && <div><button type="button" className="button button--ghost button--compact" disabled={busy || failure === 'changed'} onClick={() => { void confirm() }}>{t(busy ? 'toolScope.stopConfirming' : 'toolScope.stopConfirm')}</button></div>}
  </section>
}
