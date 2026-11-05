import type { Meta, StoryObj } from '@storybook/react-vite'
import { I18nProvider } from '../src/i18n'
import { useArgs } from 'storybook/preview-api'
import AvatarEditor from '../src/components/AvatarEditor'
import AvatarFace from '../src/components/AvatarFace'
import { avatarParts, type AvatarConfig } from '../src/lib/avatarRecipe'
import samples from './avatarSamples.json'

const meta = {
  title: 'Avatars/조합 아바타',
  args: samples[0] as AvatarConfig,
  argTypes: { version: { table: { disable: true }, control: false }, ...Object.fromEntries(Object.entries(avatarParts).map(([part, count]) => [part, { control: 'select', options: Array.from({length:count},(_,i)=>i) }])) },
  render: function Render(args: AvatarConfig) {
    const [, updateArgs] = useArgs()
    return <div style={{maxWidth:640,padding:24}}><I18nProvider><AvatarEditor value={args} onChange={value=>updateArgs(value)} expanded/></I18nProvider></div>
  },
} satisfies Meta<AvatarConfig>
export default meta
type Story = StoryObj<typeof meta>
export const Parts: Story = { name: '부위별 조합 · 다시 뽑기' }
export const OneHundred: Story = { name: '미리 만든100가지 조합', render: () => <div style={{padding:24}}><div className="avatar-gallery">{samples.map((value,i)=><figure key={i}><AvatarFace value={value as AvatarConfig} size={72}/><figcaption>{String(i+1).padStart(3,'0')}</figcaption></figure>)}</div></div> }
