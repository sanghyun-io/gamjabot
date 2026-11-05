import api from '../lib/api'
import { canonicalEventId } from '../lib/peerReply'
import type { ResultSummary } from './roomResults'

export interface BotLibraryItem extends ResultSummary { updated_at:string; source_href:string; detail_href:string }
export interface BotLibraryPage { agent_id:number; items:BotLibraryItem[]; next_cursor:string|null }
const positive=(n:unknown):n is number=>typeof n==='number'&&Number.isSafeInteger(n)&&n>0
export function librarySourceHref(item:BotLibraryItem):string|null {
 const match=/^\/chat\/([1-9]\d*)\?([^#\s]+)$/.exec(item.source_href)
 if(!match||Number(match[1])!==item.room_id)return null
 const q=new URLSearchParams(match[2]),seq=q.get('seq'),id=q.get('event')
 if([...q.keys()].some(k=>k!=='seq'&&k!=='event')||q.getAll('seq').length!==1||q.getAll('event').length!==1||!seq||!/^[1-9]\d*$/.test(seq)||!positive(Number(seq))||!canonicalEventId(id))return null
 return `/chat/${item.room_id}?${new URLSearchParams({seq,event:id!})}`
}
export function validLibraryPage(value:unknown,agent:number,query=''):value is BotLibraryPage {
 if(!value||typeof value!=='object'||Array.isArray(value))return false
 const page=value as BotLibraryPage
 const cursorId=(cursor:string)=>query?/^([0-9a-f-]{36})~[0-9a-f]{64}$/.exec(cursor)?.[1]:cursor
 if(page.agent_id!==agent||!Array.isArray(page.items)||page.items.length>50||!(page.next_cursor===null||typeof page.next_cursor==='string'&&canonicalEventId(cursorId(page.next_cursor))))return false
 const seen=new Set<string>()
 for(const item of page.items){
  if(!item||!canonicalEventId(item.result_id)||seen.has(item.result_id)||!positive(item.room_id)||!positive(item.version)||item.author_agent_id!==agent||!['text','link','file','page'].includes(item.kind)||!['draft','reviewing','final'].includes(item.state)||!positive(item.state_revision)||typeof item.title!=='string'||typeof item.author_name_snapshot!=='string'||typeof item.created_at!=='string'||typeof item.updated_at!=='string'||!Number.isFinite(Date.parse(item.updated_at))||!Number.isSafeInteger(item.linked_opinion_count)||item.linked_opinion_count<0||typeof item.source_href!=='string'||!librarySourceHref(item)||item.detail_href!==`/conversations/${item.room_id}/results/${item.result_id}/versions/${item.version}`)return false
  if(seen.size && item.result_id<=[...seen].at(-1)!)return false
  seen.add(item.result_id)
 }
 return page.next_cursor===null||page.items.length>0&&cursorId(page.next_cursor)===page.items.at(-1)!.result_id
}
export const botLibraryApi={
 list:async(agent:number,after?:string,query=''):Promise<BotLibraryPage>=>{const {data}=await api.get<BotLibraryPage>(`/bots/${agent}/library`,{params:{after,limit:50,...(query?{q:query}:{})}});const afterId=query?after?.split('~')[0]:after;if(!validLibraryPage(data,agent,query)||afterId&&data.items.some(item=>item.result_id<=afterId))throw Error('Library response mismatch');return data},
}
