import type {Routine} from '../api/routines'
import {useAuthStore} from '../stores/authStore'
type Handoff={identity:string;room:number;agent:number;routine:Routine;draft:string}
const handoffs=new Map<string,Handoff>()
useAuthStore.subscribe((now,old)=>{if(!now.token||now.epoch!==old.epoch||now.user?.id!==old.user?.id)handoffs.clear()})
export function saveRoutineHandoff(value:Handoff){const id=crypto.randomUUID();handoffs.set(id,value);return id}
export function readRoutineHandoff(id:unknown,identity:string,room:number,agent:number){if(typeof id!=='string')return null;const value=handoffs.get(id);return value&&value.identity===identity&&value.room===room&&value.agent===agent?value:null}
export function clearRoutineHandoff(id:string){handoffs.delete(id)}
