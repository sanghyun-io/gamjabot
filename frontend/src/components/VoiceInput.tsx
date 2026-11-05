import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Mic } from 'lucide-react'
import { useI18n } from '../i18n'
import { nativeVoice, type VoiceAdapter, type VoiceSnapshot } from '../lib/voiceInput'

// The host supplies a scoped, synchronous insertion into the existing draft; this never sends.
export default function VoiceInput({ canUse, scope, onInsert, onActiveChange, adapter = nativeVoice }: { canUse: () => boolean; scope: () => string | null; onInsert: (text: string) => boolean; onActiveChange?: (active: boolean) => void; adapter?: VoiceAdapter }) {
  const { locale } = useI18n(), ko = locale === 'ko'
  const [state, setState] = useState<VoiceSnapshot>({ phase: 'idle', text: '' })
  const [supported, setSupported] = useState<boolean | null>(null)
  const active = useRef<string | null>(null), startScope = useRef<string | null>(null), started = useRef(false), mounted = useRef(false), live = useRef({ canUse, scope, onInsert })
  live.current = { canUse, scope, onInsert }
  const activeChange = useRef(onActiveChange); activeChange.current = onActiveChange
  useLayoutEffect(() => { activeChange.current?.(['waiting', 'listening', 'stopping', 'review'].includes(state.phase)) }, [state.phase])
  useLayoutEffect(() => () => { activeChange.current?.(false) }, [])
  const scoped = () => live.current.canUse() && !!startScope.current && live.current.scope() === startScope.current
  useEffect(() => {
    mounted.current = true
    const hide = () => { if (document.visibilityState !== 'visible' && active.current) { const token = active.current; active.current = null; void adapter.action('cancel', token).catch(() => {}); setState({ phase: 'idle', text: '' }) } }
    const blur = () => { if (active.current) { const token = active.current; active.current = null; void adapter.action('cancel', token).catch(() => {}); setState({ phase: 'idle', text: '' }) } }
    document.addEventListener('visibilitychange', hide); window.addEventListener('blur', blur)
    return () => { mounted.current = false; document.removeEventListener('visibilitychange', hide); window.removeEventListener('blur', blur); const token = active.current; active.current = null; if (token) void adapter.action('cancel', token).catch(() => {}) }
  }, [adapter])
  useEffect(() => {
    if (!['waiting', 'listening', 'stopping'].includes(state.phase)) return
    let disposed = false, timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      const token = active.current
      if (!token) return
      if (!started.current) { timer = setTimeout(poll, 400); return }
      if (!scoped()) { active.current = null; void adapter.action('cancel', token).catch(() => {}); if (!disposed) setState({ phase: 'idle', text: '' }); return }
      try {
        const next = await adapter.snapshot(token)
        if (disposed || active.current !== token) return
        if (next.phase === 'unknown') { active.current = null; void adapter.action('cancel', token).catch(() => {}) }
        setState(next)
        if (['waiting', 'listening', 'stopping'].includes(next.phase)) timer = setTimeout(poll, 400)
      } catch { if (!disposed && active.current === token) { void adapter.action('cancel', token).catch(() => {}); active.current = null; setState({ phase: 'unknown', text: '' }) } }
    }
    timer = setTimeout(poll, 400)
    return () => { disposed = true; if (timer) clearTimeout(timer) }
  }, [state.phase, adapter])
  const cancel = () => { const token = active.current; active.current = null; activeChange.current?.(false); if (token) void adapter.action('cancel', token).catch(() => {}); setState({ phase: 'idle', text: '' }) }
  useEffect(() => { if (active.current && !scoped()) cancel() })
  const start = async () => {
    if (supported === false || active.current || !live.current.canUse() || !document.hasFocus()) return
    const scopeValue = live.current.scope(); if (!scopeValue) return
    const token = crypto.randomUUID(); startScope.current = scopeValue; started.current = false; active.current = token; activeChange.current?.(true); setState({ phase: 'waiting', text: '' })
    try { const available = await adapter.available(); if (!mounted.current || active.current !== token || !scoped()) { if (active.current === token) { active.current = null; if (mounted.current) setState({ phase: 'idle', text: '' }) } return } setSupported(available); if (!available) { active.current = null; setState({ phase: 'unsupported', text: '' }); return } await adapter.action('start', token); if (!mounted.current || active.current !== token || !scoped()) { if (active.current === token) { active.current = null; if (mounted.current) setState({ phase: 'idle', text: '' }) } await adapter.action('cancel', token) } else started.current = true }
    catch { void adapter.action('cancel', token).catch(() => {}); if (mounted.current && active.current === token) { active.current = null; setState({ phase: 'unknown', text: '' }) } }
  }
  const busy = ['waiting', 'listening', 'stopping'].includes(state.phase)
  const messages: Record<string, string> = ko ? {
    waiting: '권한과 음성 인식 준비를 확인하는 중입니다.', listening: '듣는 중입니다. 완료하면 중지를 누르세요.', stopping: '음성 인식 결과를 확인하는 중입니다.', review: '내용을 확인한 뒤 입력창에 넣으세요.', unsupported: '이 기기 또는 언어에서 기기 내 음성 인식을 사용할 수 없습니다.', permission_needed: '마이크와 음성 인식 권한이 필요합니다. 시스템 설정을 확인하세요.', error: '음성 인식에 실패했습니다. 다시 시도하세요.', unknown: '음성 인식 결과를 확인하지 못했습니다. 다시 시도하세요.',
  } : {
    waiting: 'Checking permissions and preparing speech recognition.', listening: 'Listening. Stop when you are finished.', stopping: 'Finishing recognition.', review: 'Review the text before adding it to your message.', unsupported: 'On-device speech recognition is unavailable for this device or language.', permission_needed: 'Microphone and speech recognition permissions are needed. Check system settings.', error: 'Speech recognition failed. Try again.', unknown: 'The recognition result could not be confirmed. Try again.',
  }
  return <section className="stack stack--tight" aria-label={ko ? '음성 입력' : 'Voice input'}>
    {state.phase === 'idle' && <button type="button" className="button button--secondary button--compact" disabled={supported === false || !canUse()} onClick={() => void start()}><Mic size={16} aria-hidden="true" />{ko ? '음성 입력' : 'Dictate'}</button>}
    {supported === false && state.phase === 'idle' && <p role="status" className="field-hint">{ko ? '이 환경에서는 음성 입력을 사용할 수 없습니다.' : 'Voice input is unavailable in this environment.'}</p>}
    {state.phase !== 'idle' && <p role="status" className="field-hint">{messages[state.phase]}</p>}
    {state.phase === 'review' && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{state.text || (ko ? '인식된 내용이 없습니다.' : 'No speech was recognized.')}</p>}
    {state.phase !== 'idle' && <div className="page-actions">
      {busy && state.phase !== 'stopping' && <button type="button" className="button button--secondary button--compact" onClick={() => { if (state.phase === 'waiting' || !scoped()) { cancel(); return } const token = active.current; if (token) { setState(s => ({ ...s, phase: 'stopping' })); void adapter.action('stop', token).catch(() => { void adapter.action('cancel', token).catch(() => {}); if (active.current === token) { active.current = null; setState({ phase: 'unknown', text: '' }) } }) } }}>{ko ? '중지' : 'Stop'}</button>}
      {state.phase === 'review' && <button type="button" className="button button--secondary button--compact" disabled={!state.text.trim() || !canUse()} onClick={() => { if (document.hasFocus() && scoped() && live.current.onInsert(state.text)) cancel() }}>{ko ? '입력창에 넣기' : 'Add to message'}</button>}
      <button type="button" className="button button--secondary button--compact" onClick={cancel}>{ko ? '취소' : 'Cancel'}</button>
    </div>}
  </section>
}
