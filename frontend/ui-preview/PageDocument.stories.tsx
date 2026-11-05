import type { Meta, StoryObj } from '@storybook/react-vite'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import PageDocumentView from '../src/components/PageDocument'

const syntheticPage = JSON.stringify({format:'gamja.page.v1',title:'합성 작업 현황',datasets:[{id:'tasks',columns:[{key:'name',label:'항목',type:'text'},{key:'amount',label:'금액',type:'number'},{key:'status',label:'상태',type:'text'}],rows:[{name:'Alpha',amount:12,status:'open'},{name:'Beta',amount:9,status:'done'},{name:'Gamma',amount:3,status:'open'}]}],views:[{id:'main',dataset:'tasks',filters:['status_filter']}],blocks:[{type:'heading',level:1,text:'작업 현황'},{type:'markdown',text:'합성 데이터로 만든 화면입니다.'},{type:'control',id:'status_filter',view:'main',field:'status',mode:'select',label:'상태'},{type:'columns',items:[{type:'metric',view:'main',aggregate:'count',label:'작업 수'},{type:'metric',view:'main',aggregate:'sum',field:'amount',label:'금액 합계'}]},{type:'tabs',tabs:[{label:'표',blocks:[{type:'table',view:'main',columns:['name','amount','status'],title:'작업 목록'}]},{label:'차트',blocks:[{type:'chart',kind:'bar',view:'main',category:'name',value:'amount',aggregate:'sum',title:'항목별 금액'}]}]}]})
function Surface(){return <I18nProvider><main className="page-frame page-content"><PageDocumentView content={syntheticPage}/></main></I18nProvider>}
const meta={title:'Screens/페이지 문서',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
export const SyntheticDashboard:Story={name:'합성 대시보드 · 필터와 지표',globals:{locale:'ko'}}
