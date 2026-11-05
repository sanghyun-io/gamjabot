"""Deterministic, inert XLSX export of one already validated Page version."""
import base64
from decimal import Decimal
from hashlib import sha256
import io
import math
import threading
from xml.sax.saxutils import escape,quoteattr
import zipfile
from fastapi import HTTPException
from app.models.models import User
from app.services import room_results
from app.services.page_documents import parse_document,PageDocumentError

MIME='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
MAX_BYTES=5*1024*1024
MAX_XML=8*1024*1024
MAX_CELLS=80000
# Excel's documented minimum positive calculation value, not IEEE's smaller normal.
EXCEL_MIN_POSITIVE=2.2251e-308
_slot=threading.BoundedSemaphore(1)

class ExportError(ValueError):pass

def _xml(value):
    if not isinstance(value,str) or any(not (ord(c) in (9,10,13) or 0x20<=ord(c)<=0xd7ff or 0xe000<=ord(c)<=0xfffd or 0x10000<=ord(c)<=0x10ffff) for c in value):
        raise ExportError('page_xlsx_unsupported_text')
    return escape(value,{'\r':'&#13;'})

def _column(index):
    value='';index+=1
    while index:index,remainder=divmod(index-1,26);value=chr(65+remainder)+value
    return value

def _sheet_names(datasets):
    used=set();names=[]
    for dataset in datasets:
        source=dataset['id'];base=('_'+source if source.casefold()=='history' else source)[:31];name=base;n=2
        while name.casefold() in used:
            suffix='_'+str(n);name=base[:31-len(suffix)]+suffix;n+=1
        used.add(name.casefold());names.append(name)
    return names

def _number_as_text(value):
    if isinstance(value,int):return len(str(abs(value)))>15
    if value==0 and math.copysign(1,value)<0:return True
    if 0 < abs(value) < EXCEL_MIN_POSITIVE:return True
    return len(Decimal(str(value)).as_tuple().digits)>15

def _inline(ref,value):return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_xml(value)}</t></is></c>'

