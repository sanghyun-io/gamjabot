export type PageScalar = string | number | boolean | null
export type PageColumn = { key:string; label:string; type:'text'|'number'|'boolean'|'date' }
export type PageDataset = { id:string; columns:PageColumn[]; rows:Array<Record<string,PageScalar>> }
export type PageView = { id:string; dataset:string; filters:string[] }
export type PageBlock =
 | {type:'heading';level:1|2|3;text:string} | {type:'markdown';text:string}
 | {type:'metric';view:string;aggregate:'count'|'sum'|'avg'|'min'|'max';label:string;field?:string}
 | {type:'table';view:string;columns:string[];title:string}
 | {type:'chart';view:string;kind:'bar'|'line'|'pie';category:string;aggregate:'count'|'sum'|'avg';title:string;value?:string}
 | {type:'control';id:string;view:string;field:string;mode:'search'|'select'|'toggle';label:string}
 | {type:'columns';items:PageBlock[]} | {type:'tabs';tabs:Array<{label:string;blocks:PageBlock[]}>}
export type PageDocument = {format:'gamja.page.v1';title:string;datasets:PageDataset[];views:PageView[];blocks:PageBlock[]}
const obj=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==='object'&&!Array.isArray(v)
const id=(v:unknown):v is string=>typeof v==='string'&&/^[a-z][a-z0-9_]{0,63}$/.test(v)
const text=(v:unknown,max:number):v is string=>typeof v==='string'&&v.trim().length>0&&Array.from(v).length<=max&&!v.includes('\0')
const shape=(v:Record<string,unknown>,required:string[],optional:string[]=[])=>required.every(k=>Object.hasOwn(v,k))&&Object.keys(v).every(k=>required.includes(k)||optional.includes(k))
const unique=(items:string[])=>new Set(items.map(x=>x.toLowerCase())).size===items.length
const date=(v:string)=>{if(!/^\d{4}-\d{2}-\d{2}$/.test(v)||v.startsWith('0000'))return false;const d=new Date(`${v}T00:00:00Z`);return !Number.isNaN(d.getTime())&&d.toISOString().slice(0,10)===v}
export function parsePageDocument(raw:string):PageDocument|null {
 try {
  if(typeof raw!=='string'||new TextEncoder().encode(raw).length>131072)return null
  const value:unknown=JSON.parse(raw);if(!obj(value)||!shape(value,['format','title','datasets','views','blocks'])||value.format!=='gamja.page.v1'||!text(value.title,200)||!Array.isArray(value.datasets)||value.datasets.length>12||!Array.isArray(value.views)||value.views.length>32||!Array.isArray(value.blocks)||!value.blocks.length||value.blocks.length>256)return null
  const datasets=new Map<string,Map<string,PageColumn>>(),rowCounts=new Map<string,number>();let rows=0
  for(const d of value.datasets){if(!obj(d)||!shape(d,['id','columns','rows'])||!id(d.id)||datasets.has(d.id)||!Array.isArray(d.columns)||!d.columns.length||d.columns.length>40||!Array.isArray(d.rows)||(rows+=d.rows.length)>2000)return null
   const columns=new Map<string,PageColumn>();for(const c of d.columns){if(!obj(c)||!shape(c,['key','label','type'])||!id(c.key)||columns.has(c.key)||!text(c.label,120)||!['text','number','boolean','date'].includes(String(c.type)))return null;columns.set(c.key,c as unknown as PageColumn)}
   if(!unique([...columns.values()].map(c=>c.label)))return null
   for(const row of d.rows){if(!obj(row)||Object.keys(row).some(k=>!columns.has(k)))return null;for(const [k,v] of Object.entries(row)){if(v===null)continue;const type=columns.get(k)!.type;if(type==='text'&&(typeof v!=='string'||Array.from(v).length>4000||v.includes('\0'))||type==='date'&&(typeof v!=='string'||!date(v))||type==='boolean'&&typeof v!=='boolean'||type==='number'&&(typeof v!=='number'||!Number.isFinite(v)||Math.abs(v)>Number.MAX_SAFE_INTEGER))return null}}
   datasets.set(d.id,columns);rowCounts.set(d.id,d.rows.length)
  }
  const views=new Map<string,{columns:Map<string,PageColumn>;filters:string[];rowCount:number}>()
  for(const v of value.views){if(!obj(v)||!shape(v,['id','dataset','filters'])||!id(v.id)||views.has(v.id)||typeof v.dataset!=='string'||!datasets.has(v.dataset)||!Array.isArray(v.filters)||v.filters.length>32||!v.filters.every(id)||!unique(v.filters))return null;views.set(v.id,{columns:datasets.get(v.dataset)!,filters:v.filters,rowCount:rowCounts.get(v.dataset)!})}
  const controls=new Map<string,string>();let count=0,renderUnits=0
  const visit=(b:unknown,depth:number):boolean=>{if(++count>256||++renderUnits>50000||depth>6||!obj(b)||typeof b.type!=='string')return false
   if(b.type==='heading')return shape(b,['type','level','text'])&&[1,2,3].includes(b.level as number)&&text(b.text,500)
   if(b.type==='markdown')return shape(b,['type','text'])&&text(b.text,16000)
   if(b.type==='columns')return shape(b,['type','items'])&&Array.isArray(b.items)&&b.items.length>0&&b.items.length<=4&&b.items.every(c=>visit(c,depth+1))
   if(b.type==='tabs'){if(!shape(b,['type','tabs'])||!Array.isArray(b.tabs)||!b.tabs.length||b.tabs.length>12)return false;const names:string[]=[];for(const tab of b.tabs){if(!obj(tab)||!shape(tab,['label','blocks'])||!text(tab.label,120)||!Array.isArray(tab.blocks)||!tab.blocks.length||!tab.blocks.every(c=>visit(c,depth+1)))return false;names.push(tab.label)}return unique(names)}
   if(!['control','metric','table','chart'].includes(b.type)||typeof b.view!=='string'||!views.has(b.view))return false;const view=views.get(b.view)!,columns=view.columns;renderUnits+=b.type==='control'&&b.mode==='select'?view.rowCount+1:b.type==='table'&&Array.isArray(b.columns)?(view.rowCount+1)*b.columns.length:b.type==='chart'?view.rowCount:0;if(renderUnits>50000)return false
   if(b.type==='control'){if(!shape(b,['type','id','view','field','mode','label'])||!id(b.id)||controls.has(b.id)||!text(b.label,120)||typeof b.field!=='string'||!columns.has(b.field)||!['search','select','toggle'].includes(String(b.mode))||b.mode==='search'&&columns.get(b.field)!.type!=='text'||b.mode==='toggle'&&columns.get(b.field)!.type!=='boolean')return false;controls.set(b.id,b.view);return true}
   if(b.type==='table')return shape(b,['type','view','columns','title'])&&text(b.title,200)&&Array.isArray(b.columns)&&b.columns.length>0&&b.columns.length<=40&&b.columns.every(c=>typeof c==='string'&&columns.has(c))&&unique(b.columns)
   const chart=b.type==='chart';if(!shape(b,chart?['type','view','kind','category','aggregate','title']:['type','view','aggregate','label'],chart?['value']:['field'])||!text(chart?b.title:b.label,200))return false
   if(chart&&(!['bar','line','pie'].includes(String(b.kind))||typeof b.category!=='string'||!columns.has(b.category)))return false
   if(!((chart?['count','sum','avg']:['count','sum','avg','min','max']).includes(String(b.aggregate))))return false
   const key=chart?'value':'field';return b.aggregate==='count'?!Object.hasOwn(b,key):typeof b[key]==='string'&&columns.get(b[key] as string)?.type==='number'
  }
  if(!value.blocks.every(b=>visit(b,1)))return null
  const assigned=new Set<string>();for(const [view,v] of views)for(const filter of v.filters){if(assigned.has(filter)||controls.get(filter)!==view)return null;assigned.add(filter)}
  if(assigned.size!==controls.size)return null
  return value as unknown as PageDocument
 }catch{return null}
}
