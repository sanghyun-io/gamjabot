export type AgentConnectionFilter={kind:'all'}|{kind:'one';id:number}|{kind:'invalid'}
export function readAgentConnectionFilter(search:string):AgentConnectionFilter{
 const values=new URLSearchParams(search).getAll('connection')
 if(!values.length||values.length===1&&values[0]==='')return {kind:'all'}
 if(values.length!==1||!/^\d+$/.test(values[0])||!Number.isSafeInteger(Number(values[0]))||Number(values[0])<1||String(Number(values[0]))!==values[0])return {kind:'invalid'}
 return {kind:'one',id:Number(values[0])}
}
export function agentConnectionHref(id:number){
 return Number.isSafeInteger(id)&&id>0?`/agents?connection=${id}`:'/agents?connection=invalid'
}
