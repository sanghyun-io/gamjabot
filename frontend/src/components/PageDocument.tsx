import { useMemo, useState, type ReactNode } from 'react'
import MessageContent from './MessageContent'
import { useI18n } from '../i18n'
import { parsePageDocument, type PageBlock, type PageDocument, type PageScalar } from '../lib/pageDocument'

type Control=Extract<PageBlock,{type:'control'}>
type Values=Record<string,string|boolean>
const cell=(row:Record<string,PageScalar>,key:string):PageScalar=>Object.hasOwn(row,key)?row[key]??null:null
const display=(value:PageScalar|undefined)=>value==null?'—':typeof value==='boolean'?(value?'✓':'—'):String(value)
const aggregate=(values:number[],kind:'count'|'sum'|'avg'|'min'|'max',count:number)=>kind==='count'?count:!values.length?null:kind==='sum'?values.reduce((a,b)=>a+b,0):kind==='avg'?values.reduce((a,b)=>a+b,0)/values.length:kind==='min'?Math.min(...values):Math.max(...values)
function chartData(rows:Array<Record<string,PageScalar>>,block:Extract<PageBlock,{type:'chart'}>){
 const groups=new Map<string,{label:string;values:number[];count:number}>()
 for(const row of rows){const category=cell(row,block.category),key=JSON.stringify(category),old=groups.get(key)||{label:display(category),values:[],count:0};old.count++;const v=block.value?cell(row,block.value):null;if(typeof v==='number')old.values.push(v);groups.set(key,old)}
 return [...groups.values()].map(g=>({label:g.label,value:aggregate(g.values,block.aggregate,g.count)})).filter((v):v is {label:string;value:number}=>v.value!==null)
}
function Chart({data,kind,title}:{data:Array<{label:string;value:number}>;kind:'bar'|'line'|'pie';title:string}) {
 const {t}=useI18n()
 const width=600,height=220,pad=24,max=Math.max(1,...data.map(x=>Math.abs(x.value))),span=Math.max(1,data.length)
 const positive=data.every(x=>x.value>=0)&&data.some(x=>x.value>0),total=data.reduce((sum,x)=>sum+Math.max(0,x.value),0)
 let angle=-Math.PI/2
 const colors=['#2563eb','#0f766e','#c2410c','#7c3aed','#b45309','#be185d']
 return <div className="page-chart"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title}><title>{title}</title>
  {kind==='bar'&&data.map((x,i)=>{const bar=(x.value/max)*80;return <rect key={i} x={pad+i*(width-2*pad)/span} y={110-Math.max(0,bar)} width={Math.max(1,(width-2*pad)/span-4)} height={Math.max(1,Math.abs(bar))} fill={colors[i%colors.length]}/>})}
  {kind==='line'&&data.length>0&&<polyline fill="none" stroke="#2563eb" strokeWidth="3" points={data.map((x,i)=>`${pad+i*(width-2*pad)/Math.max(1,data.length-1)},${110-(x.value/max)*80}`).join(' ')}/>}
  {kind==='pie'&&positive&&data.map((x,i)=>{const sweep=2*Math.PI*x.value/total,start=angle;angle+=sweep;if(!sweep)return null;if(sweep>=Math.PI*2-0.000001)return <circle key={i} cx="300" cy="110" r="95" fill={colors[i%colors.length]}/>;const a=[300+95*Math.cos(start),110+95*Math.sin(start)],b=[300+95*Math.cos(angle),110+95*Math.sin(angle)];return <path key={i} fill={colors[i%colors.length]} d={`M 300 110 L ${a[0]} ${a[1]} A 95 95 0 ${sweep>Math.PI?1:0} 1 ${b[0]} ${b[1]} Z`}/>})}
 </svg>{kind==='pie'&&!positive&&data.length>0&&<p role="status">{t('pageView.pieUnavailable')}</p>}<ul>{data.map((x,i)=><li key={i}>{x.label} · {x.value}</li>)}</ul></div>
}
function Tabs({block,render}:{block:Extract<PageBlock,{type:'tabs'}>;render:(b:PageBlock,key:string)=>ReactNode}) {
 const [active,setActive]=useState(0)
 return <section className="stack stack--tight"><div className="inline-actions">{block.tabs.map((tab,i)=><button type="button" className="button button--secondary button--compact" aria-pressed={active===i} onClick={()=>setActive(i)} key={tab.label}>{tab.label}</button>)}</div><div className="stack stack--tight">{block.tabs[active].blocks.map((b,i)=>render(b,String(i)))}</div></section>
}
export type PageFeedbackSelection = { index: number; type: 'heading'|'markdown'; text: string }
function ReadyPage({doc,onSelect}:{doc:PageDocument;onSelect?:(selection:PageFeedbackSelection)=>void}) {
 const {t,locale}=useI18n(),[values,setValues]=useState<Values>({})
 const controls=useMemo(()=>{const result=new Map<string,Control>();const walk=(blocks:PageBlock[])=>blocks.forEach(b=>{if(b.type==='control')result.set(b.id,b);else if(b.type==='columns')walk(b.items);else if(b.type==='tabs')b.tabs.forEach(tab=>walk(tab.blocks))});walk(doc.blocks);return result},[doc])
 const dataset=(view:string)=>{const v=doc.views.find(x=>x.id===view)!;return doc.datasets.find(x=>x.id===v.dataset)!}
 const rows=(view:string)=>{const v=doc.views.find(x=>x.id===view)!,d=dataset(view);return d.rows.filter(row=>v.filters.every(id=>{const control=controls.get(id)!,selected=values[id];if(control.mode==='toggle')return selected!==true||cell(row,control.field)===true;if(control.mode==='search'){const needle=typeof selected==='string'?selected.trim().toLocaleLowerCase():'';return !needle||typeof cell(row,control.field)==='string'&&(cell(row,control.field) as string).toLocaleLowerCase().includes(needle)}return typeof selected!=='string'||selected===''||JSON.stringify(cell(row,control.field))===selected}))}
 const render=(b:PageBlock,key:string):ReactNode=>{
  if(b.type==='heading'){const Tag=(`h${b.level}`) as 'h1'|'h2'|'h3';return <Tag key={key}>{b.text}</Tag>}
  if(b.type==='markdown')return <MessageContent key={key} content={b.text}/>
  if(b.type==='columns')return <div key={key} className="page-document__columns">{b.items.map((child,i)=><div key={i} className="stack stack--tight">{render(child,String(i))}</div>)}</div>
  if(b.type==='tabs')return <Tabs key={key} block={b} render={render}/>
  const d=dataset(b.view), filtered=rows(b.view)
  if(b.type==='control'){const options=[...new Map(d.rows.map(row=>{const v=cell(row,b.field);return [JSON.stringify(v)!,v] as const})).entries()];return <label className="field" key={key}>{b.label}{b.mode==='search'?<input value={typeof values[b.id]==='string'?values[b.id] as string:''} onChange={e=>setValues(v=>({...v,[b.id]:e.target.value}))}/>:b.mode==='toggle'?<input type="checkbox" checked={values[b.id]===true} onChange={e=>setValues(v=>({...v,[b.id]:e.target.checked}))}/>:<select value={typeof values[b.id]==='string'?values[b.id] as string:''} onChange={e=>setValues(v=>({...v,[b.id]:e.target.value}))}><option value="">{t('pageView.all')}</option>{options.map(([id,value])=><option value={id} key={id}>{display(value)}</option>)}</select>}</label>}
  if(b.type==='metric'){const nums=b.field?filtered.map(r=>cell(r,b.field!)).filter((v):v is number=>typeof v==='number'):[];return <section key={key} className="page-document__metric"><span>{b.label}</span><strong>{display(aggregate(nums,b.aggregate,filtered.length))}</strong></section>}
  if(b.type==='table')return <section key={key} className="stack stack--tight"><h3>{b.title}</h3><div className="page-document__table"><table><thead><tr>{b.columns.map(k=><th key={k} scope="col">{d.columns.find(c=>c.key===k)!.label}</th>)}</tr></thead><tbody>{filtered.map((row,i)=><tr key={i}>{b.columns.map(k=><td key={k}>{display(cell(row,k))}</td>)}</tr>)}</tbody></table></div></section>
  return <section key={key} className="stack stack--tight"><h3>{b.title}</h3><Chart data={chartData(filtered,b)} kind={b.kind} title={b.title}/></section>
 }
 return <div className="page-document stack"><p className="metadata">{t('pageView.localOnly')}</p>{doc.blocks.map((b,i)=><div key={i}>{render(b,String(i))}{onSelect&&(b.type==='heading'||b.type==='markdown')&&b.text.length<=1500&&<button type="button" className="text-link" onClick={()=>onSelect({index:i,type:b.type,text:b.text})}>{locale==='ko'?'이 부분에 의견 남기기':'Give feedback on this section'}</button>}</div>)}</div>
}
export default function PageDocumentView({content,onSelect}:{content:string;onSelect?:(selection:PageFeedbackSelection)=>void}) { const {t}=useI18n(),doc=useMemo(()=>parsePageDocument(content),[content]);return doc?<ReadyPage doc={doc} onSelect={onSelect}/>:<p role="alert">{t('pageView.invalid')}</p> }
