import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { TeamService, type TeamServiceAdapter } from '../components/TeamService'
import { teamService, teamCommands, pendingTeamStore, sharedRoomApi, sharedRoomPendingStore } from '../api/teamService'
import { useAuthStore } from '../stores/authStore'

export default function Teams() {
  const navigate = useNavigate()
  const adapter:TeamServiceAdapter = useMemo(() => ({...teamService,...teamCommands,pendingStore:pendingTeamStore,sharedOptions:sharedRoomApi.options,sharedOpen:sharedRoomApi.open,sharedRead:sharedRoomApi.read,sharedPendingStore:sharedRoomPendingStore,openChat:roomId=>navigate(`/chat/${roomId}`),copy:value=>navigator.clipboard.writeText(value),confirm:message=>window.confirm(message)}),[navigate])
  const owner = useAuthStore(state => state.user?.id)
  const epoch = useAuthStore(state => state.epoch)
  const identity = `${owner ?? 'none'}:${epoch}`
  return <TeamService key={identity} identity={identity} ownerId={owner ?? 0} adapter={adapter}/>
}
