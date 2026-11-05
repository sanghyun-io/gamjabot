import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useI18n } from '../i18n'
import type { TeamActivation, TeamCredentials, TeamProfile, TeamSession, TeamCommand, TeamReceipt, PendingTeamStore, PendingTeamOperation, SharedRoomOptions, SharedRoomSelection, SharedRoom, SharedRoomPendingStore, SharedRoomPointer } from '../api/teamService'

export type TeamServiceAdapter = {
  profiles(): Promise<TeamProfile[]>
  status(): Promise<TeamSession>
  login(input: TeamCredentials): Promise<TeamSession>
  activate(input: TeamActivation): Promise<void>
  logout(generation: string): Promise<{ state: 'disconnected'; remote_session_revoked?: boolean }>
  command(generation: string, operation: TeamCommand, payload: Record<string, unknown>, operationId?: string): Promise<Record<string, unknown>>
  receipt(generation: string, operationId: string): Promise<TeamReceipt>
  select(generation: string, teamId: string): Promise<TeamSession>
  copy(value: string): Promise<void>
  confirm(message: string): boolean
  pendingStore: PendingTeamStore
  sharedOptions(): Promise<SharedRoomOptions>
  sharedOpen(team:string, selection:SharedRoomSelection): Promise<SharedRoom>
  sharedRead(team:string, publication:string, generation:string): Promise<SharedRoom>
  sharedPendingStore: SharedRoomPendingStore
  openChat(roomId:number): void
}

type Props = { adapter: TeamServiceAdapter; identity: string; ownerId: number }
const activationUnconfirmed = new Set<string>()
const loginUnconfirmed = new Set<string>()

