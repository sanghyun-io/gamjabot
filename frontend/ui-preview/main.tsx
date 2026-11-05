import { createRoot } from 'react-dom/client'
import { createPreviewScenario } from './fixtures'
import './networkGuard'
const params = new URLSearchParams(window.location.search)
document.documentElement.dataset.previewTheme = params.get('theme') === 'dark' ? 'dark' : 'light'
const scenario = createPreviewScenario({ screen: params.get('screen') || 'chat', state: params.get('state') || 'ready' })
createRoot(document.getElementById('root')!).render(scenario.element)
if (import.meta.hot) import.meta.hot.dispose(scenario.dispose)
