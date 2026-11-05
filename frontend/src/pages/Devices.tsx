import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { mobileAccessApi, remoteApi } from '../api/remote'
import { AppShell, PageHeader } from '../components/ui'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import MobileLibraryAccess from '../components/MobileLibraryAccess'

export default function Devices() {
  const { t } = useI18n()
  const owner = useAuthStore(s => s.user?.id)
  const epoch = useAuthStore(s => s.epoch)
  const [origin, setOrigin] = useState<string | null>(null)
  const lock = useRef(false), mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [owner, epoch])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [pair, setPair] = useState<{code: string; expires_at: number; url: string} | null>(null)
  const status = useQuery({ queryKey: ['mobile-access', owner, epoch], queryFn: mobileAccessApi.status })
  const { data: devices = [], isLoading, isError } = useQuery({ queryKey: ['devices', owner, epoch], queryFn: remoteApi.listDevices })
  async function action(work: () => Promise<unknown>) {
    if (lock.current) return
    lock.current = true; setBusy(true); setError('')
    const current = () => mounted.current && useAuthStore.getState().user?.id === owner && useAuthStore.getState().epoch === epoch
    try { await work(); if (current()) await status.refetch() }
    catch { if (current()) setError('요청 결과를 확인하지 못했습니다. 아래 새로고침으로 실제 상태를 확인한 뒤 직접 다시 시도하세요. 자동 재시도하지 않습니다.') }
    finally { lock.current = false; if (current()) setBusy(false) }
  }
  const value = status.data
  return <AppShell><PageHeader title={t('devices.mobileTitle')} backTo="/" width="medium" />
  <main className="page-frame page-frame--medium page-content stack mobile-access-page">
    <p className="section-copy">Mac과 감자봇을 켜고 로그인해 두세요. 휴대폰도 같은 개인 Tailscale 네트워크에 연결합니다.</p>
    <section className="surface stack stack--tight mobile-access-page__status">
      <div className="split"><h2 className="section-title">연결 상태</h2><button type="button" className="button button--ghost button--compact" disabled={busy} onClick={() => { setPair(null); void status.refetch() }}>새로고침</button></div>
      <p role="status" className={value?.listening ? 'status-text status-text--success' : 'status-text status-text--warning'}>{status.isLoading ? t('common.loading') : value?.enabled ? value.listening ? '휴대폰 접속 가능' : '수신기 미실행 · 아래 연결 설정을 확인하세요' : '모바일 연결 꺼짐'}</p>
      {value?.origin && <a className="text-link mobile-access-page__address" href={value.origin} target="_blank" rel="noreferrer">{value.origin}</a>}
      {status.isError && <p role="alert" className="notice notice--warning">연결 상태를 읽지 못했습니다. Mac의 감자봇에서 로그인한 뒤 새로고침하세요.</p>}
    </section>
    {error && <p role="alert" className="notice notice--warning">{error}</p>}
    <section className="surface stack stack--tight mobile-access-page__pairing">
      <div className="split"><div><h2 className="section-title">새 휴대폰 연결</h2><p className="field-hint">휴대폰에서 위 주소를 열고 코드를 입력하세요.</p></div>
      <button className="button button--primary" disabled={busy || !value?.listening} onClick={() => action(async () => {
        const before = useAuthStore.getState(); const result = await mobileAccessApi.pairing()
        if (before.user?.id === useAuthStore.getState().user?.id && before.epoch === useAuthStore.getState().epoch) setPair(result)
      })}>연결 코드 만들기</button></div>
      {pair && <div className="notice notice--info mobile-access-page__code"><p>휴대폰 주소: <a href={pair.url} target="_blank" rel="noreferrer">{pair.url}</a></p><p>연결 코드: <strong className="text-prewrap">{pair.code}</strong></p><p>만료: {new Date(pair.expires_at * 1000).toLocaleTimeString()} · 한 번만 사용 · 새 코드를 만들면 이전 코드는 무효</p></div>}
      <p className="field-hint">내 기기만 연결하세요. 대화·작업 열람, 메시지 전송·한 번 승인·거절·중지가 가능합니다.</p>
    </section>
    <section className="stack stack--tight mobile-access-page__devices">
      <h2 className="section-title">연결된 휴대폰</h2>
      {status.isSuccess && value?.devices.length === 0 && <p className="section-copy">연결된 휴대폰이 없습니다.</p>}
      <ul className="stack stack--tight">{value?.devices.map(d => <li className="surface mobile-access-page__device-row" key={d.id}><div className="mobile-access-page__device-info"><strong>{d.name}</strong><p className="metadata">만료: {new Date(d.expires_at * 1000).toLocaleString()}</p></div>{owner&&<MobileLibraryAccess key={`${owner}:${epoch}:${d.id}`} device={d} owner={owner} epoch={epoch} ownerEpoch={value?.owner_epoch} disabled={busy||status.isError} onRefresh={async()=>{const result=await status.refetch();if(result.isError)throw Error('Status unavailable')}}/>}<button className="button button--secondary button--compact" disabled={busy} onClick={() => action(async () => { await mobileAccessApi.revoke(d.id); setPair(null) })}>이 기기 연결 해제</button></li>)}</ul>
      <p className="field-hint">휴대폰을 분실하면 바로 연결을 해제하세요.</p>
    </section>
    <details className="compact-details mobile-access-page__settings"><summary>연결 설정 · Tailscale</summary><section className="surface stack">
      <p>Tailscale Serve의 개인 HTTPS 경로를 아래 로컬 포트로 연결합니다. 기존 경로를 덮지 말고 사용하지 않는 포트(예: 8443)를 쓰세요. 공개 인터넷에 노출하는 Funnel은 사용하지 않습니다.</p>
      <pre className="text-prewrap">tailscale serve --bg --https=8443 http://127.0.0.1:{value?.loopback_port ?? 8432}</pre>
      <label className="field">이 Mac의 Tailscale HTTPS 주소
        <input className="input" type="url" placeholder="https://내-Mac.내-네트워크.ts.net:8443" value={origin ?? value?.origin ?? ''} onChange={e => { setOrigin(e.target.value); setPair(null) }} disabled={busy} />
      </label>
      <p className="field-hint">주소를 변경하면 이전 휴대폰 연결이 해제됩니다. 아래 버튼은 감자봇 수신기만 켜거나 끄며 Tailscale Serve 설정은 바꾸지 않습니다.</p>
      <div className="row-actions"><button className="button button--primary" disabled={busy || status.isLoading} onClick={() => action(async () => { setPair(null); await mobileAccessApi.configure(true, origin ?? value?.origin ?? '') })}>모바일 연결 켜기</button>
        <button className="button button--secondary" disabled={busy || !value?.enabled} onClick={() => action(async () => { setPair(null); await mobileAccessApi.configure(false, '') })}>모바일 연결 끄기</button></div>
      <p className="field-hint">이것은 감자봇의 기기 인증입니다. ChatGPT OAuth·API 키 등 모델 인증과 별개이며 AI 자격증명을 휴대폰에 전달하지 않습니다. 모바일에서는 광범위 자동 승인이나 방 세션 승인을 만들 수 없습니다.</p>
    </section></details>
    <details className="compact-details"><summary>이전 기기 기록</summary><p>{t('devices.unavailable')}</p>{isLoading && <p>{t('common.loading')}</p>}{isError && <p role="alert">{t('devices.loadFailure')}</p>}
      <ul>{devices.map(device => <li key={device.device_id}>{device.device_name} · {t('devices.previous')}</li>)}</ul></details>
  </main></AppShell>
}