/** All credentials and invitation secrets are scoped to this mounted screen. */
export function TeamService({ adapter, identity, ownerId }: Props) {
  const { locale } = useI18n()
  const ko = locale === 'ko'
  const text = (k: string, e: string) => ko ? k : e
  const [profiles, setProfiles] = useState<TeamProfile[] | null>(null)
  const [session, setSession] = useState<TeamSession | null>(null)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [activation, setActivation] = useState(false)
  const [profileId, setProfileId] = useState('')
  const [loginUnknown, setLoginUnknown] = useState(() => loginUnconfirmed.has(identity))
  const [activationUnknown, setActivationUnknown] = useState(() => activationUnconfirmed.has(identity))
  const serial = useRef(0)
  const password = useRef<HTMLInputElement>(null), inviteToken = useRef<HTMLInputElement>(null)
  useEffect(() => {
    let alive = true
    const generation = ++serial.current
    setProfiles(null); setSession(null); setMessage(''); setActivationUnknown(activationUnconfirmed.has(identity)); setLoginUnknown(loginUnconfirmed.has(identity))
    Promise.all([adapter.profiles(), adapter.status()]).then(([p, s]) => {
      if (alive && serial.current === generation) { setProfiles(p); setSession(s); if (s.session_generation) { loginUnconfirmed.delete(identity); setLoginUnknown(false) } }
    }).catch(() => { if (alive && serial.current === generation) setMessage(text('팀 서비스 상태를 확인할 수 없습니다. 잠시 후 다시 열어 주세요.', 'Unable to check the team service. Open this page again later.')) })
    return () => { alive = false; ++serial.current; if (password.current) password.current.value = ''; if (inviteToken.current) inviteToken.current.value = '' }
  }, [adapter, identity])

  useEffect(() => {
    if (!session?.session_generation) return
    let alive = true
    const original = session
    const checkSession = async () => {
      try {
        const next = await adapter.status()
        if (!alive) return
        if (next.session_generation !== original.session_generation || next.account_id !== original.account_id || next.team_id !== original.team_id || next.profile_id !== original.profile_id || next.issuer !== original.issuer || next.shared_private_memory_available !== original.shared_private_memory_available || next.shared_text_execution_available !== original.shared_text_execution_available || next.state === 'team_session_expired' || next.state === 'disconnected') {
          setSession(next); setMessage(text('팀 연결이 변경되어 화면을 닫았습니다.', 'The team connection changed; the previous view was closed.'))
        }
      } catch {
        if (alive) { setSession(null); loginUnconfirmed.add(identity); setLoginUnknown(true); setMessage(text('팀 연결 상태를 확인할 수 없습니다. 화면을 다시 열어 확인하세요.', 'Unable to confirm the team connection. Reopen this page to check.')) }
      }
    }
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void checkSession() }, 15000)
    const onVisible = () => { if (document.visibilityState === 'visible') void checkSession() }
    document.addEventListener('visibilitychange', onVisible)
    return () => { alive = false; window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisible) }
  }, [adapter, identity, session?.session_generation, session?.shared_private_memory_available, session?.shared_text_execution_available])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const secret = password.current?.value || ''
    const token = inviteToken.current?.value || ''
    if (password.current) password.current.value = ''
    if (inviteToken.current) inviteToken.current.value = ''
    const generation = serial.current
    const credentials: TeamCredentials = { profile_id: String(form.get('profile') || ''), username: String(form.get('username') || '').trim(), password: secret }
    setBusy(true); setMessage('')
    try {
      if (activation) {
        await adapter.activate({ username:credentials.username, password:credentials.password, invite_code:token })
        if (serial.current === generation) { setActivation(false); activationUnconfirmed.delete(identity); setActivationUnknown(false); setMessage(text('계정이 활성화되었습니다. 팀 계정으로 로그인하세요.', 'Account activated. Sign in to your team account.')) }
      } else {
        const result = await adapter.login(credentials)
        if (serial.current === generation) { setSession(result); loginUnconfirmed.delete(identity); setLoginUnknown(false); activationUnconfirmed.delete(identity); setActivationUnknown(false) }
      }
    } catch (error) {
      if (serial.current === generation) {
        const code = (error as { response?: { data?: { detail?: { code?: string } } } }).response?.data?.detail?.code
        if (activation && ['team_request_invalid','team_permission_denied','team_resource_not_found','team_conflict','team_invitation_invalid','team_invitation_untrusted'].includes(code || '')) setMessage(text('초대 또는 계정 정보를 확인하세요.', 'Check the invitation and account information.'))
        else if (activation) { activationUnconfirmed.add(identity); setActivationUnknown(true); setMessage(text('활성화 결과를 확인할 수 없습니다. 다시 활성화하지 말고 선택한 계정과 비밀번호로 로그인해 확인하세요.', 'Activation could not be confirmed. Do not activate again; check by signing in with the chosen credentials.')) }
        else if (code === 'team_credentials_invalid' || code === 'team_request_invalid') setMessage(text('팀 정보와 계정을 확인하세요.', 'Check the team information and account.'))
        else { loginUnconfirmed.add(identity); setLoginUnknown(true); setMessage(text('연결 결과를 확인할 수 없습니다. 다시 로그인하지 말고 화면을 다시 열어 상태를 확인하세요.', 'Sign-in could not be confirmed. Do not sign in again; reopen this page to check the status.')) }
      }
    } finally { if (serial.current === generation) setBusy(false) }
  }
  async function logout() {
    const generation = serial.current
    setBusy(true); setMessage('')
    try {
      if (!session?.session_generation && session?.state === 'team_session_expired') { setSession({state:'disconnected',shared_execution_available:false}); return }
      if (!session?.session_generation) throw new Error('Team session unavailable')
      const result = await adapter.logout(session.session_generation)
      if (serial.current === generation) { setSession({state:'disconnected',shared_execution_available:false}); setMessage(result.remote_session_revoked === false ? text('이 기기에서 연결을 끊었습니다. 서비스에서 로그아웃했는지는 확인하지 못했습니다.', 'Disconnected on this device. Remote sign-out could not be confirmed.') : '') }
    } catch { if (serial.current === generation) { setSession(null); setMessage(text('연결 종료 상태를 확인하지 못했습니다. 화면을 다시 열어 확인하세요.', 'Could not confirm sign-out. Reopen this page to check.')) } }
    finally { if (serial.current === generation) setBusy(false) }
  }
  const available = profiles?.filter(p => p.ready) || []
  const connected = session && session.state !== 'disconnected'
  return <main className="page-frame page-content stack">
    <header><h1>{text('팀과 공유 Bot', 'Teams and shared Bots')}</h1><p>{session?.shared_text_execution_available ? text('공유 Bot과 텍스트 대화를 사용할 수 있습니다. 내 개인 앱 사용은 대화에서 별도 확인이 필요합니다.', 'Text conversations with shared Bots are available. Personal app use requires a separate check in the conversation.') : text('팀 설정을 관리합니다. 공유 Bot 대화는 아직 사용할 수 없습니다.', 'Manage your team settings. Shared Bot conversations are not available yet.')}</p></header>
    {message && <p role="status" className="notice">{message}</p>}
    {!profiles && !message && <p>{text('팀 서비스 확인 중…', 'Checking the team service…')}</p>}
    {profiles && available.length === 0 && <p className="notice">{text('사용할 수 있는 팀 서비스가 설정되지 않았습니다. 관리자에게 문의하세요.', 'No team service is configured. Contact your administrator.')}</p>}
    {connected ? <section className="surface stack">
      <p>{session.state === 'authenticated' ? (session.team_id ? text('로그인했지만 팀 연결을 확인할 수 없습니다.', 'Signed in, but the team connection could not be confirmed.') : text('팀 계정으로 로그인했습니다. 팀을 선택하세요.', 'Signed in. Choose a team.')) : text('팀 계정 연결됨', 'Team account connected')}</p>
      
      {session.state === 'team_session_expired' ? <p>{text('팀 로그인이 만료되었습니다. 로그아웃 후 다시 로그인하세요.', 'Team sign-in expired. Sign out and sign in again.')}</p> : session.session_generation && session.account_id && session.profile_id && session.issuer && profiles?.some(p => p.profile_id === session.profile_id && p.issuer === session.issuer && p.ready) ? (session.team_id ? <TeamManagement key={`${identity}:${session.session_generation}`} ownerId={ownerId} profile={profiles.find(p => p.profile_id === session.profile_id && p.issuer === session.issuer)!} session={session} adapter={adapter} ko={ko} onSelected={setSession}/> : <TeamPicker key={`${identity}:${session.session_generation}`} ownerId={ownerId} profile={profiles.find(p => p.profile_id === session.profile_id && p.issuer === session.issuer)!} session={session} adapter={adapter} ko={ko} onSelected={setSession}/>) : <p>{text('팀 서비스 구성을 확인할 수 없습니다. 다시 로그인하세요.', 'The team service configuration could not be confirmed. Sign in again.')}</p>} 
      <button type="button" className="button button--secondary" disabled={busy} onClick={logout}>{text('팀 로그아웃', 'Sign out of team')}</button>
    </section> : profiles && available.length > 0 && <section className="surface stack">
      <h2>{activation ? text('초대받은 계정 활성화', 'Activate an invited account') : text('팀 계정 로그인', 'Team sign in')}</h2>
      <form className="form-grid" onSubmit={submit} autoComplete="off">
        {!activation && <label className="field">{text('팀 서비스', 'Team service')} <select name="profile" required value={profileId || available[0]?.profile_id || ''} onChange={event => { setProfileId(event.target.value); if (password.current) password.current.value = ''; if (inviteToken.current) inviteToken.current.value = '' }}>{available.map(p => <option value={p.profile_id} key={p.profile_id}>{p.display_name}</option>)}</select></label>}
        {!activation && <p className="field-hint">{text('연결할 서비스', 'Service')}: {available.find(p => p.profile_id === (profileId || available[0]?.profile_id))?.base_url}</p>}
        {activation && <label className="field">{text('초대 코드', 'Invitation code')} <input ref={inviteToken} type="password" autoComplete="off" required /></label>}
        <label className="field">{text('계정 이름', 'Username')} <input name="username" autoComplete="off" required /></label>
        <label className="field">{text('비밀번호', 'Password')} <input ref={password} type="password" autoComplete="off" required /></label>
        <div className="page-actions"><button className="button button--primary" disabled={busy || (activation ? activationUnknown : loginUnknown)}>{activation ? text('계정 활성화', 'Activate account') : text('로그인', 'Sign in')}</button>
          <button type="button" className="button button--secondary" disabled={busy} onClick={() => { if (password.current) password.current.value = ''; if (inviteToken.current) inviteToken.current.value = ''; setActivation(!activation); setMessage('') }}>{activation ? text('로그인으로 돌아가기', 'Back to sign in') : text('초대 계정 활성화', 'Activate an invitation')}</button></div>
      </form>
    </section>}
  </main>
}

