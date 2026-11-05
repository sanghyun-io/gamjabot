import type { StorybookConfig } from '@storybook/react-vite'
import path from 'node:path'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const config: StorybookConfig = {
  stories: ['../ui-preview/**/*.stories.tsx'],
  framework: '@storybook/react-vite',
  core: { disableTelemetry: true },
  typescript: { reactDocgen: false },
  async viteFinal(config) {
    // Feed the actual bundled mobile page into the sandboxed synthetic Story.
    config.plugins ??= []
    config.plugins.push({name:'mobile-page-source',resolveId(id){return id==='virtual:gamja-mobile-page'?'\0gamja-mobile-page':undefined},load(id){return id==='\0gamja-mobile-page'?`export default ${JSON.stringify(readFileSync(path.resolve(root,'../backend/app/services/mobile_page.py'),'utf8'))}`:undefined}})
    // Do not inherit the application's /api proxy into the UI workbench.
    config.server = { ...config.server, proxy: {} }
    config.resolve ??= {}
    const aliases = config.resolve.alias
    config.resolve.alias = [
      { find: /^(?:\.\.\/lib\/localHost|\.\/localHost)$/, replacement: path.join(root, 'ui-preview/localHost.ts') },
      { find: '@tauri-apps/api/core', replacement: path.join(root, 'ui-preview/native.ts') },
      ...(Array.isArray(aliases) ? aliases : Object.entries(aliases || {}).map(([find, replacement]) => ({ find, replacement }))),
    ]
    return config
  },
}
export default config
