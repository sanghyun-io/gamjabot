import type { Meta, StoryObj } from '@storybook/react-vite'
// @ts-expect-error Storybook-only virtual module supplies the real bundled mobile source.
import mobileSource from 'virtual:gamja-mobile-page'
import './networkGuard'

type Mode='ready'|'denied'|'removed'
const page=mobileSource.match(/PAGE='''([\s\S]*?)'''\nJS=r'''/)?.[1]
const script=mobileSource.match(/\nJS=r'''([\s\S]*?)'''\s*$/)?.[1]
if(!page||!script)throw Error('Mobile production source unavailable')
function MobileLibraryStory({mode}:{mode:Mode}){
 const fixture=`
 const mode=${JSON.stringify(mode)},id='11111111-1111-4111-8111-111111111111',sha='a'.repeat(64);
 window.fetch=async(path)=>{const url=new URL(path,'https://fixture.invalid');let body;
 if(url.pathname==='/session')body={paired:true,library_read:mode!=='denied'};
 else if(url.pathname==='/api/rooms')body={items:[],next_cursor:null};
 else if(url.pathname==='/api/library/bots')body={items:[{id:8,name:'합성 Bot'}],next_cursor:null};
 else if(url.pathname==='/api/library/bots/8/results')body={items:[{room_id:501,result_id:id,version:2,kind:'page',title:'합성 Page',author_agent_id:8}],next_cursor:null};
 else if(url.pathname==='/api/library/bots/8/uploads')body={items:[{upload_id:id,bot_id:8,filename:'합성 파일.txt',size_bytes:12,sha256:sha,removed:mode==='removed'}],next_cursor:null};
 else if(url.pathname==='/api/library/bots/8/pins')body={items:[{source:{kind:'result',room_id:501,result_id:id,version:2},revision:1,pinned:true,availability:'available'}],next_cursor:null};
 else if(url.pathname==='/api/library/bots/8/results/501/'+id+'/2')body={room_id:501,result_id:id,version:2,kind:'page',title:'합성 Page',author_agent_id:8,content:JSON.stringify({format:'gamja.page.v1',title:'합성 Page',datasets:[],views:[],blocks:[{type:'markdown',text:'합성 내용'}]}),file_status:null};
 else if(url.pathname==='/api/library/bots/8/uploads/'+id)body={upload_id:id,bot_id:8,filename:'합성 파일.txt',size_bytes:12,sha256:sha,removed:mode==='removed'};
 else throw Error('Synthetic network blocked');return new Response(JSON.stringify(body),{status:200,headers:{'Content-Type':'application/json'}})};
 `
 const source=page!.replace('<script src="/mobile.js" defer></script>',`<script>${fixture}\n${script}</script>`)
 return <iframe title="합성 모바일 보관함" sandbox="allow-scripts" srcDoc={source} style={{width:390,maxWidth:'100%',height:780,border:'1px solid #aaa'}}/>
}
const meta={id:'mobile-library',title:'Screens/모바일 보관함',component:MobileLibraryStory,args:{mode:'ready'}} satisfies Meta<typeof MobileLibraryStory>
export default meta
type Story=StoryObj<typeof meta>
export const Ready:Story={name:'읽기 허용 · 합성 Bot 보관함',args:{mode:'ready'}}
export const Denied:Story={name:'읽기 미허용 · Mac 안내',args:{mode:'denied'}}
export const Removed:Story={name:'원본 파일 삭제',args:{mode:'removed'}}
