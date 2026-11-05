import { parsePageDocument, type PageBlock, type PageDataset, type PageScalar } from './pageDocument'

const MAX_BYTES=2*1024*1024
export type PageMarkdownResult={ok:true;text:string;bytes:number}|{ok:false;reason:'invalid'|'too_large'|'unsupported'}
const cell=(row:Record<string,PageScalar>,key:string):PageScalar=>Object.hasOwn(row,key)?row[key]??null:null
const display=(v:PageScalar|undefined)=>v==null?'—':typeof v==='boolean'?(v?'✓':'—'):String(v)
// Escape structural Markdown and HTML in data; authored Markdown blocks remain Markdown.
const safe=(v:string)=>v.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\/g,'\\\\').replace(/([`*_{}\[\]()#+.!|~-])/g,'\\$1').replace(/\r\n?|\n/g,'<br>')
function aggregate(values:number[],kind:'count'|'sum'|'avg'|'min'|'max',count:number):number|null {
 if(kind==='count')return count
 if(!values.length)return null
 const value=kind==='sum'||kind==='avg'?values.reduce((a,b)=>a+b,0):kind==='min'?Math.min(...values):Math.max(...values)
 const result=kind==='avg'?value/values.length:value
 if(!Number.isFinite(result))throw new Error('unsupported')
 return result
}
export function pageMarkdownExport(raw:string):PageMarkdownResult {
 const doc=parsePageDocument(raw);if(!doc)return {ok:false,reason:'invalid'}
 const encoder=new TextEncoder(),parts:string[]=[];let bytes=0
 const add=(s:string)=>{bytes+=encoder.encode(s).byteLength;if(bytes>MAX_BYTES)throw new Error('too_large');parts.push(s)}
 const datasets=new Map(doc.datasets.map(d=>[d.id,d])),views=new Map(doc.views.map(v=>[v.id,v]))
 const dataset=(view:string):PageDataset=>datasets.get(views.get(view)!.dataset)!
 const table=(head:string[],rows:string[][])=>{add(`| ${head.map(safe).join(' | ')} |\n| ${head.map(()=> '---').join(' | ')} |\n`);for(const row of rows)add(`| ${row.map(safe).join(' | ')} |\n`);add('\n')}
 const render=(block:PageBlock)=>{
  if(block.type==='heading'){add(`${'#'.repeat(block.level+1)} ${safe(block.text)}\n\n`);return}
  if(block.type==='markdown'){add(`${block.text}\n\n`);return}
  if(block.type==='columns'){block.items.forEach(render);return}
  if(block.type==='tabs'){for(const tab of block.tabs){add(`### ${safe(tab.label)}\n\n`);tab.blocks.forEach(render)}return}
  const d=dataset(block.view)
  if(block.type==='control'){add(`> ${safe(block.label)}: interactive filter not applied in this export.\n\n`);return}
  if(block.type==='metric'){const numbers=block.field?d.rows.map(row=>cell(row,block.field!)).filter((v):v is number=>typeof v==='number'):[];add(`**${safe(block.label)}:** ${safe(display(aggregate(numbers,block.aggregate,d.rows.length)))}\n\n`);return}
  if(block.type==='table'){add(`### ${safe(block.title)}\n\n`);table(block.columns.map(k=>d.columns.find(c=>c.key===k)!.label),d.rows.map(row=>block.columns.map(k=>display(cell(row,k)))));return}
  add(`### ${safe(block.title)}\n\n`)
  const groups=new Map<string,{label:string;values:number[];count:number}>()
  for(const row of d.rows){const category=cell(row,block.category),key=JSON.stringify(category),group=groups.get(key)||{label:display(category),values:[],count:0};group.count++;const value=block.value?cell(row,block.value):null;if(typeof value==='number')group.values.push(value);groups.set(key,group)}
  const rows=[...groups.values()].map(group=>[group.label,display(aggregate(group.values,block.aggregate,group.count))]).filter(row=>row[1]!=='—')
  table([d.columns.find(c=>c.key===block.category)!.label,`${block.aggregate}${block.value?` (${d.columns.find(c=>c.key===block.value)!.label})`:''}`],rows)
 }
 try {
  add(`# ${safe(doc.title)}\n\n> 정적 내보내기: 전체 데이터와 모든 탭이 포함됩니다. 화면의 필터는 적용되지 않으며 차트는 데이터 표로 표시됩니다. / Static export: all data and tabs are included. Interactive filters are not applied; charts are represented as data tables.\n\n`)
  doc.blocks.forEach(render)
  // Include every source dataset and column, including fields not shown by a block.
  if(doc.datasets.length)add('## Source data\n\n')
  for(const d of doc.datasets){add(`### ${safe(d.id)}\n\n`);table(d.columns.map(c=>`${c.label} (${c.key}: ${c.type})`),d.rows.map(row=>d.columns.map(c=>Object.hasOwn(row,c.key)?(JSON.stringify(row[c.key])??'null'):'(not set)')))}
  return {ok:true,text:parts.join(''),bytes}
 }catch(e){return {ok:false,reason:e instanceof Error&&e.message==='too_large'?'too_large':'unsupported'}}
}
