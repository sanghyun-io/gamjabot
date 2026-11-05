import type { Meta, StoryObj } from '@storybook/react-vite'
import { Button, InlineNotice, StatusText } from '../src/components/ui'

const meta = { title: 'Components/버튼과 상태', component: Button,
  args: { children: '새 대화', variant: 'primary' },
  argTypes: { variant: { control: 'select', options: ['primary', 'secondary', 'danger', 'ghost'] } },
} satisfies Meta<typeof Button>
export default meta
type Story = StoryObj<typeof meta>
export const ButtonControl: Story = { name: '실제 버튼 · 속성 조절' }
export const Hierarchy: Story = { name: '버튼 위계 · 상태 비교', render: () => <div style={{ padding: 24, display: 'grid', gap: 20 }}>
  <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}><Button variant="primary">보내기</Button><Button>옵션</Button><Button variant="danger">중지</Button><Button disabled>처리 중</Button></div>
  <StatusText tone="warning">승인 필요</StatusText><StatusText tone="busy">작업 중</StatusText>
  <InlineNotice tone="danger">모델 연결을 확인해 주세요.</InlineNotice>
</div> }
