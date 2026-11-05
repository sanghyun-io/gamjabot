import type {Meta,StoryObj} from '@storybook/react-vite'
import Scenario from './PreviewScenario'
const meta={title:'Product/Routine reference in Chat',component:Scenario,args:{screen:'chat',state:'routine-chat-personal',incomingMessages:0}} satisfies Meta<typeof Scenario>
export default meta
type Story=StoryObj<typeof meta>
export const PersonalChat:Story={name:'개인 대화 · 루틴 선택',args:{screen:'chat',state:'routine-chat-personal'}}
export const PausedRoutine:Story={name:'개인 대화 · 일시정지 루틴',args:{screen:'chat',state:'routine-chat-paused'}}
export const GroupChat:Story={name:'여러 Bot 방 · 개인 대화로 이어가기',args:{screen:'chat',state:'routine-chat-group'}}
export const Unknown:Story={name:'개인 대화 · 접수 확인 필요',args:{screen:'chat',state:'routine-chat-unknown'}}
export const Rejected:Story={name:'개인 대화 · 변경된 루틴 다시 선택',args:{screen:'chat',state:'routine-chat-rejected'}}
