import { useEffect, useState } from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AxiosHeaders } from 'axios'
import api from '../src/lib/api'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import BrowserProfiles from '../src/components/BrowserProfiles'
import type { BrowserProfile } from '../src/components/BrowserProfileActionDialog'

// Synthetic browser frames only. No Core, credentials, real pixels or model calls.
async function frame(label: string, count: number): Promise<Blob> {
  const canvas = document.createElement('canvas'); canvas.width = 960; canvas.height = 540
  const paint = canvas.getContext('2d')!
  paint.fillStyle = '#f4f6f2'; paint.fillRect(0, 0, 960, 540)
  paint.fillStyle = '#163e32'; paint.fillRect(0, 0, 960, 72)
  paint.fillStyle = '#fff'; paint.font = '24px sans-serif'; paint.fillText(`Synthetic ${label}`, 30, 46)
  paint.fillStyle = '#163e32'; paint.font = '30px sans-serif'; paint.fillText(label === 'Research' ? 'Research queue' : 'Draft review', 40, 140)
  for (let i = 0; i < 3; i++) {
    paint.fillStyle = '#dce6de'; paint.fillRect(40, 180 + i * 80, 880, 58)
    paint.fillStyle = '#163e32'; paint.font = '20px sans-serif'; paint.fillText(`Synthetic task ${i + 1}`, 60, 217 + i * 80)
  }
  paint.font = '18px sans-serif'; paint.fillText(`Synthetic capture ${count}; no real site or login`, 40, 490)
  return await new Promise((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(Error('Synthetic frame unavailable')), 'image/jpeg', .8))
}

