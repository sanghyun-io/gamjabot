import api from '../lib/api'

export interface RemoteDevice {
  device_id: number
  device_name: string
  access_token?: string
  last_connected_at: string | null
  is_active: boolean
}

export const remoteApi = {
  registerDevice: async (deviceName: string) => {
    const response = await api.post('/remote/devices', { device_name: deviceName })
    return response.data
  },

  listDevices: async (): Promise<RemoteDevice[]> => {
    const response = await api.get('/remote/devices')
    return response.data
  },

  removeDevice: async (deviceId: number) => {
    await api.delete(`/remote/devices/${deviceId}`)
  },

  connectDevice: async (deviceId: number, accessToken: string) => {
    const response = await api.post(`/remote/devices/${deviceId}/connect`, {
      access_token: accessToken,
    })
    return response.data
  },
}

export interface MobileAccessStatus {
  enabled: boolean
  listening: boolean
  origin: string
  loopback_port: number
  owner_epoch?: string
  devices: { id: string; name: string; expires_at: number; last_seen_at: number | null; library_read?: boolean; library_revision?: number }[]
}
export type MobileLibraryCommand = {operation_id:string;enabled:boolean;expected_revision:number;expected_owner_epoch:string}
export type MobileLibraryReceipt = MobileLibraryCommand & {device_id:string;state:'applied'|'rejected'|'fenced';code:string|null;library_read:boolean;library_revision:number}
function mobileLibraryReceipt(value:unknown, command:MobileLibraryCommand,deviceId:string):MobileLibraryReceipt {
 if(!value||typeof value!=='object'||Array.isArray(value))throw Error('Invalid library receipt')
 const r=value as MobileLibraryReceipt
 if(r.device_id!==deviceId||r.operation_id!==command.operation_id||r.enabled!==command.enabled||r.expected_revision!==command.expected_revision||r.expected_owner_epoch!==command.expected_owner_epoch||!['applied','rejected','fenced'].includes(r.state)||!(r.code===null||typeof r.code==='string')||typeof r.library_read!=='boolean'||!Number.isSafeInteger(r.library_revision)||r.library_revision<0)throw Error('Invalid library receipt')
 return r
}
export const mobileAccessApi = {
  status: async () => (await api.get<MobileAccessStatus>('/remote/mobile/status')).data,
  configure: async (enabled: boolean, origin: string) => (await api.put<MobileAccessStatus>('/remote/mobile/settings', { enabled, origin })).data,
  libraryChange: async (id:string,command:MobileLibraryCommand) => mobileLibraryReceipt((await api.post(`/remote/mobile/devices/${id}/library/operations`,command)).data,command,id),
  libraryResolve: async (id:string,command:MobileLibraryCommand) => mobileLibraryReceipt((await api.post(`/remote/mobile/devices/${id}/library/resolve`,command)).data,command,id),
  pairing: async () => (await api.post<{code: string; expires_at: number; url: string}>('/remote/mobile/pairing')).data,
  revoke: async (id: string) => { await api.delete(`/remote/mobile/devices/${id}`) },
}
