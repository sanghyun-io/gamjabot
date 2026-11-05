import type { Meta, StoryObj } from '@storybook/react-vite'
import { MemoryRouter } from 'react-router-dom'
import { I18nProvider } from '../src/i18n'
import Login from '../src/pages/Login'
import './networkGuard'
import '../src/index.css'

function LoginPreview() {
  return <I18nProvider><MemoryRouter initialEntries={['/login']}><Login /></MemoryRouter></I18nProvider>
}

const meta = { title: 'Screens/Login', component: LoginPreview } satisfies Meta<typeof LoginPreview>
export default meta
type Story = StoryObj<typeof meta>
export const WithoutAutoLogin: Story = { name: '자동 로그인 선택 전' }
