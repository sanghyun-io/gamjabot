import { useEffect, useRef } from 'react'
import { useIsMutating, useMutation, useMutationState, useQueryClient } from '@tanstack/react-query'
import { toolsApi, type BroadMode } from '../api/tools'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { editorSaveFailure } from '../components/EditorSaveNotice'

type BroadRequest = Readonly<{ mode: 'off' | 'timed' | 'always'; minutes?: number }>
export const broadModeKey = (owner: number, epoch: number) => ['broad-mode', owner, epoch] as const
export const toolActivityKey = (room: number, owner: number, epoch: number) => ['activity', room, owner, epoch] as const
export const toolGrantsKey = (room: number, owner: number, epoch: number) => ['grants', room, owner, epoch] as const
export const desktopLeaseKey = (owner: number, epoch: number) => ['desktop-lease', owner, epoch] as const
export function toolOwnerCurrent(owner: number, epoch: number) {
  const auth = useAuthStore.getState()
  return !!auth.token && auth.user?.id === owner && auth.epoch === epoch
}
export async function readOwnerToolData<T>(owner: number, epoch: number, read: () => Promise<T>): Promise<T> {
  if (!toolOwnerCurrent(owner, epoch)) throw new Error('Tool data Owner changed')
  const value = await read()
  if (!toolOwnerCurrent(owner, epoch)) throw new Error('Tool data Owner changed')
  return value
}
const validBroad = (value: BroadMode) => value && typeof value.enabled === 'boolean'
  && (value.scope === null || typeof value.scope === 'string')
  && (value.expires_at === null || typeof value.expires_at === 'string' && Number.isFinite(Date.parse(value.expires_at)))
export async function readBroadModeForOwner(owner: number, epoch: number) {
  const value = await readOwnerToolData(owner, epoch, toolsApi.broadMode)
  if (!validBroad(value)) throw new Error('Broad mode response unavailable')
  return value
}

// Both the room header and activity panel use the same query/mutation keys.
// The existing mutation cache owns the pending operation; there is no automatic write retry.
export function useBroadModeAction(owner: number, epoch: number) {
  const cache = useQueryClient(), { t } = useI18n()
  const key = ['owner-broad-mode-write', owner, epoch] as const
  const queryKey = broadModeKey(owner, epoch)
  const acknowledged = useRef(new Set<number>())
  useEffect(() => { acknowledged.current.clear() }, [owner, epoch])
  const history = useMutationState({ filters: { mutationKey: key, exact: true }, select: mutation => ({
    id: mutation.mutationId, status: mutation.state.status,
    request: mutation.state.variables as BroadRequest | undefined,
    failure: editorSaveFailure(mutation.state.error),
  }) })
  const unknown = history.filter(row => row.request && row.status === 'error' && row.failure === 'unknown')
  const busy = useIsMutating({ mutationKey: key, exact: true }) > 0
  const action = useMutation({ mutationKey: key, retry: false,
    mutationFn: async (request: BroadRequest) => {
      if (!toolOwnerCurrent(owner, epoch)) throw new Error('Broad mode Owner changed')
      await cache.cancelQueries({ queryKey, exact: true })
      if (!toolOwnerCurrent(owner, epoch)) throw new Error('Broad mode Owner changed')
      const value = await toolsApi.setBroadMode(request.mode !== 'off', request.mode === 'timed' ? request.minutes : undefined, request.mode === 'always')
      if (!toolOwnerCurrent(owner, epoch)) throw new Error('Broad mode Owner changed')
      if (!validBroad(value) || value.enabled !== (request.mode !== 'off')
        || request.mode === 'always' && value.expires_at !== null
        || request.mode === 'timed' && value.expires_at === null) throw new Error('Broad mode change response unconfirmed')
      return value
    },
    onSuccess: async value => {
      if (!toolOwnerCurrent(owner, epoch)) return
      await cache.cancelQueries({ queryKey, exact: true })
      if (toolOwnerCurrent(owner, epoch)) cache.setQueryData(queryKey, value)
    },
    onSettled: () => { if (toolOwnerCurrent(owner, epoch)) return cache.invalidateQueries({ queryKey, exact: true }) },
  })
  const send = (mode: BroadRequest['mode'], minutes?: number) => {
    if (!toolOwnerCurrent(owner, epoch)
      || cache.getMutationCache().find({ mutationKey: key, exact: true, status: 'pending' })) return
    if (mode === 'timed' && (!Number.isSafeInteger(minutes) || minutes! < 1 || minutes! > 60)) return
    if (mode !== 'off') {
      const current = cache.getQueryState<BroadMode>(queryKey)
      if (!current || current.status !== 'success' || current.fetchStatus !== 'idle' || current.isInvalidated || current.data?.enabled !== false) return
      const unacknowledged = unknown.filter(row => !acknowledged.current.has(row.id))
      if (unacknowledged.length && !window.confirm(t('activity.repeatUnconfirmed'))) return
      if (!toolOwnerCurrent(owner, epoch)) return
      unacknowledged.forEach(row => acknowledged.current.add(row.id))
    }
    action.mutate(Object.freeze({ mode, minutes: mode === 'timed' ? minutes : undefined }))
  }
  return { busy, send, latest: history.at(-1), unknown }
}
