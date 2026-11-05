import api from '../lib/api'
export type ImageCheckPreview={version:1;model_id:string;max_generation_requests:number;max_catalog_requests:number;max_metered_model_http_requests:number;config_revision:number;auth_grant_revision:number}
export type ImageCheckReceipt=ImageCheckPreview & {status:'pending'|'verified'|'failed'|'unknown';actual_generation_requests:number|null;actual_catalog_requests:number|null;actual_metered_model_http_requests:number|null;error_code?:unknown;phase?:unknown}
const path=(connection:number)=>`/connections/${connection}/features/image-input`
export const imageInputVerificationApi={
 preview:async(connection:number,model:string,signal?:AbortSignal)=>(await api.get<ImageCheckPreview>(`${path(connection)}/preview`,{params:{model_id:model},signal})).data,
 check:async(connection:number,preview:ImageCheckPreview,key:string,signal?:AbortSignal)=>(await api.post<ImageCheckReceipt>(`${path(connection)}/check`,{model_id:preview.model_id,config_revision:preview.config_revision,auth_grant_revision:preview.auth_grant_revision},{headers:{'Idempotency-Key':key},signal})).data,
 command:async(connection:number,key:string,signal?:AbortSignal)=>(await api.get<ImageCheckReceipt>(`${path(connection)}/commands/${encodeURIComponent(key)}`,{signal})).data,
}