function WorkspaceFixture({ sharing = false, lost = false, receiving = false, delegatedConflict = false, parked = false, custody }:  { sharing?: boolean; lost?: boolean; receiving?: boolean; delegatedConflict?: boolean; parked?: boolean; custody?: 'parked'|'borrowed'|'unknown'|'returned' }) {
  const roomId = receiving ? 602 : 501
  const [ready, setReady] = useState(false)
  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } }))
  const [fixture] = useState(() => {
    const profiles: BrowserProfile[] = ['Research', 'Writing', 'Spare'].map((name, i) => ({
      id: String(i + 1).repeat(32), name, account_label: 'Synthetic session only', agent_ids: [8 + i],
      origin_urls: ['https://example.invalid'], revision: 1, controller: 'agent', agent_id: 8 + i,
      connected: true, last_used_at: null, expires_in_seconds: 1200,
      home_room_id: 501, shared_room_ids: [],
    }))
    if (custody) { profiles[0].connected = false; profiles[0].controller = custody==='returned'?'disconnected':'settling'; profiles[0].revision = custody==='returned'?2:1; profiles[0].agent_id = null; profiles.splice(1) }
    if (parked) { profiles[0].connected = false; profiles[0].controller = 'parked'; profiles[0].agent_id = null; profiles.splice(1) }
    if (sharing) { profiles[0].connected = false; profiles[0].controller = 'disconnected'; profiles[0].shared_room_ids = [602] }
    if (receiving) { profiles[0].agent_ids = [42]; profiles[0].agent_id = null; profiles.splice(1) }
    return { profiles, failures: new Set<string>(), captures: new Map<string, number>(),
      bindings: [{ room_id: 602, agent_ids: [42] }], allowDelegated: false, lostConsumed: false }
  })
  useEffect(() => {
    const previous = api.defaults.adapter
    if(custody)sessionStorage.removeItem(`gamja.vaultProfileHandoff.unknown:1:${roomId}:${fixture.profiles[0].id}:${'e'.repeat(32)}`)
    useAuthStore.getState().setAuth('synthetic-browser-workspace', { id: 1, email: 'owner@example.invalid', username: 'Preview Owner' })
    api.defaults.adapter = async config => {
      const path = config.url || '', method = config.method || 'get', prefix = `/tools/rooms/${roomId}`
      let data: unknown, headers: Record<string, string> = {}
      const match = path.match(new RegExp(`^${prefix}/browser-profiles/([a-f0-9]{32})/(preview|control|sharing)$`))
      if (path === '/agents' && method === 'get') data = fixture.profiles.map(p => ({ id: p.agent_ids[0], name: p.name + ' Bot', user_id: 1, is_active: true }))
      else if (path === `/conversations/${roomId}/members` && method === 'get') data = fixture.profiles.map(p => ({ room_id: roomId, agent_id: p.agent_ids[0], is_active: true }))
      else if (path === `${prefix}/browser-profiles` && method === 'get') data = { items: structuredClone(fixture.profiles) }
      else if (/\/vault-handoff$/.test(path) && method === 'get') { const profile=fixture.profiles.find(p=>path.includes(p.id)); if (!profile) throw Error('Synthetic profile missing'); const h='e'.repeat(32); data=custody?{version:1,profile_id:profile.id,revision:profile.revision,handoff:{handoff_id:h,owner_id:1,owner_epoch:'a'.repeat(64),profile_id:profile.id,profile_revision:1,room_id:roomId,origin:'https://example.invalid',username:'demo@example.invalid',profile_label:profile.name,account_label:'Demo',state:custody==='returned'?'returned':'prepared',prior_outcome_unknown:false},native_status:{version:1,status:custody==='returned'?'closed':custody==='unknown'?'unknown':'active',handoff_id:h,profile_id:profile.id,profile_revision:1,fill_status:custody==='parked'||custody==='borrowed'?'not_filled':'unknown'},allowed_origins:[['https','example.invalid',443]],can_fill:false,can_return:false,can_recover:custody!=='returned'}:{version:1,profile_id:profile.id,revision:profile.revision,handoff:null,native_status:null,allowed_origins:[],can_fill:false,can_return:false,can_recover:false} }
      else if (/\/vault-handoff\/custody$/.test(path) && method === 'get') { const profile=fixture.profiles.find(p=>path.includes(p.id)); if(!profile)throw Error('Synthetic profile missing'); data=custody&&custody!=='returned'?{version:1,status:custody,handoff_id:'e'.repeat(32),profile_id:profile.id,profile_revision:profile.revision,custody_id:'b'.repeat(32)}:{version:1,status:'unavailable',handoff_id:'e'.repeat(32),profile_id:null,profile_revision:null,custody_id:null} }
      else if (path === `${prefix}/browser-demonstrations` && method === 'get') data = { items: [], next_after_demo_id: null, unavailable_count: 0 }
      else if (match) {
        const p = fixture.profiles.find(p => p.id === match[1]); if (!p) throw Error('Synthetic profile missing')
        if (match[2] === 'sharing' && p.home_room_id !== roomId) throw Error('Synthetic home room required')
        if (match[2] === 'preview' && method === 'get') {
          if (Number(config.params?.revision) !== p.revision || !p.connected || fixture.failures.has(p.id)) throw Error('Synthetic preview unavailable')
          const count = (fixture.captures.get(p.id) || 0) + 1; fixture.captures.set(p.id, count)
          data = await frame(p.name, count)
          headers = { 'x-browser-profile-id': p.id, 'x-browser-profile-revision': String(p.revision),
            'x-browser-context-id': `synthetic-context-${p.id}-${p.revision}`, 'x-captured-at': new Date().toISOString() }
        } else if (match[2] === 'control' && method === 'post') {
          const input = typeof config.data === 'string' ? JSON.parse(config.data) : config.data
          if (input.revision !== p.revision) throw Error('Synthetic profile changed')
          p.revision++; p.controller = input.action === 'takeover' ? 'user' : input.action === 'return' ? 'parked' : 'disconnected'; p.agent_id = null
          p.connected = input.action === 'takeover'; data = structuredClone(p)
        } else if (match[2] === 'sharing' && method === 'get') {
          data = { profile_id: p.id, revision: p.revision, home_room_id: 501, allow_delegated: fixture.allowDelegated, bindings: structuredClone(fixture.bindings),
            rooms: [{ room_id: 602, title: 'Support team', agents: [{ id: 42, name: 'Support Bot', available: true }, { id: 43, name: 'Review Bot', available: true }] },
              { room_id: 603, title: 'Other team', agents: [{ id: 44, name: 'Already assigned Bot', available: false }] }] }
        } else if (match[2] === 'sharing' && method === 'post') {
          const input = typeof config.data === 'string' ? JSON.parse(config.data) : config.data
          if (p.connected || input.revision !== p.revision || typeof input.allow_delegated !== 'boolean') throw Error('Synthetic sharing changed')
          fixture.bindings = structuredClone(input.bindings); fixture.allowDelegated = input.allow_delegated; p.shared_room_ids = fixture.bindings.map(b => b.room_id); p.revision++
          if (lost && !fixture.lostConsumed) {
            fixture.lostConsumed = true
            if (delegatedConflict) { fixture.allowDelegated = false; p.revision++ }
            throw Error('Synthetic response lost after save')
          }
          data = { ...structuredClone(p), allow_delegated: fixture.allowDelegated }
        } else throw Error('Unimplemented synthetic operation')
      } else throw Error(`[UI fixture missing] ${method} ${path}`)
      return { data, status: 200, statusText: 'OK', headers: new AxiosHeaders(headers), config }
    }
    setReady(true)
    return () => { api.defaults.adapter = previous; if(custody)sessionStorage.removeItem(`gamja.vaultProfileHandoff.unknown:1:${roomId}:${fixture.profiles[0].id}:${'e'.repeat(32)}`); client.clear(); useAuthStore.getState().logout() }
  }, [client, fixture, lost, roomId, delegatedConflict, custody])
  return <MemoryRouter><QueryClientProvider client={client}><I18nProvider>
    <main className="stack" style={{ padding: 24, maxWidth: 1200, margin: '0 auto', minWidth: 0 }}>
      <h1 className="page-title">브라우저 작업 공간 · 합성 상태</h1>
      <p className="section-copy">실제 제품 컴포넌트입니다. 브라우저 프로필을 펼치고 각 화면 보기를 눌러 비교합니다. 로그인·모델·실제 사이트를 사용하지 않습니다.</p>
      {!receiving && !parked && !custody && <div className="page-actions">
        <button className="button button--secondary" onClick={() => { fixture.failures.add(fixture.profiles[1].id) }}>합성 둘째 화면 갱신 실패</button>
        <button className="button button--secondary" onClick={() => { fixture.profiles[0].connected = false; fixture.profiles[0].controller = 'disconnected'; void client.invalidateQueries({ queryKey: ['browser-profiles', 501] }) }}>합성 첫 화면 연결 해제</button>
      </div>}
      {ready && <BrowserProfiles roomId={roomId} />}
    </main>
  </I18nProvider></QueryClientProvider></MemoryRouter>
}