def _sheet(dataset):
    parts=['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    cols=dataset['columns'];parts.append('<row r="1">')
    for i,column in enumerate(cols):parts.append(_inline(_column(i)+'1',column['label']))
    parts.append('</row>');stringified=0
    for row_index,row in enumerate(dataset['rows'],2):
        parts.append(f'<row r="{row_index}">')
        for col_index,column in enumerate(cols):
            value=row.get(column['key']);ref=_column(col_index)+str(row_index)
            if value is None:continue
            if type(value) is bool:parts.append(f'<c r="{ref}" t="b"><v>{int(value)}</v></c>')
            elif type(value) in (int,float):
                if _number_as_text(value):parts.append(_inline(ref,str(value)));stringified+=1
                else:parts.append(f'<c r="{ref}" t="n"><v>{value}</v></c>')
            else:parts.append(_inline(ref,value))
        parts.append('</row>')
    parts.append('</sheetData></worksheet>')
    value=''.join(parts).encode('utf-8')
    if len(value)>MAX_XML:raise ExportError('page_xlsx_too_large')
    return value,stringified

def _charts(blocks):
    for block in blocks:
        if block['type']=='chart':yield block
        elif block['type']=='columns':yield from _charts(block['items'])
        elif block['type']=='tabs':
            for tab in block['tabs']:yield from _charts(tab['blocks'])

def _js_number_label(value):
    # Match JavaScript String(number) for the renderer's bounded Page numbers.
    if value==0:return '0'
    if value.is_integer():return str(int(value))
    raw=repr(value).lower()
    if 1e-6<=abs(value)<1e21:return format(Decimal(raw),'f')
    if 'e' not in raw:return raw
    mantissa,exponent=raw.split('e');return mantissa+'e'+('+' if int(exponent)>=0 else '')+str(int(exponent))

def _chart_data(doc,block):
    view=next(v for v in doc['views'] if v['id']==block['view'])
    dataset=next(d for d in doc['datasets'] if d['id']==view['dataset'])
    groups={}
    for row in dataset['rows']:
        category=row.get(block['category'])
        # JS JSON.stringify uses a single numeric type and equates -0 with 0.
        if type(category) in (int,float):key=('number',float(category))
        else:key=(type(category).__name__,category)
        if key not in groups:
            if category is None:label='—'
            elif type(category) is bool:label='✓' if category else '—'
            elif type(category) in (int,float):label=_js_number_label(float(category))
            else:label=str(category)
            groups[key]=[label,0,0.0,0]
        group=groups[key];group[1]+=1
        value=row.get(block.get('value'))
        if type(value) in (int,float):group[2]+=float(value);group[3]+=1
    result=[]
    for label,count,total,n in groups.values():
        value=count if block['aggregate']=='count' else (total if block['aggregate']=='sum' else total/n) if n else None
        if value is None:continue
        if not math.isfinite(value) or _number_as_text(value):raise ExportError('page_xlsx_chart_unsupported')
        result.append((label,value))
    if not result or block['kind']=='pie' and (any(v<0 for _,v in result) or not any(v>0 for _,v in result)):
        raise ExportError('page_xlsx_chart_unsupported')
    return result

def _chart_sheet(data,source,block):
    rows=['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheetData>',
        '<row r="1">'+_inline('A1',source+'.'+block['category'])+_inline('B1',block['aggregate']+' ('+source+('.'+block['value'] if 'value' in block else '')+') — snapshot')+'</row>']
    for index,(label,value) in enumerate(data,2):
        rows.append(f'<row r="{index}">'+_inline('A'+str(index),label)+f'<c r="B{index}" t="n"><v>{value}</v></c></row>')
    rows.append('</sheetData><drawing r:id="rId1"/></worksheet>')
    return ''.join(rows).encode('utf-8')

def _chart_xml(sheet,block,data):
    last=len(data)+1;name=sheet.replace("'","''")
    cat=f"'{name}'!$A$2:$A${last}";val=f"'{name}'!$B$2:$B${last}"
    cat_cache=''.join(f'<c:pt idx="{i}"><c:v>{_xml(label)}</c:v></c:pt>' for i,(label,_) in enumerate(data))
    val_cache=''.join(f'<c:pt idx="{i}"><c:v>{value}</c:v></c:pt>' for i,(_,value) in enumerate(data))
    series=(f'<c:ser><c:idx val="0"/><c:order val="0"/><c:cat><c:strRef><c:f>{_xml(cat)}</c:f><c:strCache><c:ptCount val="{len(data)}"/>{cat_cache}</c:strCache></c:strRef></c:cat>'
        f'<c:val><c:numRef><c:f>{_xml(val)}</c:f><c:numCache><c:formatCode>General</c:formatCode><c:ptCount val="{len(data)}"/>{val_cache}</c:numCache></c:numRef></c:val></c:ser>')
    kind=block['kind'];tag={'bar':'barChart','line':'lineChart','pie':'pieChart'}[kind]
    options='<c:barDir val="col"/><c:grouping val="clustered"/>' if kind=='bar' else '<c:grouping val="standard"/>' if kind=='line' else '<c:varyColors val="1"/>'
    axes='' if kind=='pie' else '<c:axId val="100001"/><c:axId val="100002"/>'
    axis_defs='' if kind=='pie' else ('<c:catAx><c:axId val="100001"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:axPos val="b"/><c:crossAx val="100002"/><c:crosses val="autoZero"/></c:catAx>'
        '<c:valAx><c:axId val="100002"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:axPos val="l"/><c:crossAx val="100001"/><c:crosses val="autoZero"/></c:valAx>')
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><c:chart>'
        f'<c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{_xml(block["title"])}</a:t></a:r></a:p></c:rich></c:tx></c:title>'
        f'<c:plotArea><c:layout/><c:{tag}>{options}{series}{axes}</c:{tag}>{axis_defs}</c:plotArea><c:plotVisOnly val="1"/></c:chart></c:chartSpace>').encode('utf-8')

def _add_chart(parts,index,sheet_index,sheet,source,block,data):
    ns='http://schemas.openxmlformats.org/package/2006/relationships'
    office='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    parts[f'xl/worksheets/sheet{sheet_index}.xml']=_chart_sheet(data,source,block)
    parts[f'xl/worksheets/_rels/sheet{sheet_index}.xml.rels']=(f'<Relationships xmlns="{ns}"><Relationship Id="rId1" Type="{office}/drawing" Target="../drawings/drawing{index}.xml"/></Relationships>').encode()
    parts[f'xl/drawings/drawing{index}.xml']=(f'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:r="{office}"><xdr:twoCellAnchor><xdr:from><xdr:col>3</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>13</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>17</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to><xdr:graphicFrame macro=""><xdr:nvGraphicFramePr><xdr:cNvPr id="{index}" name="Chart {index}"/><xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr><xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart r:id="rId1"/></a:graphicData></a:graphic></xdr:graphicFrame><xdr:clientData/></xdr:twoCellAnchor></xdr:wsDr>').encode()
    parts[f'xl/drawings/_rels/drawing{index}.xml.rels']=(f'<Relationships xmlns="{ns}"><Relationship Id="rId1" Type="{office}/chart" Target="../charts/chart{index}.xml"/></Relationships>').encode()
    parts[f'xl/charts/chart{index}.xml']=_chart_xml(sheet,block,data)

