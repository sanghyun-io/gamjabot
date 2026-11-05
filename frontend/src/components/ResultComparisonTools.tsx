import { useRef, useState } from 'react'
import type { ResultDetail } from '../api/roomResults'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { useI18n } from '../i18n'
import ResultContentCopy from './ResultContentCopy'
import ResultFileSave from './ResultFileSave'

type Props = { value: ResultDetail; primaryVersion: number; ownerId: number; epoch: number; roomId: number; canUse: () => boolean }
export default function ResultComparisonTools({ value, primaryVersion, ownerId, epoch, roomId, canUse }: Props) {
  const { t } = useI18n(), [expanded, setExpanded] = useState(false)
  const root = useRef<HTMLDetailsElement>(null), generation = useRef(directoryViewGeneration()).current
  const allowed = () => canUse() && !!root.current?.open && root.current.isConnected && !!root.current.getClientRects().length && !root.current.closest('[inert], [aria-hidden="true"]')
  if (value.kind === 'file') return null
  return <details ref={root} className="compact-details" onToggle={event => {
    if (event.target !== event.currentTarget) return
    if (!event.currentTarget.open) { setExpanded(false); return }
    if (!allowed()) { event.currentTarget.open = false; setExpanded(false); return }
    setExpanded(true)
  }}><summary>{t('results.comparison.sourceTools', { version: value.version })}</summary>
    {expanded && <div className="stack stack--tight">
      <ResultContentCopy content={value.content} version={value.version} canCopy={allowed}
        copyContext={`comparison:${ownerId}:${epoch}:${generation}:${roomId}:${primaryVersion}:${value.result_id}:${value.version}`}
        findMemory={{ identity: `${ownerId}:${epoch}`, generation, key: `result-find:${roomId}:${value.result_id}:${value.version}` }}/>
      <ResultFileSave ownerId={ownerId} epoch={epoch} roomId={roomId} resultId={value.result_id} version={value.version} title={value.title} content={value.content} canUse={allowed}/>
    </div>}
  </details>
}
