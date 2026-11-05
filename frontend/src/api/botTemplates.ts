import api from '../lib/api'
import { validAvatar, type AvatarConfig } from '../lib/avatarRecipe'
import { validSchedule, sameSchedule, type RoutineSchedule } from './routines'

export type BotTemplate={version:1;name:string;description:string;system_prompt:string;avatar:AvatarConfig;settings:{temperature:string;max_tokens:number};skills:{name:string;description:string;content:string}[];routines:{name:string;instruction:string;schedule:RoutineSchedule;enabled:boolean}[]}
export type TemplateSelection={name:string;connection_id:number;model_id:string}
export type TemplatePlan=TemplateSelection&{version:1;template_sha256:string;plan_hash:string;skills_count:number;connection_enabled:boolean;routines:{name:string;schedule:RoutineSchedule;enabled:boolean;next_run_at:string|null;webhook_setup_required:boolean;availability:'paused'|'webhook_setup_required'|'connection_disabled'|'requires_runtime_checks'}[];app_bindings_copied:false}
export type TemplateReceipt=TemplateSelection&{version:1;template_sha256:string;plan_hash:string;agent_id:number;room_id:number;project_id:number;href:string;skill_count:number;routine_count:number;webhook_setup_required:boolean;app_bindings_copied:false;replayed:boolean}
const obj=(x:unknown):x is Record<string,unknown>=>!!x&&typeof x==='object'&&!Array.isArray(x)
const keys=(v:Record<string,unknown>,names:string[])=>Object.keys(v).sort().join(',')===[...names].sort().join(',')
const text=(v:unknown,max:number)=>typeof v==='string'&&!!v.trim()&&Array.from(v).length<=max
const positive=(v:unknown):v is number=>typeof v==='number'&&Number.isSafeInteger(v)&&v>0
const hash=(v:unknown):v is string=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v)
export function validBotTemplate(value:unknown):value is BotTemplate {
 if(!obj(value)||!keys(value,['version','name','description','system_prompt','avatar','settings','skills','routines'])||value.version!==1||!text(value.name,100)||!text(value.description,8000)||!text(value.system_prompt,16000)||!validAvatar(value.avatar)||!obj(value.settings)||!keys(value.settings,['temperature','max_tokens'])||typeof value.settings.temperature!=='string'||(typeof value.settings.max_tokens!=='number'||!Number.isSafeInteger(value.settings.max_tokens)||value.settings.max_tokens<0||value.settings.max_tokens>100000)||!Array.isArray(value.skills)||value.skills.length>8||!Array.isArray(value.routines)||value.routines.length>50)return false
 return value.skills.every(s=>obj(s)&&keys(s,['name','description','content'])&&text(s.name,120)&&typeof s.description==='string'&&Array.from(s.description).length<=500&&text(s.content,8000))&&value.routines.every(r=>obj(r)&&keys(r,['name','instruction','schedule','enabled'])&&text(r.name,120)&&text(r.instruction,4000)&&validSchedule(r.schedule)&&typeof r.enabled==='boolean')
}
export function validTemplatePlan(v:unknown,selection:TemplateSelection,template:BotTemplate):v is TemplatePlan {
 if(!obj(v)||v.version!==1||v.name!==selection.name||v.connection_id!==selection.connection_id||v.model_id!==selection.model_id||!hash(v.template_sha256)||!hash(v.plan_hash)||v.skills_count!==template.skills.length||typeof v.connection_enabled!=='boolean'||v.app_bindings_copied!==false||!Array.isArray(v.routines)||v.routines.length!==template.routines.length)return false
 return v.routines.every((r,i)=>obj(r)&&r.name===template.routines[i].name&&r.enabled===template.routines[i].enabled&&validSchedule(r.schedule)&&sameSchedule(r.schedule,template.routines[i].schedule)&&(r.next_run_at===null||typeof r.next_run_at==='string'&&Number.isFinite(Date.parse(r.next_run_at)))&&typeof r.webhook_setup_required==='boolean'&&r.webhook_setup_required===(r.schedule.kind==='webhook')&&typeof r.availability==='string'&&['paused','webhook_setup_required','connection_disabled','requires_runtime_checks'].includes(r.availability))
}
export function validTemplateReceipt(v:unknown,selection:TemplateSelection,templateHash:string,planHash:string):v is TemplateReceipt {
 if(!obj(v))return false
 return v.version===1&&v.name===selection.name&&v.connection_id===selection.connection_id&&v.model_id===selection.model_id&&v.template_sha256===templateHash&&hash(v.template_sha256)&&v.plan_hash===planHash&&hash(v.plan_hash)&&positive(v.agent_id)&&positive(v.room_id)&&positive(v.project_id)&&v.href===`/chat/${v.room_id}`&&typeof v.skill_count==='number'&&Number.isSafeInteger(v.skill_count)&&v.skill_count>=0&&typeof v.routine_count==='number'&&Number.isSafeInteger(v.routine_count)&&v.routine_count>=0&&typeof v.webhook_setup_required==='boolean'&&v.app_bindings_copied===false&&typeof v.replayed==='boolean'
}
export function templatePreviewUrl(value:string):string|null {
 if(typeof value!=='string'||value.length>2048||value!==value.trim()||/[\s\u0000-\u001f\u007f?#\\]/u.test(value))return null
 try {const url=new URL(value);if(url.protocol!=='https:'||url.username||url.password||url.search||url.hash||url.port&&url.port!=='443'||!/^\/t\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}(?:\/snapshot)?$/.test(url.pathname))return null;return `${url.origin}${url.pathname.replace(/\/snapshot$/i,'').toLowerCase()}`}catch{return null}
}
export function hasPendingTemplateImport(identity:string):boolean {
 try{const value=JSON.parse(sessionStorage.getItem(`bot-template-import:${identity}`)||'null');return !!value&&typeof value.key==='string'&&/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value.key)&&typeof value.selection?.name==='string'&&positive(value.selection?.connection_id)&&typeof value.selection?.model_id==='string'&&hash(value.templateHash)&&hash(value.planHash)&&Number.isSafeInteger(value.skillCount)&&value.skillCount>=0&&Number.isSafeInteger(value.routineCount)&&value.routineCount>=0&&typeof value.webhook==='boolean'}catch{return false}
}
export const botTemplatesApi={
 linkPreview:async(url:string,signal?:AbortSignal)=>{const expected=templatePreviewUrl(url);if(!expected)throw Error('Invalid public template URL');const {data}=await api.post<unknown>('/bot-templates/preview-link',{url},{signal});if(!obj(data)||data.version!==1||typeof data.source_url!=='string'||templatePreviewUrl(data.source_url)!==expected||data.source_url!==expected||!validBotTemplate(data.template)||!hash(data.template_sha256))throw Error('Invalid public template result');return {source_url:data.source_url,template:data.template,template_sha256:data.template_sha256}},
 exportPreview:async(source_id:number)=>{const {data}=await api.post<unknown>('/bot-templates/preview-export',{source_id});if(!obj(data)||data.version!==1||!validBotTemplate(data.template)||!hash(data.template_sha256))throw Error('Invalid template');return {template:data.template,template_sha256:data.template_sha256}},
 importPreview:async(template:BotTemplate,selection:TemplateSelection)=>{const {data}=await api.post<unknown>('/bot-templates/preview-import',{template,...selection});if(!validTemplatePlan(data,selection,template))throw Error('Invalid plan');return data},
 import:async(template:BotTemplate,selection:TemplateSelection,plan_hash:string,key:string)=>{const {data}=await api.post<unknown>('/bot-templates/import',{template,...selection,plan_hash},{headers:{'Idempotency-Key':key}});return data},
 receipt:async(key:string)=>{const {data}=await api.get<unknown>(`/bot-templates/import/commands/${encodeURIComponent(key)}`);return data},
}
