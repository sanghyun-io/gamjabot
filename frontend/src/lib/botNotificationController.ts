import { botNotificationsApi, notificationNative, type NotificationItem } from '../api/botNotifications'
import { useAuthStore } from '../stores/authStore'

export const NOTIFICATION_PERMISSION_CHANGED='gamja-notification-permission-changed'
export function notificationHref(item:NotificationItem):string {
 const query=new URLSearchParams()
 if(item.seq)query.set('seq',String(item.seq))
 if(item.kind==='approval'&&item.action_id)query.set('action',item.action_id)
 else if(item.kind==='failure'&&item.event_id)query.set('failure',item.event_id)
 else if(item.event_id)query.set('event',item.event_id)
 return `/chat/${item.room_id}${query.size?`?${query}`:''}`
}
/** While GUI is running, one authenticated session claims each delivery at most once. */
export function startBotNotifications(ownerId:number,authEpoch:number,onClick:(item:NotificationItem,epoch:string)=>void,onProblem:()=>void,onUnresolvedClick:(id:string,epoch:string|null)=>void) {
 const seenClicks=new Set<string>()
 let alive=true,timer:ReturnType<typeof setTimeout>|null=null,session:string|null=null,epoch:string|null=null,running=false,stopped=false
 const current=()=>{const a=useAuthStore.getState();return alive&&!!a.token&&a.user?.id===ownerId&&a.epoch===authEpoch}
 const schedule=(ms=5000)=>{if(current()&&!stopped&&!timer)timer=setTimeout(()=>{timer=null;void tick()},ms)}
 const tick=async()=>{if(!current()||stopped)return;if(running){schedule(1000);return}running=true
  let taken:string|null=null,clickEpoch:string|null=null
  try {
   if(!notificationNative.available())return
   const permission=await notificationNative.status();if(!current())return
   const click=await notificationNative.take();if(!current())return
   if(click&&!seenClicks.has(click))taken=click
   if(permission!=='authorized'&&!click){session=null;epoch=null;schedule(30000);return}
   const context=await botNotificationsApi.status(ownerId);if(!current())return
   if(epoch!==context.owner_epoch)session=null;epoch=context.owner_epoch;clickEpoch=epoch
   if(taken){const selected=await botNotificationsApi.resolve(taken,epoch);if(!current())return;seenClicks.add(taken);onClick(selected,epoch);taken=null}
   if(permission!=='authorized'){session=null;schedule(30000);return}
   if(!session){const next=await botNotificationsApi.session(epoch);if(!current())return;session=next}
   if(!current()||!session||!epoch)return
   const activeSession=session,activeEpoch=epoch
   const candidate=await botNotificationsApi.next(activeSession,activeEpoch);if(!current()||session!==activeSession||epoch!==activeEpoch)return
   if(candidate){await botNotificationsApi.ready(candidate.delivery_id,activeSession,activeEpoch);if(!current()||session!==activeSession||epoch!==activeEpoch)return
    let outcome:'accepted'|'failed'|'unknown'='unknown';try{outcome=await notificationNative.deliver(candidate.delivery_id,candidate.kind)}catch{/* A lost result is not safe to repeat. */}
    if(!current()||session!==activeSession||epoch!==activeEpoch)return
    try{await botNotificationsApi.finish(candidate.delivery_id,activeEpoch,outcome)}catch{if(current())onProblem()}
    if(outcome==='unknown'&&current())onProblem()
   }
   schedule()
  }catch{if(current()){stopped=true;if(taken)onUnresolvedClick(taken,clickEpoch);onProblem()}}finally{running=false}
 }
 const permissionChanged=()=>{if(!current())return;session=null;epoch=null;stopped=false;if(timer){clearTimeout(timer);timer=null}void tick()}
 window.addEventListener(NOTIFICATION_PERMISSION_CHANGED,permissionChanged);void tick()
 return ()=>{alive=false;if(timer)clearTimeout(timer);window.removeEventListener(NOTIFICATION_PERMISSION_CHANGED,permissionChanged)}
}
