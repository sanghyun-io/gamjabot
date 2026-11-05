export function errorMessage(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((item) => String(item?.msg || 'Invalid input')).join('; ')
  if (detail && typeof detail === 'object' && 'message' in detail) return String(detail.message)
  return fallback
}
