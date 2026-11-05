import { isAxiosError } from 'axios'
import { editorSaveFailure } from '../components/EditorSaveNotice'

export type LoginFailure = 'login' | 'unknown' | 'credentials' | 'rememberLocked' | 'rememberUnavailable' | 'savedLoginMismatch' | 'savedLoginChanged' | 'sessionOnlyUnsupported'
export type RegisterFailure = 'rejected' | 'unknown' | 'duplicate' | 'validation'

// Match only the existing Core contract. Never retain/render an error response,
// echoed validation input, credential, token, correlation ID or provider detail.
export function loginFailure(error: unknown, stage: 'login' | 'profile', rememberRequested: boolean, sessionOnly = false): LoginFailure {
  if (isAxiosError(error) && stage === 'login') {
    const status = error.response?.status, detail = error.response?.data?.detail
    if (sessionOnly && status === 404) return 'sessionOnlyUnsupported'
    if (sessionOnly && status === 409 && detail && typeof detail === 'object' && !Array.isArray(detail)) {
      if (detail.code === 'saved_login_owner_mismatch') return 'savedLoginMismatch'
      if (detail.code === 'saved_login_unavailable' || detail.code === 'saved_login_changed') return 'savedLoginChanged'
    }
    if (status === 401 && detail === 'Incorrect email or password') return 'credentials'
    if (status === 503 && rememberRequested && detail && typeof detail === 'object' && !Array.isArray(detail)) {
      if (detail.code === 'credential_locked') return 'rememberLocked'
      if (detail.code === 'secret_store_unavailable') return 'rememberUnavailable'
    }
  }
  return editorSaveFailure(error) === 'unknown' ? 'unknown' : 'login'
}

export function registerFailure(error: unknown): RegisterFailure {
  if (isAxiosError(error)) {
    if (error.response?.status === 400 && error.response.data?.detail === 'User with this email or username already exists') return 'duplicate'
    if (error.response?.status === 422) return 'validation'
  }
  return editorSaveFailure(error) === 'unknown' ? 'unknown' : 'rejected'
}
