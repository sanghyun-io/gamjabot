import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { validSkill, type Skill } from './skills'
export type PackageManifest={package_id:string;package_version:string;author:string;license:string;name:string;description:string;content:string}
export type PackagePreview={version:1;manifest:PackageManifest;digest:string}
const object=(x:unknown):x is Record<string,unknown>=>!!x&&typeof x==='object'&&!Array.isArray(x)
const keys=['package_id','package_version','author','license','name','description','content'] as const
export function validManifest(x:unknown):x is PackageManifest {
 if(!object(x)||Object.keys(x).length!==keys.length||!keys.every(k=>typeof x[k]==='string'))return false
 const v=x as PackageManifest
 return /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(v.package_id)&&/^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$/.test(v.package_version)&&!!v.author.trim()&&v.author.length<=120&&!!v.license.trim()&&v.license.length<=120&&!!v.name.trim()&&v.name.length<=120&&v.description.length<=500&&!!v.content.trim()&&v.content.length<=8000
}
export function validPackagePreview(x:unknown):x is PackagePreview {return object(x)&&x.version===1&&validManifest(x.manifest)&&typeof x.digest==='string'&&/^[a-f0-9]{64}$/.test(x.digest)}
const same=(a:PackageManifest,b:PackageManifest)=>keys.every(k=>a[k]===b[k])
const owner=()=>{const s=useAuthStore.getState();if(!s.token||!s.user?.id)throw Error('Owner unavailable');return {id:s.user.id,epoch:s.epoch}}
const check=(o:ReturnType<typeof owner>)=>{const s=useAuthStore.getState();if(!s.token||s.epoch!==o.epoch||s.user?.id!==o.id)throw Error('Owner changed')}
export async function matchesPackageReceipt(item:Skill,preview:PackagePreview):Promise<boolean> {
 const p=item.package_provenance,m=preview.manifest
 if(!validSkill(item)||item.revision!==1||item.active||item.source!==null||item.package_modified!==false||!p||p.digest!==preview.digest||p.package_id!==m.package_id||p.package_version!==m.package_version||p.author!==m.author||p.license!==m.license||item.name!==m.name||item.description!==m.description||item.content!==m.content)return false
 const hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(m.content));return Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,'0')).join('')===p.original_content_digest
}
export const skillPackagesApi={
 catalog:async(q=''):Promise<PackagePreview[]>=>{const a=owner(),{data}=await api.get<unknown>('/skill-packages',{params:{q}});check(a);if(!object(data)||data.version!==1||!Array.isArray(data.items)||!data.items.every(validPackagePreview))throw Error('Packages unavailable');return data.items},
 preview:async(manifest:PackageManifest):Promise<PackagePreview>=>{const a=owner(),{data}=await api.post<unknown>('/skill-packages/preview',manifest);check(a);if(!validPackagePreview(data)||!same(data.manifest,manifest))throw Error('Preview unavailable');return data},
 install:async(preview:PackagePreview,key:string):Promise<Skill>=>{const a=owner(),{data}=await api.post<unknown>('/skill-packages/install',{manifest:preview.manifest,digest:preview.digest},{headers:{'Idempotency-Key':key}});check(a);if(!validSkill(data))throw Error('Install result unavailable');return data},
}
