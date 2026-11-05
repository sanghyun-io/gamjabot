import api from '../src/lib/api'

// Missing fixtures must fail visibly, never fall through to a real Core/provider.
api.defaults.adapter = async config => {
  throw new Error(`[UI fixture missing] ${config.method?.toUpperCase()} ${config.url}`)
}

const originalFetch = window.fetch.bind(window)
window.fetch = (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input), location.href)
  if (url.origin !== location.origin || /^\/api(?:\/|$)/.test(url.pathname)) {
    return Promise.reject(new Error('[UI workbench] Live network request blocked'))
  }
  return originalFetch(input, init)
}
