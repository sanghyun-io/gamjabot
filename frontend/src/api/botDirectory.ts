import api from '../lib/api'

export type BotSection = { id:string; name:string }
export type BotDirectoryItem = { agent_id:number; pinned:boolean; hidden:boolean; section_id:string|null }
export type BotDirectory = { version:1; revision:number; sections:BotSection[]; items:BotDirectoryItem[] }
export type BotDirectoryCommand = BotDirectory & { command_key:string; expected_revision:number; replayed:boolean }
export type BotDirectoryWrite = Pick<BotDirectory,'sections'|'items'> & { expected_revision:number }
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
export function validBotDirectory(value:unknown):value is BotDirectory {
 if (!value || typeof value!=='object') return false
 const v=value as BotDirectory
 if(v.version!==1 || !Number.isSafeInteger(v.revision) || v.revision<0 || !Array.isArray(v.sections) || !Array.isArray(v.items))return false
 if(v.sections.some(s=>!s || !uuid.test(s.id) || typeof s.name!=='string' || !s.name.trim()) || new Set(v.sections.map(s=>s.id)).size!==v.sections.length)return false
 const sectionIds=new Set(v.sections.map(s=>s.id))
 return !v.items.some(i=>!i || !Number.isSafeInteger(i.agent_id) || i.agent_id<1 || typeof i.pinned!=='boolean' || typeof i.hidden!=='boolean' || i.section_id!==null && !sectionIds.has(i.section_id)) && new Set(v.items.map(i=>i.agent_id)).size===v.items.length
}
export const botDirectoryApi={
 read:async()=>{const {data}=await api.get<BotDirectory>('/bot-directory');if(!validBotDirectory(data))throw Error('Invalid directory');return data},
 save:async(body:BotDirectoryWrite,key:string)=>{const {data}=await api.put<BotDirectoryCommand>('/bot-directory',body,{headers:{'Idempotency-Key':key}});return data},
 receipt:async(key:string)=>{const {data}=await api.get<BotDirectoryCommand>(`/bot-directory/commands/${encodeURIComponent(key)}`);return data},
}
export function matchesBotDirectoryCommand(receipt:BotDirectoryCommand,body:BotDirectoryWrite,key:string):boolean {
 return validBotDirectory(receipt) && receipt.command_key===key && receipt.expected_revision===body.expected_revision && receipt.revision===body.expected_revision+1 && JSON.stringify(receipt.sections)===JSON.stringify(body.sections) && JSON.stringify(receipt.items)===JSON.stringify(body.items)
}