type Row = Record<string, any>
const kinds: Partial<Record<TeamCommand,string>> = { create_team:'team.create', invite_existing:'invite.existing', invite_new:'invite.enrollment', accept_invite:'invite.accept', revoke_invite:'invite.revoke', revoke_member:'member.revoke', put_publication:'publication.put', revoke_publication:'publication.revoke' }

function TeamManagement({ ownerId, profile, session, adapter, ko, onSelected }: { ownerId: number; profile: TeamProfile; session: TeamSession; adapter: TeamServiceAdapter; ko: boolean; onSelected:(s:TeamSession)=>void }) {
  const t = (k: string, e: string) => ko ? k : e
  const generation = session.session_generation!, team = session.team_id!, account = session.account_id!
  const scope = JSON.stringify([ownerId,profile.profile_id,profile.issuer,profile.key_fingerprint,team,account])
  const matches = (p: PendingTeamOperation) => p.owner === ownerId && p.profile_id === profile.profile_id && p.issuer === profile.issuer && p.fingerprint === profile.key_fingerprint && p.team === team && p.account === account
  const [recovery, setRecovery] = useState<{pending?:PendingTeamOperation; error:boolean}>(() => {
    try { return {pending:adapter.pendingStore.read().find(matches),error:false} } catch { return {error:true} }
  })
  const pending = recovery.pending
  function clearPointer(op: PendingTeamOperation) {
    adapter.pendingStore.remove(op)
    setRecovery({pending:adapter.pendingStore.read().find(matches),error:false})
  }
  const [sentInvites,setSentInvites]=useState<Row[]>([])
  const [teams, setTeams] = useState<Row[]>([]), [members, setMembers] = useState<Row[]>([]), [publications, setPublications] = useState<Row[]>([])
  const [revision, setRevision] = useState<number | null>(null), [selected, setSelected] = useState<Row | null>(null)
  const [secret, setSecret] = useState<{id:string;token:string}|null>(null)
  const [notice, setNotice] = useState(''), [busy, setBusy] = useState(false)
  const alive = useRef(true), request = useRef(0), mutationInFlight = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false; ++request.current } }, [])
  async function current() {
    const s = await adapter.status()
    if (s.session_generation !== generation || s.team_id !== team || s.account_id !== account || s.profile_id !== profile.profile_id || s.issuer !== profile.issuer || s.state === 'team_session_expired' || s.state === 'disconnected') throw new Error('changed')
  }
  async function refresh() {
    const n = ++request.current
    setBusy(true); setNotice('')
    try {
      await current()
      const results = await Promise.allSettled(['list_teams','list_members','list_invitations','list_publications','list_sent_invitations'].map(op => adapter.command(generation,op as TeamCommand,{})))
      const [a,b,c,d,e] = results.map(x => x.status === 'fulfilled' ? x.value : {})
      await current()
      if (!alive.current || n !== request.current) return
      if (!Array.isArray(a.items) || !Array.isArray(c.items)) throw new Error('changed')
      const validMembers = b.team_id === team && Array.isArray(b.items) && Number.isSafeInteger(b.revision)
      setTeams(a.items); setMembers(validMembers ? b.items as Row[] : []); setSentInvites(e.team_id === team && Array.isArray(e.items) ? e.items as Row[] : []); setPublications(Array.isArray(d.items) ? d.items as Row[] : []); setRevision(validMembers ? b.revision as number : null)
      if (a.truncated || b.truncated || c.truncated || d.truncated || e.truncated) setNotice(t('목록 일부만 표시됩니다.', 'Only part of the list is shown.'))
    } catch { if (alive.current && n === request.current) { setTeams([]); setMembers([]); setPublications([]); setRevision(null); setSelected(null); setSecret(null); setNotice(t('팀 정보를 확인할 수 없습니다. 연결 상태를 확인해 주세요.', 'Unable to check team information. Check the connection.')) } }
    finally { if (alive.current && n === request.current) setBusy(false) }
  }
  useEffect(() => { void refresh() }, [scope, generation])
  const own = teams.find(x => x.team_id === team), manager = own?.role === 'owner' || own?.role === 'manager'
  async function mutate(operation: TeamCommand, payload: Record<string, unknown>) {
    if (busy || pending || recovery.error || mutationInFlight.current || !kinds[operation]) return
    mutationInFlight.current = true
    let dispatched = false
    const candidate = payload.id ?? payload.invite_id ?? payload.publication_id ?? payload.account_id
    const target = typeof candidate === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(candidate) ? candidate : undefined
    const op: PendingTeamOperation = { owner:ownerId, profile_id:profile.profile_id, issuer:profile.issuer, fingerprint:profile.key_fingerprint, account, id:crypto.randomUUID(), kind:kinds[operation]!, team, target, created_at:Date.now() }
    setBusy(true); setNotice(''); setSecret(null)
    try {
      await current()
      adapter.pendingStore.add(op); setRecovery({pending:op,error:false})
      dispatched = true
      const result = await adapter.command(generation, operation, payload, op.id)
      await current()
      if (!alive.current) return
      const expected = operation === 'create_team' ? result.team_id === team
        : operation === 'invite_new' || operation === 'invite_existing' ? result.invite_id === payload.id && result.state === 'pending'
        : operation === 'accept_invite' ? result.state === 'accepted'
        : operation === 'revoke_invite' || operation === 'revoke_member' ? result.state === 'revoked'
        : operation === 'put_publication' ? result.publication_id === payload.publication_id && Number.isSafeInteger(result.revision)
        : operation === 'revoke_publication' ? result.publication_id === payload.publication_id && result.state === 'revoked' : false
      if (!expected) throw new Error('Unconfirmed result')
      clearPointer(op)
      if (operation === 'invite_new' && typeof result.invite_code === 'string' && typeof result.invite_id === 'string') setSecret({id:result.invite_id,token:result.invite_code as string})
      setNotice(t('완료했습니다. 목록을 새로 확인하세요.', 'Completed. Refresh the list to see changes.'))
    } catch (error) {
      if (!alive.current) return
      const code = (error as {response?:{data?:{detail?:{code?:string}}}}).response?.data?.detail?.code
      if (dispatched && ['team_request_invalid','team_permission_denied','team_resource_not_found','team_conflict','team_command_invalid'].includes(code || '')) {
        try { clearPointer(op); setNotice(t('요청을 처리하지 못했습니다. 정보를 새로 확인하세요.', 'The request was rejected. Refresh the information.')) }
        catch { setRecovery({pending:op,error:true}); setNotice(t('복구 기록을 확인할 수 없습니다.', 'Recovery storage is unavailable.')) }
      } else if (dispatched) {
        setRecovery({pending:op,error:false})
        setNotice(t('결과를 확인하지 못했습니다. 같은 요청을 다시 보내지 말고 결과 확인을 누르세요.', 'The result is unknown. Do not resend; check the result.'))
      } else {
        setRecovery({error:true})
        setNotice(t('연결 또는 복구 기록을 확인할 수 없어 요청하지 않았습니다.', 'The request was not sent because the connection or recovery storage could not be checked.'))
      }
    } finally { mutationInFlight.current = false; if (alive.current) setBusy(false) }
  }
  async function check() {
    if (!pending || busy) return
    setBusy(true); setNotice('')
    try {
      await current()
      const receipt = await adapter.receipt(generation,pending.id)
      await current()
      if (!alive.current) return
      if (receipt.state !== 'committed' || receipt.operation_id !== pending.id || receipt.kind !== pending.kind || receipt.team_id !== pending.team) throw new Error('changed')
      clearPointer(pending)
      setNotice(pending.kind === 'invite.enrollment' ? t('초대는 생성됐지만 코드를 다시 볼 수 없습니다. 초대를 회수하고 새로 만드세요.', 'The invitation was created, but its code cannot be shown again. Revoke it and create a new one.') : t('요청이 완료된 것을 확인했습니다. 목록을 새로 확인하세요.', 'The request completed. Refresh the list to see changes.'))
    } catch { if (alive.current) setNotice(t('아직 결과를 확인할 수 없습니다. 다시 보내지 말고 나중에 결과를 확인하세요.', 'The result is still unknown. Do not resend; check again later.')) }
    finally { if (alive.current) setBusy(false) }
  }
  async function read(operation: TeamCommand, payload: Row, setter: (v:Row)=>void) {
    if (busy) return
    setBusy(true); setNotice('')
    try { await current(); const value = await adapter.command(generation,operation,payload); await current(); if (alive.current) setter(value) }
    catch { if (alive.current) { setter({}); setNotice(t('정보를 불러올 수 없습니다.', 'Could not load the information.')) } }
    finally { if (alive.current) setBusy(false) }
  }
  const disabled = busy || !!pending || recovery.error
  return <div>
    <div className="page-actions"><button type="button" className="button button--secondary" disabled={busy} onClick={refresh}>{t('팀 정보 새로고침','Refresh team information')}</button></div>
    {notice && <p role="status" className="notice">{notice}</p>}
    {recovery.error && <p role="alert" className="notice">{t('복구 기록을 확인할 수 없어 변경을 잠갔습니다. 기록을 지우지 말고 도움을 요청하세요.','Recovery storage cannot be read; changes are locked. Do not clear it; ask for help.')}</p>}
    {pending && <div className="notice">{pending.target && <details><summary>{t('참조 정보','Reference information')}</summary><p>{pending.target}</p></details>}<p>{t('요청 결과 확인 필요. 같은 요청을 다시 보내지 않습니다.', 'Request result needs confirmation. The request will not be resent.')}</p><button type="button" className="button button--secondary" disabled={busy} onClick={check}>{t('결과 확인','Check result')}</button></div>}
    <section className="surface stack"><h2>{t('내 팀','My teams')}</h2>{teams.map(x => <p key={x.team_id}>{x.name} · {x.role === 'owner' ? t('소유자','Owner') : x.role === 'manager' ? t('관리자','Manager') : t('멤버','Member')}{x.team_id === team ? ` · ${t('현재 팀','Current team')}` : <button type="button" className="button button--secondary" disabled={disabled} onClick={async()=>{try{await current();const next=await adapter.select(generation,x.team_id);if(next.team_id!==x.team_id || next.account_id!==account || next.profile_id!==profile.profile_id || !next.session_generation || next.session_generation===generation)throw new Error('changed');onSelected(next)}catch{setNotice(t('팀 선택을 확인할 수 없습니다.','Could not confirm team selection.'))}}}>{t('팀 열기','Open team')}</button>}</p>)}</section>
    <section className="surface stack"><h2>{t('멤버','Members')}</h2>{members.map(x => <p key={x.account_id}>{x.username} · {x.role === 'owner' ? t('소유자','Owner') : x.role === 'manager' ? t('관리자','Manager') : t('멤버','Member')} · {x.state === 'active' ? t('활성','Active') : t('접근 회수됨','Access removed')}{manager && x.state === 'active' && x.role !== 'owner' && <button type="button" className="button button--danger" disabled={disabled || revision === null} onClick={() => { if (adapter.confirm(t('이 멤버의 접근을 회수할까요?', 'Remove this member’s access?'))) void mutate('revoke_member',{account_id:x.account_id,expected_revision:revision}) }}>{t('접근 회수','Remove access')}</button>}</p>)}</section>
    {manager && <section className="surface stack"><h2>{t('멤버 초대','Invite a member')}</h2><p>{t('초대는 자동 전송되지 않습니다. 코드는 처음 한 번만 표시됩니다.', 'Invitations are not sent automatically. A new account code is shown only once.')}</p>
      <form className="form-grid" onSubmit={e => { e.preventDefault(); const f = new FormData(e.currentTarget); const fresh = f.get('kind') === 'new'; const expires = Date.parse(String(f.get('expires'))); if (!Number.isFinite(expires) || revision === null) return; void mutate(fresh?'invite_new':'invite_existing',{id:crypto.randomUUID(),...(fresh ? {account_id:crypto.randomUUID()} : {username:String(f.get('account')).trim()}),role:String(f.get('role')),expires_at:Math.floor(expires/1000),expected_revision:revision}) }}>
        <label className="field">{t('초대 유형','Invitation type')} <select name="kind"><option value="new">{t('새 계정','New account')}</option><option value="existing">{t('기존 계정','Existing account')}</option></select></label>
        <label className="field">{t('기존 계정 이름 (기존 계정 초대 시)','Existing username (for an existing account)')} <input name="account" /></label>
        <label className="field">{t('역할','Role')} <select name="role"><option value="member">{t('멤버','Member')}</option><option value="manager">{t('관리자','Manager')}</option></select></label>
        <label className="field">{t('만료 시각','Expires')} <input type="datetime-local" name="expires" required /></label><button className="button button--primary" disabled={disabled || revision === null}>{t('초대 만들기','Create invitation')}</button>
      </form>
      {secret && <div className="notice"><p>{t('초대 코드를 안전한 방법으로 직접 전달하세요. 이 코드는 다시 표시되지 않습니다.', 'Share the invitation code yourself using a safe method. The code will not be shown again.')}</p><code>{secret.token}</code> <button type="button" className="button button--secondary" onClick={() => void adapter.copy(secret.token).then(() => setNotice(t('코드를 복사했습니다.','Code copied.'))).catch(() => setNotice(t('복사하지 못했습니다.','Could not copy.')))}>{t('코드 복사','Copy code')}</button><button type="button" className="button button--secondary" onClick={() => setSecret(null)}>{t('닫기','Close')}</button></div>}
      <h3>{t('보낸 초대','Sent invitations')}</h3>{sentInvites.map(x=><p key={x.invite_id}>{x.username || t('새 계정','New account')} · {x.state === 'pending' ? t('대기 중','Pending') : x.state === 'accepted' ? t('수락됨','Accepted') : x.state === 'revoked' ? t('회수됨','Revoked') : t('만료됨','Expired')} · {new Date(x.expires_at*1000).toLocaleString()} {x.state === 'pending' && <button type="button" className="button button--danger" disabled={disabled || revision === null} onClick={()=>{if(adapter.confirm(t('초대를 회수할까요?','Revoke this invitation?')))void mutate('revoke_invite',{invite_id:x.invite_id,expected_revision:revision})}}>{t('초대 회수','Revoke invitation')}</button>}</p>)}
    </section>}
    <section className="surface stack"><h2>{t('공유 Bot 설정','Shared Bot settings')}</h2><p>{session.shared_text_execution_available ? t('공유 역할과 지식을 사용하는 개인 대화를 열 수 있습니다.','You can open a private conversation using the shared role and knowledge.') : t('이 설정은 공유 역할과 지식만 저장합니다. 대화 실행은 아직 사용할 수 없습니다.','These settings save shared role and knowledge. Conversation execution is not available yet.')}</p>
      {publications.map((x,index) => <p key={x.publication_id}>{t('공유 설정','Shared settings')} {index+1} · {x.your_role === 'owner' ? t('소유자','Owner') : x.your_role === 'manager' ? t('관리자','Manager') : t('참여자','Collaborator')} <button type="button" className="button button--secondary" disabled={busy} onClick={() => read('get_publication',{publication_id:x.publication_id},setSelected)}>{t('열기','Open')}</button></p>)}
      <button type="button" className="button button--secondary" disabled={busy} onClick={() => setSelected({publication_id:crypto.randomUUID(),revision:0,role_text:'',knowledge_text:'',audience:'team',people:[],your_role:'owner'})}>{t('공유 설정 만들기','Create shared settings')}</button>
      {selected?.publication_id && session.shared_text_execution_available && selected.revision > 0 && selected.state !== 'revoked' && <SharedConversationOpen key={`${scope}:${selected.publication_id}`} ownerId={ownerId} profile={profile} session={session} publication={selected.publication_id} adapter={adapter} ko={ko} />}
      {selected?.publication_id && <PublicationForm key={`${selected.publication_id}:${selected.revision}`} item={selected} members={members} disabled={disabled} ko={ko} save={payload => mutate('put_publication',payload)} revoke={() => { if (adapter.confirm(t('공유 설정을 회수할까요?','Revoke these shared settings?'))) void mutate('revoke_publication',{publication_id:selected.publication_id,expected_revision:selected.revision}) }}/>} 
    </section>
  </div>
}

