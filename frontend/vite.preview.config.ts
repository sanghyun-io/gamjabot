import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  plugins: [react()],
  resolve: { alias: [{ find: /^(?:\.\.\/lib\/localHost|\.\/localHost)$/, replacement: path.resolve(root, 'ui-preview/localHost.ts') }] },
  server: { host: '127.0.0.1', port: 4174, strictPort: true },
})
