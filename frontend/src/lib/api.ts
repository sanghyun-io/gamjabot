import axios, { CanceledError } from 'axios'
import { useAuthStore } from '../stores/authStore'
import { getLocalEndpoint } from './localHost'
import { ensureSessionContinuity } from './sessionContinuity'

const api = axios.create({
  headers: {
    'Content-Type': 'application/json',
  },
})

// Bind each request to the owner that started it, including requests waiting for IPC.
const owners = new WeakMap<object, { epoch: number; sentToken: string | null }>()

// Request interceptor to add auth token
api.interceptors.request.use(
  async (config) => {
    const auth = useAuthStore.getState()
    const endpoint = await getLocalEndpoint()
    const { base_url, session_token } = endpoint
    if (useAuthStore.getState().epoch !== auth.epoch) {
      throw new CanceledError('Session changed')
    }
    config.baseURL = `${base_url}/api/v1`
    config.headers['X-Gamja-Session'] = session_token
    const managed = !!auth.token && !config.headers.has('Authorization') && !config.url?.startsWith('/auth/')
    if (managed) await ensureSessionContinuity(endpoint, auth)
    const resumed = useAuthStore.getState()
    if (resumed.epoch !== auth.epoch || resumed.user?.id !== auth.user?.id) {
      throw new CanceledError('Session changed')
    }
    const token = managed ? resumed.token : auth.token
    if (token && !config.headers.has('Authorization')) {
      config.headers.Authorization = `Bearer ${token}`
    }
    owners.set(config, {
      epoch: auth.epoch,
      sentToken: managed ? token : null,
    })
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// Response interceptor for error handling
api.interceptors.response.use(
  (response) => response,
  (error) => {
    const owner = error.config && owners.get(error.config)
    const auth = useAuthStore.getState()
    if (error.response?.status === 401 && owner?.sentToken
      && owner.epoch === auth.epoch && owner.sentToken === auth.token) {
      // App renders the sign-in route in place. A hard reload can strand desktop IPC.
      auth.logout()
    }
    return Promise.reject(error)
  }
)

export default api