function PublicationForm({item,members,disabled,ko,save,revoke}:{item:Row;members:Row[];disabled:boolean;ko:boolean;save:(payload:Row)=>void;revoke:()=>void}) {
  const t=(k:string,e:string)=>ko?k:e
  const editable = item.your_role === 'owner' || item.your_role === 'manager'
  return <form className="form-grid" onSubmit={e=>{e.preventDefault();const f=new FormData(e.currentTarget);const audience=String(f.get('audience')); const people=members.filter(x=>f.has(`person:${x.account_id}`)).map(x=>({account_id:x.account_id,role:String(f.get(`role:${x.account_id}`))}));save({publication_id:item.publication_id,role_text:String(f.get('role_text')),knowledge_text:String(f.get('knowledge_text')),audience,people,expected_revision:item.revision})}}>
    <label className="field">{t('공유 역할','Public role')} <textarea name="role_text" defaultValue={item.role_text} readOnly={!editable} required /></label>
    <label className="field">{t('공유 지식','Public knowledge')} <textarea name="knowledge_text" defaultValue={item.knowledge_text} readOnly={!editable} /></label>
    {editable && <><label className="field">{t('접근 범위','Audience')} <select name="audience" defaultValue={item.audience}><option value="team">{t('팀 전체','Whole team')}</option><option value="selected">{t('선택한 멤버','Selected members')}</option></select></label>
      <fieldset><legend>{t('멤버별 접근','Member access')}</legend>{members.filter(x=>x.state === 'active').map(x => { const person=(item.people||[]).find((p:Row)=>p.account_id===x.account_id); return <div key={x.account_id}><label><input type="checkbox" name={`person:${x.account_id}`} defaultChecked={!!person} /> {x.username}</label> <select name={`role:${x.account_id}`} defaultValue={person?.role || 'collaborator'}><option value="collaborator">{t('참여자','Collaborator')}</option><option value="manager">{t('관리자','Manager')}</option></select></div> })}</fieldset>
      <div className="page-actions"><button className="button button--primary" disabled={disabled}>{t('공유 설정 저장','Save shared settings')}</button>{item.revision > 0 && <button type="button" className="button button--danger" disabled={disabled} onClick={revoke}>{t('공유 회수','Revoke sharing')}</button>}</div></>}
  </form>
}