def workbook(raw):
    try:doc,_,_=parse_document(raw)
    except PageDocumentError:raise ExportError('page_xlsx_document_invalid') from None
    datasets=doc['datasets']
    if not datasets:raise ExportError('page_xlsx_no_datasets')
    if sum((len(d['rows'])+1)*len(d['columns']) for d in datasets)>MAX_CELLS:raise ExportError('page_xlsx_too_large')
    names=_sheet_names(datasets);sheets=[];parts={};text_numbers=0;xml_total=0
    for index,(dataset,name) in enumerate(zip(datasets,names),1):
        content,count=_sheet(dataset);xml_total+=len(content)
        if xml_total>MAX_XML:raise ExportError('page_xlsx_too_large')
        parts[f'xl/worksheets/sheet{index}.xml']=content;text_numbers+=count
        sheets.append({'dataset_id':dataset['id'],'name':name,'rows':len(dataset['rows']),'columns':len(dataset['columns'])})
    charts=list(_charts(doc['blocks']));chart_details=[]
    if len(charts)>32:raise ExportError('page_xlsx_chart_unsupported')
    used={name.casefold() for name in names};chart_cells=0
    for index,block in enumerate(charts,1):
        name=f'chart_{index}';suffix=2
        while name.casefold() in used:name=f'chart_{index}_{suffix}';suffix+=1
        used.add(name.casefold());names.append(name)
        data=_chart_data(doc,block);chart_cells+=2*(len(data)+1)
        if chart_cells+sum((len(d['rows'])+1)*len(d['columns']) for d in datasets)>MAX_CELLS:raise ExportError('page_xlsx_too_large')
        view=next(v for v in doc['views'] if v['id']==block['view'])
        source=names[next(i for i,d in enumerate(datasets) if d['id']==view['dataset'])]
        _add_chart(parts,index,len(names),name,source,block,data)
        chart_details.append({'title':block['title'],'kind':block['kind'],'aggregate':block['aggregate'],'source_sheet':source,'snapshot_sheet':name})
    if sum(len(value) for value in parts.values())>MAX_XML:raise ExportError('page_xlsx_too_large')
    content_types=''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(names)+1))
    content_types+=''.join(f'<Override PartName="/xl/drawings/drawing{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/><Override PartName="/xl/charts/chart{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>' for i in range(1,len(charts)+1))
    sheet_tags=''.join(f'<sheet name={quoteattr(name)} sheetId="{i}" r:id="rId{i}"/>' for i,name in enumerate(names,1))
    rels=''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(names)+1))
    parts['[Content_Types].xml']=(f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>{content_types}</Types>').encode()
    parts['_rels/.rels']=b'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    parts['xl/workbook.xml']=(f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{sheet_tags}</sheets></workbook>').encode()
    parts['xl/_rels/workbook.xml.rels']=(f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>').encode()
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for name in sorted(parts):
            info=zipfile.ZipInfo(name,(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.create_system=3;info.external_attr=0o600<<16
            archive.writestr(info,parts[name],compress_type=zipfile.ZIP_DEFLATED,compresslevel=6)
    data=output.getvalue()
    if len(data)>MAX_BYTES:raise ExportError('page_xlsx_too_large')
    return data,sheets,text_numbers,chart_details

def prepare(db,owner,room,result_id,version,epoch):
    room_results.require_room(db,room,owner,active=False)
    user=db.get(User,owner,populate_existing=True)
    if not user or not user.is_active:raise HTTPException(404,{'code':'page_xlsx_unavailable'})
    _,row=room_results.version_row(db,owner,room,result_id,version)
    if row.kind!='page':raise HTTPException(409,{'code':'page_xlsx_not_page'})
    if not _slot.acquire(blocking=False):raise HTTPException(503,{'code':'page_xlsx_busy'})
    try:
        try:data,sheets,text_numbers,charts=workbook(row.content)
        except ExportError as error:raise HTTPException(422,{'code':str(error)}) from None
    finally:_slot.release()
    metadata={'version':1,'owner_id':owner,'owner_epoch':epoch,'room_id':room,'result_id':row.result_id,'page_version':row.version,
        'document_sha256':sha256(row.content.encode('utf-8')).hexdigest(),'sha256':sha256(data).hexdigest(),
        'size_bytes':len(data),'mime':MIME,'sheets':sheets,'stringified_numbers':text_numbers,'charts':charts}
    return metadata,data

def require_current(db,owner,room,result_id,version,expected_digest):
    db.rollback();db.expire_all()
    room_results.require_room(db,room,owner,active=False)
    user=db.get(User,owner,populate_existing=True)
    if not user or not user.is_active:raise HTTPException(404,{'code':'page_xlsx_unavailable'})
    _,row=room_results.version_row(db,owner,room,result_id,version)
    if row.kind!='page' or sha256(row.content.encode('utf-8')).hexdigest()!=expected_digest:
        raise HTTPException(409,{'code':'page_xlsx_changed'})

def encoded(data):return base64.b64encode(data).decode('ascii')
