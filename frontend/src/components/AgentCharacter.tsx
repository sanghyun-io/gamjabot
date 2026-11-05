import type { AgentVisualState } from './agentStatus'
import AvatarFace from './AvatarFace'
import { avatarFromSeed, validAvatar, type AvatarConfig } from '../lib/avatarRecipe'
import { useAuthStore } from '../stores/authStore'
import { useQuery } from '@tanstack/react-query'
import { agentsApi } from '../api/agents'

/** Saved recipes are Owner-scoped and include inactive Agents for historical faces. */
export function AgentCharacter({ agentId, state = 'idle', size = 'small', avatar }: {
  agentId: number | null; state?: AgentVisualState; size?: 'small' | 'medium'; avatar?: AvatarConfig
}) {
  const identity = useAuthStore(s => `${s.user?.id || 0}:${s.epoch}`)
  const token = useAuthStore(s => s.token)
  const saved = useQuery({ queryKey: ['agent-avatars', identity], staleTime: Infinity, retry: false,
    enabled: !!token && !!agentId && !avatar,
    queryFn: async () => {
      const value = await agentsApi.avatars(), current = useAuthStore.getState()
      if (!current.token || `${current.user?.id || 0}:${current.epoch}` !== identity) throw Error('Owner changed')
      return value
    },
  })
  const value = avatar || saved.data?.[String(agentId)]
  return <span className={`agent-character agent-character--parts agent-character--${state} agent-character--${size}`} aria-hidden="true">
    <AvatarFace value={validAvatar(value) ? value : avatarFromSeed(agentId || 0)} size={size === 'medium' ? 40 : 30}/>
  </span>
}