function TeamPicker({ownerId,profile,session,adapter,ko,onSelected}:{ownerId:number;profile:TeamProfile;session:TeamSession;adapter:TeamServiceAdapter;ko:boolean;onSelected:(s:TeamSession)=>void}) {
  const t=(k:string,e:string)=>ko?k:e, generation=session.session_generation!, account=session.account_id!
  const matches=(p:PendingTeamOperation)=>p.owner===ownerId && p.profile_id===profile.profile_id && p.issuer===profile.issuer && p.fingerprint===profile.key_fingerprint && p.account===account && p.team===null
  const [recovery,setRecovery]=useState<{pending?:PendingTeamOperation;error:boolean}>(()=>{try{return {pending:adapter.pendingStore.read().find(matches),error:false}}catch{return {error:true}}})
  const [teams,setTeams]=useState<Row[]>([]),[invitations,setInvitations]=useState<Row[]>([]),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false)
  const active=useRef(true),locked=useRef(false)
  useEffect(()=>{active.current=true;return()=>{active.current=false}},[])
  async function current(){const s=await adapter.status();if(s.session_generation!==generation||s.account_id!==account||s.profile_id!==profile.profile_id||s.issuer!==profile.issuer||s.team_id)throw new Error('changed')}
  async function refresh(){setBusy(true);try{await current();const [a,b]=await Promise.all([adapter.command(generation,'list_teams',{}),adapter.command(generation,'list_invitations',{})]);await current();if(!Array.isArray(a.items)||!Array.isArray(b.items))throw new Error('changed');if(active.current){setTeams(a.items);setInvitations(b.items)}}catch{if(active.current)setNotice(t('팀 목록을 확인할 수 없습니다.','Unable to load teams.'))}finally{if(active.current)setBusy(false)}}
  useEffect(()=>{void refresh()},[generation])
  function clear(p:PendingTeamOperation){adapter.pendingStore.remove(p);setRecovery({pending:adapter.pendingStore.read().find(matches),error:false})}
  async function changeTeam(teamId:string){if(busy||recovery.pending||recovery.error)return;setBusy(true);try{await current();const next=await adapter.select(generation,teamId);if(next.team_id!==teamId||next.account_id!==account||next.profile_id!==profile.profile_id||!next.session_generation||next.session_generation===generation)throw new Error('changed');if(active.current)onSelected(next)}catch{if(active.current)setNotice(t('팀 선택 결과를 확인할 수 없습니다. 화면을 다시 열어 확인하세요.','Could not confirm the selected team. Reopen this page to check.'))}finally{if(active.current)setBusy(false)}}
  async function mutate(kind:'team.create'|'invite.accept',payload:Row,resultTeam:string,target:string){
    if(busy||locked.current||recovery.pending||recovery.error)return
    locked.current=true;setBusy(true);setNotice('');let dispatched=false
    const p:PendingTeamOperation={owner:ownerId,profile_id:profile.profile_id,issuer:profile.issuer,fingerprint:profile.key_fingerprint,team:null,account,id:crypto.randomUUID(),kind,target,result_team:resultTeam,created_at:Date.now()}
    try{await current();adapter.pendingStore.add(p);setRecovery({pending:p,error:false});dispatched=true;const result=await adapter.command(generation,kind==='team.create'?'create_team':'accept_invite',payload,p.id);await current();if(!active.current)return;if(kind==='team.create'?result.team_id!==resultTeam:result.state!=='accepted')throw new Error('unknown');clear(p);setNotice(t('완료했습니다. 팀 목록을 새로 확인하세요.','Completed. Refresh the team list.'))}
    catch(error){if(!active.current)return;const code=(error as {response?:{data?:{detail?:{code?:string}}}}).response?.data?.detail?.code;if(dispatched&&['team_request_invalid','team_permission_denied','team_resource_not_found','team_conflict','team_command_invalid'].includes(code||'')){try{clear(p);setNotice(t('요청을 처리하지 못했습니다.','The request was rejected.'))}catch{setRecovery({pending:p,error:true})}}else if(dispatched){setRecovery({pending:p,error:false});setNotice(t('결과를 확인하지 못했습니다. 다시 보내지 말고 결과를 확인하세요.','The result is unknown. Do not resend; check the result.'))}else{setRecovery({error:true});setNotice(t('연결 또는 복구 기록을 확인할 수 없어 요청하지 않았습니다.','The request was not sent because the connection or recovery storage could not be checked.'))}}
    finally{locked.current=false;if(active.current)setBusy(false)}
  }
  async function check(){const p=recovery.pending;if(!p||busy)return;setBusy(true);try{await current();const r=await adapter.receipt(generation,p.id);await current();if(r.operation_id!==p.id||r.kind!==p.kind||r.state!=='committed'||r.team_id!==p.result_team)throw new Error('unknown');clear(p);if(active.current)setNotice(t('요청이 완료됐습니다. 팀 목록을 새로 확인하세요.','The request completed. Refresh the team list.'))}catch{if(active.current)setNotice(t('결과를 아직 확인할 수 없습니다. 다시 보내지 마세요.','The result is still unknown. Do not resend.'))}finally{if(active.current)setBusy(false)}}
  const disabled=busy||!!recovery.pending||recovery.error
  return <section className="surface stack"><h2>{t('내 팀','My teams')}</h2>
    {notice&&<p role="status" className="notice">{notice}</p>}
    {recovery.error&&<p role="alert">{t('복구 기록을 확인할 수 없어 변경을 잠갔습니다. 기록을 지우지 말고 도움을 요청하세요.','Recovery storage cannot be read; changes are locked. Do not clear it; ask for help.')}</p>}
    {recovery.pending&&<div className="notice"><p>{t('요청 결과 확인 필요. 같은 요청을 다시 보내지 않습니다.','Request result needs confirmation. The request will not be resent.')}</p><button type="button" className="button button--secondary" disabled={busy} onClick={check}>{t('결과 확인','Check result')}</button></div>}
    {teams.map(x=><p key={x.team_id}>{x.name} · {x.role==='owner'?t('소유자','Owner'):x.role==='manager'?t('관리자','Manager'):t('멤버','Member')} <button type="button" className="button button--secondary" disabled={disabled} onClick={()=>changeTeam(x.team_id)}>{t('팀 열기','Open team')}</button></p>)}
    <button type="button" className="button button--secondary" disabled={busy} onClick={refresh}>{t('목록 새로고침','Refresh list')}</button>
    <form className="form-grid" onSubmit={e=>{e.preventDefault();const f=new FormData(e.currentTarget);const id=crypto.randomUUID();void mutate('team.create',{id,name:String(f.get('name')).trim()},id,id)}}><label className="field">{t('새 팀 이름','New team name')} <input name="name" required /></label><button className="button button--primary" disabled={disabled}>{t('팀 만들기','Create team')}</button></form>
    <h3>{t('받은 초대','Invitations received')}</h3>{invitations.map(x=><p key={x.invite_id}>{x.team_name || t('팀 초대','Team invitation')} <button type="button" className="button button--secondary" disabled={disabled} onClick={()=>mutate('invite.accept',{team_id:x.team_id,invite_id:x.invite_id},x.team_id,x.invite_id)}>{t('초대 수락','Accept invitation')}</button></p>)}
  </section>
}

