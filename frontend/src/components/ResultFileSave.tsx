import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { invoke, isTauri } from '@tauri-apps/api/core'
import { useQuery } from '@tanstack/react-query'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { readTranscriptSaves, saveTranscript, subscribeTranscriptSaves, transcriptSavePending, transcriptSaveVersion } from '../lib/transcriptFileSave'
import TranscriptSaveHistory from './TranscriptSaveHistory'

const MAX_RESULT_SAVE_BYTES = 2 * 1024 * 1024

type Props = { ownerId: number; epoch: number; roomId: number; resultId: string; version: number; title: string; content: string; canUse: () => boolean }
export default function ResultFileSave({ ownerId, epoch, roomId, resultId, version, title, content, canUse }: Props) {
  const { t } = useI18n()
  const generation = useRef(directoryViewGeneration()).current
  const key = `${ownerId}:${epoch}:${roomId}:result:${resultId}:${version}`
  const root = useRef<HTMLDetailsElement>(null), mounted = useRef(false), intent = useRef(0)
  const live = useRef({ canUse, content, key }); live.current = { canUse, content, key }
  const [expanded, setExpanded] = useState(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; intent.current++ } }, [])
  const ownerCurrent = () => { const auth = useAuthStore.getState(); return auth.user?.id === ownerId && auth.epoch === epoch && directoryViewGeneration() === generation }
  const viewCurrent = () => mounted.current && ownerCurrent() && live.current.key === key && live.current.canUse() && !!root.current?.open && root.current.isConnected && !!root.current.getClientRects().length
  useSyncExternalStore(subscribeTranscriptSaves, transcriptSaveVersion)
  const records = readTranscriptSaves(key), pending = transcriptSavePending()
  const transport = useQuery({ queryKey: ['transcript-save-transport', ownerId, epoch], queryFn: () => invoke<string>('workspace_export_transport'), enabled: expanded && isTauri(), retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false })
  const ready = useMemo(() => {
    // Closed panels and non-native views do not need a full-body byte buffer.
    // UTF-16 code-unit length is a lower bound on TextEncoder's UTF-8 size.
    if (!expanded || !isTauri() || !content.length || content.length > MAX_RESULT_SAVE_BYTES || content.includes('\0')) return false
    return new TextEncoder().encode(content).byteLength <= MAX_RESULT_SAVE_BYTES
  }, [expanded, content])
  const save = () => {
    if (!ready || pending || transport.data !== 'native' || !viewCurrent()) return
    const captured = ++intent.current, frozen = content
    void saveTranscript(key, generation, { text: frozen, title: `${title} · v${version}`, capturedAt: new Date().toISOString(), filenameKind: 'result' },
      t('results.fileSave.panelTitle'), ownerCurrent, () => viewCurrent() && intent.current === captured && live.current.content === frozen)
  }
  if (!isTauri()) return null
  return <details ref={root} className="compact-details" onToggle={event => {
    if (event.target !== event.currentTarget) return
    if (!event.currentTarget.open) intent.current++
    setExpanded(event.currentTarget.open)
  }}>
    <summary>{t('results.fileSave.title', { version })}</summary>
    {expanded && <div className="stack stack--tight">
      <p className="field-hint">{t('results.fileSave.help')}</p>
      <div><button type="button" className="button button--secondary button--compact" disabled={!ready || pending || transport.data !== 'native'} onClick={save}>{t('results.fileSave.action', { version })}</button></div>
      {!ready && <p role="status">{t('results.fileSave.size')}</p>}
      {transport.isFetching && <p role="status">{t('common.loading')}</p>}
      {(transport.isError || transport.isSuccess && transport.data !== 'native') && <p role="status">{t('chat.fileSave.unavailable')}</p>}
      {pending && !['choosing', 'saving'].includes(records.at(-1)?.phase || '') && <p role="status">{t('chat.fileSave.otherPending')}</p>}
      <p className="field-hint">{t('results.fileSave.limit')}</p>
      <TranscriptSaveHistory kind="result" key={key} scope={key} records={records} canUse={viewCurrent}/>
    </div>}
  </details>
}