const meta = { title: 'Screens/브라우저 작업 공간', component: WorkspaceFixture,
  render: args => <WorkspaceFixture key={[args.sharing,args.receiving,args.lost,args.delegatedConflict,args.parked,args.custody].join(':')} {...args} />,
  parameters: { layout: 'fullscreen' } } satisfies Meta<typeof WorkspaceFixture>
export default meta
type Story = StoryObj<typeof meta>
export const TwoScreens: Story = { name: '화면 두 개와 개별 실패' }
export const LoginSharingRecovery: Story = { name: '로그인 공유 · 응답 유실 후 비교', args: { sharing: true, lost: true } }
export const SharedLoginRoom: Story = { name: '로그인 공유 · 받은 대화의 원본 이동', args: { sharing: true, receiving: true } }
export const DelegatedLoginRecovery: Story = { name: '위임 로그인 · 다른 창 변경과 응답 유실', args: { sharing: true, lost: true, delegatedConflict: true } }

export const ParkedReopen: Story = { name: '다음 작업 대기 · 다시 열기', args: { parked: true } }
export const ParkedClose: Story = { name: '다음 작업 대기 · 명시 종료', args: { parked: true } }

export const CustodyParked: Story = { name: '보관 중 · 부모 프로필', args: { custody: 'parked' } }
export const CustodyBorrowed: Story = { name: 'Bot 사용 중 · 부모 프로필', args: { custody: 'borrowed' } }
export const CustodyUnknown: Story = { name: '확인 필요 · 부모 프로필', args: { custody: 'unknown' } }
export const CustodyReturned: Story = { name: '종료 후 · 부모 프로필', args: { custody: 'returned' } }