/** Explicitly opened by the user. Only non-secret target references survive a lost response. */
export function SharedConversationOpen({ownerId,profile,session,publication,adapter,ko}:{ownerId:number;profile:TeamProfile;session:TeamSession;publication:string;adapter:TeamServiceAdapter;ko:boolean}) {
  const t=(k:string,e:string)=>ko?k:e, team=session.team_id!, account=session.account_id!, generation=session.session_generation!
  const matches=(p:SharedRoomPointer)=>p.owner===ownerId&&p.profile_id===profile.profile_id&&p.issuer===profile.issuer&&p.fingerprint===profile.key_fingerprint&&p.account===account&&p.team===team&&p.publication===publication
  const [recovery,setRecovery]=useState<{pending?:SharedRoomPointer;error:boolean}>(()=>{try{return {pending:adapter.sharedPendingStore.read().find(matches),error:false}}catch{return {error:true}}})
  const [options,setOptions]=useState<SharedRoomOptions|null>(null),[room,setRoom]=useState<SharedRoom|null>(null),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false)
  const [project,setProject]=useState(''),[connection,setConnection]=useState('')
  const alive=useRef(true),lock=useRef(false)
  useEffect(()=>{alive.current=true;return()=>{alive.current=false}},[])
  async function current(){const s=await adapter.status();if(s.session_generation!==generation||s.team_id!==team||s.account_id!==account||s.profile_id!==profile.profile_id||s.issuer!==profile.issuer||s.state!=='team_ready'||!s.shared_text_execution_available)throw new Error('changed')}
  function bound(r:SharedRoom){if(r.owner_id!==ownerId||r.account_id!==account||r.issuer!==profile.issuer||r.team_id!==team||r.publication_id!==publication||r.session_generation!==generation||r.text_only!==true)throw new Error('changed')}
  async function prepare(){
    if(busy||recovery.error||recovery.pending)return
    setBusy(true);setNotice('');setRoom(null)
    try{
      await current()
      try{const existing=await adapter.sharedRead(team,publication,generation);await current();bound(existing);if(alive.current){setRoom(existing);return}}
      catch(error){const failure=(error as {response?:{status?:number;data?:{detail?:{code?:string}}}}).response;if(failure?.status!==404||failure.data?.detail?.code!=='shared_room_not_found')throw error}
      const result=await adapter.sharedOptions();await current()
      if(alive.current){setOptions(result);setProject(result.projects.length===1?String(result.projects[0].id):'');setConnection(result.connections.length===1?String(result.connections[0].id):'')}
    }catch{if(alive.current)setNotice(t('공유 Bot 상태를 확인할 수 없습니다. 팀 연결을 확인하세요.','Could not confirm the shared Bot. Check your team connection.'))}
    finally{if(alive.current)setBusy(false)}
  }
  async function create(){
    if(lock.current||busy||recovery.error||recovery.pending||!options)return
    const chosen=options.connections.find(x=>x.id===Number(connection)), chosenProject=options.projects.find(x=>x.id===Number(project))
    if(!chosen||!chosenProject)return
    const pointer:SharedRoomPointer={owner:ownerId,profile_id:profile.profile_id,issuer:profile.issuer,fingerprint:profile.key_fingerprint,account,team,publication,project_id:chosenProject.id,connection_id:chosen.id,model_id:chosen.model_id}
    lock.current=true;setBusy(true);setNotice('');let dispatched=false
    try{
      await current();adapter.sharedPendingStore.add(pointer);setRecovery({pending:pointer,error:false});dispatched=true
      const result=await adapter.sharedOpen(team,{session_generation:generation,publication_id:publication,project_id:pointer.project_id,connection_id:pointer.connection_id,model_id:pointer.model_id});await current();bound(result)
      if(result.project_id!==pointer.project_id||result.connection_id!==pointer.connection_id||result.model_id!==pointer.model_id)throw new Error('changed')
      adapter.sharedPendingStore.remove(pointer)
      if(alive.current){setRecovery({error:false});setRoom(result);setOptions(null)}
    }catch{if(alive.current){if(dispatched){setRecovery({pending:pointer,error:false});setNotice(t('대화 준비 결과를 확인하지 못했습니다. 다시 만들지 말고 결과를 확인하세요.','The result is unknown. Do not create another conversation; check the result.'))}else{setRecovery({error:true});setNotice(t('연결 또는 복구 기록을 확인할 수 없어 대화를 만들지 않았습니다.','The conversation was not created because the connection or recovery storage could not be checked.'))}}}
    finally{lock.current=false;if(alive.current)setBusy(false)}
  }
  async function check(){const p=recovery.pending;if(!p||busy)return;setBusy(true);setNotice('');try{await current();const found=await adapter.sharedRead(team,publication,generation);await current();bound(found);if(found.project_id!==p.project_id||found.connection_id!==p.connection_id||found.model_id!==p.model_id)throw new Error('changed');adapter.sharedPendingStore.remove(p);if(alive.current){setRecovery({error:false});setRoom(found)}}catch{if(alive.current)setNotice(t('아직 같은 대화의 결과를 확인할 수 없습니다. 다시 만들지 마세요.','The same conversation could not be confirmed. Do not create another.'))}finally{if(alive.current)setBusy(false)}}
  async function open(){if(!room||busy)return;setBusy(true);try{await current();const found=await adapter.sharedRead(team,publication,generation);await current();bound(found);if(found.room_id!==room.room_id||found.agent_id!==room.agent_id||found.project_id!==room.project_id||found.connection_id!==room.connection_id||found.model_id!==room.model_id)throw new Error('changed');if(alive.current)adapter.openChat(found.room_id)}catch{if(alive.current){setRoom(null);setNotice(t('공유 접근을 확인할 수 없어 대화를 열지 않았습니다.','The conversation was not opened because shared access could not be confirmed.'))}}finally{if(alive.current)setBusy(false)}}
  return <div className="surface stack stack--tight">
    <h3 className="section-title">{t('공유 Bot과 대화','Chat with shared Bot')}</h3>
    <p className="field-hint">{t('공유된 역할과 지식으로 나만의 대화가 만들어집니다. 내 모델 연결과 사용량이 적용되며 공유자의 개인 기억, 앱, 인증 정보는 가져오지 않습니다.','A private conversation uses the shared role and knowledge with your model connection and usage. The publisher’s private memory, apps, and credentials are not copied.')} {session.shared_private_memory_available ? t('선택한 모델이 지원하면 대화로 이 공유 Bot의 내 개인 기억을 관리할 수 있습니다.','If your selected model supports it, you can manage your private memory for this shared Bot in conversation.') : t('개인 기억은 현재 사용할 수 없습니다.','Private memory is currently unavailable.')} {t('내 개인 앱은 대화에서 사용 가능 여부와 계정 사용을 별도로 확인합니다. 공유자가 제공하는 클라우드 앱, 첨부와 음성은 사용할 수 없습니다.','Personal app availability and account use are checked separately in the conversation. Publisher-provided cloud apps, attachments, and voice are unavailable.')}</p>
    {notice&&<p role="status" className="notice">{notice}</p>}
    {recovery.error&&<p role="alert">{t('복구 기록을 확인할 수 없어 대화 만들기를 잠갔습니다.','Recovery storage is unavailable; creating a conversation is locked.')}</p>}
    {recovery.pending&&<div className="notice stack stack--tight"><p>{t('이전 대화 준비 결과를 확인해야 합니다.','The previous creation result needs confirmation.')}</p><button type="button" className="button button--secondary" disabled={busy} onClick={check}>{t('결과 확인','Check result')}</button></div>}
    {!room&&!options&&!recovery.pending&&!recovery.error&&<button type="button" className="button button--secondary" disabled={busy} onClick={prepare}>{t('대화 준비','Prepare conversation')}</button>}
    {options&&!room&&!recovery.pending&&<div className="form-grid"><label className="field">{t('내 프로젝트','My project')}<select value={project} onChange={e=>setProject(e.target.value)}><option value="">{t('선택하세요','Select')}</option>{options.projects.map(x=><option key={x.id} value={x.id}>{x.name}</option>)}</select></label><label className="field">{t('내 모델 연결과 기본 모델','My connection and default model')}<select value={connection} onChange={e=>setConnection(e.target.value)}><option value="">{t('선택하세요','Select')}</option>{options.connections.map(x=><option key={x.id} value={x.id}>{x.label} · {x.model_id}</option>)}</select></label>{(!options.projects.length||!options.connections.length)&&<p className="field-hint">{t('사용할 프로젝트와 기본 모델이 설정된 연결이 필요합니다. 프로젝트 또는 연결 설정에서 추가하세요.','An active project and a connection with a default model are required. Add them in Projects or Connections.')}</p>}<button type="button" className="button button--primary" disabled={busy||!project||!connection} onClick={create}>{t('개인 대화 만들기','Create private conversation')}</button></div>}
    {room&&<div className="stack stack--tight"><p>{t('내 개인 대화가 준비되었습니다.','Your private conversation is ready.')}</p><button type="button" className="button button--primary" disabled={busy} onClick={open}>{t('대화 열기','Open conversation')}</button></div>}
  </div>
}

/** Binding is read from the server; old message history remains separate from send readiness. */
export function SharedRoomStatus({state,onRefresh,ko,memoryAvailable=false}:{state:'checking'|'ready'|'unavailable'|'unknown';onRefresh?:()=>void;ko:boolean;memoryAvailable?:boolean}) {
  const t=(k:string,e:string)=>ko?k:e
  return <div className={`notice ${state==='ready'?'notice--info':'notice--warning'} stack stack--tight`} role="status">
    {state==='ready' ? <><strong>{t('공유 Bot과의 내 개인 대화','Your private conversation with a shared Bot')}</strong><p>{t('현재 공유 역할과 지식과 내 모델 사용량이 적용됩니다.','The current shared role and knowledge and your model usage apply.')} {memoryAvailable?t('선택한 모델이 지원하면 대화에서 이 공유 Bot의 내 개인 기억을 관리할 수 있습니다.','If your selected model supports it, you can manage your private memory for this shared Bot in conversation.'):t('개인 기억은 현재 사용할 수 없습니다.','Private memory is currently unavailable.')} {t('내 개인 앱은 대화에서 사용 가능 여부와 계정 사용을 별도로 확인합니다. 공유자가 제공하는 클라우드 앱, 첨부와 음성은 사용할 수 없습니다.','Personal app availability and account use are checked separately in the conversation. Publisher-provided cloud apps, attachments, and voice are unavailable.')}</p></> : <><strong>{t(state==='checking'?'대화 상태 확인 중':'새 요청을 보낼 수 없습니다.',state==='checking'?'Checking conversation status':'New requests are unavailable.')}</strong><p>{t('표시된 이전 기록은 새 실행이 아닙니다. 공유 접근과 팀 연결을 확인한 뒤 다시 시도하세요.','Displayed history does not indicate a new run. Check shared access and your team connection before trying again.')}</p>{onRefresh&&<button type="button" className="button button--secondary button--compact" onClick={onRefresh}>{t('상태 확인','Check status')}</button>}</>}
  </div>
}
