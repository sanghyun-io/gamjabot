"""Synthetic Page workbook exports; no external service or user files."""
import base64
from datetime import datetime
from hashlib import sha256
import io
import json
from types import SimpleNamespace
from uuid import uuid4
import zipfile
import pytest
from app.models.room_result import RoomResult,RoomResultVersion
from app.services.page_documents import normalize_document
from app.services import page_xlsx
from app.api.routes import page_xlsx as route
from test_room_results import results,headers


def document():
    return {'format':'gamja.page.v1','title':'합성 보고서','datasets':[
        {'id':'abcdefghijklmnopqrstuvwxzyabcdef_first','columns':[{'key':'name','label':'이름','type':'text'},
            {'key':'n','label':'숫자','type':'number'},{'key':'yes','label':'확인','type':'boolean'},
            {'key':'date','label':'날짜','type':'date'}],
         'rows':[{'name':'=HYPERLINK("https://invalid.example")','n':9007199254740991,'yes':True,'date':'2026-11-04'},
                 {'name':'감자 & <테스트>','n':42,'yes':False,'date':None}]},
        {'id':'abcdefghijklmnopqrstuvwxzyabcdef_second','columns':[{'key':'empty','label':'빈 열','type':'text'}],'rows':[]},
        {'id':'history','columns':[{'key':'value','label':'값','type':'text'}],'rows':[{'value':'+1+1'}]}],
        'views':[],'blocks':[{'type':'heading','level':1,'text':'보고서'}]}


def test_workbook_is_deterministic_inert_and_preserves_cells():
    _,raw,_=normalize_document(document())
    data,sheets,count,_=page_xlsx.workbook(raw);again,_,_,_=page_xlsx.workbook(raw)
    assert data==again and data[:4]==b'PK\x03\x04' and count==1
    assert sheets[0]['name']!=sheets[1]['name'] and all(len(s['name'])<=31 for s in sheets)
    assert sheets[0]['rows']==2 and sheets[1]['rows']==0 and sheets[2]['name']=='_history'
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        assert not any('externalLink' in n or 'vbaProject' in n for n in archive.namelist())
        xml=archive.read('xl/worksheets/sheet1.xml').decode()
        assert '<f' not in xml and 't="inlineStr"' in xml and '9007199254740991' in xml
        assert '=HYPERLINK' in xml and '감자 &amp; &lt;테스트&gt;' in xml



def test_rejects_empty_and_xml_control_characters():
    doc=document();doc['datasets']=[]
    _,raw,_=normalize_document(doc)
    with pytest.raises(page_xlsx.ExportError,match='page_xlsx_no_datasets'):page_xlsx.workbook(raw)
    doc=document();doc['datasets'][0]['rows'][0]['name']='bad\x01control'
    _,raw,_=normalize_document(doc)
    with pytest.raises(page_xlsx.ExportError,match='page_xlsx_unsupported_text'):page_xlsx.workbook(raw)


def test_api_binds_owner_epoch_and_exact_immutable_version(results,monkeypatch):
    client,ids=results;epoch='a'*64;core='b'*64
    def fake_identity(request,owner,expected=None):
        if expected is not None and expected!=epoch:
            from fastapi import HTTPException
            raise HTTPException(409,{'code':'page_xlsx_owner_changed'})
        return epoch,core
    monkeypatch.setattr(route,'identity',fake_identity)
    doc=document();doc['views']=[{'id':'v','dataset':doc['datasets'][0]['id'],'filters':[]}]
    doc['blocks'].append({'type':'chart','kind':'bar','view':'v','category':'name','aggregate':'count','title':'Chart'})
    _,raw,digest=normalize_document(doc);result_id=str(uuid4());stamp=datetime.utcnow()
    with client.app.state.session_factory() as db:
        db.add(RoomResult(result_id=result_id,owner_id=ids['owner'],room_id=ids['room'],current_version=1,created_at=stamp,updated_at=stamp));db.flush()
        db.add(RoomResultVersion(result_id=result_id,version=1,room_id=ids['room'],kind='page',title='Sample',source_event_id=ids['answer'],source_seq=2,selector='page:'+digest,author_agent_id=ids['author'],author_name_snapshot='Synthetic',execution={},content=raw,evidence=[],created_at=stamp));db.commit()
    path=f"/api/v1/conversations/{ids['room']}/results/{result_id}/versions/1/xlsx"
    response=client.get(path+'/preview',headers=headers(client,ids));assert response.status_code==200,response.text
    preview=response.json();assert preview['charts']==[{'title':'Chart','kind':'bar','aggregate':'count','source_sheet':preview['sheets'][0]['name'],'snapshot_sheet':'chart_1'}];assert preview['document_sha256']==sha256(raw.encode()).hexdigest() and preview['owner_id']==ids['owner']
    body={'consent':True,'expected_owner_epoch':epoch,'expected_document_sha256':preview['document_sha256'],'expected_sha256':preview['sha256']}
    saved=client.post(path,json=body,headers=headers(client,ids));assert saved.status_code==200,saved.text
    data=base64.b64decode(saved.json()['content_base64'],validate=True)
    assert sha256(data).hexdigest()==preview['sha256'] and len(data)==preview['size_bytes']
    with zipfile.ZipFile(io.BytesIO(data)) as archive:assert 'xl/charts/chart1.xml' in archive.namelist()
    assert client.post(path,json={**body,'consent':False},headers=headers(client,ids)).status_code==422
    assert client.post(path,json={**body,'expected_sha256':'f'*64},headers=headers(client,ids)).status_code==409
    assert client.post(path,json={**body,'expected_owner_epoch':'c'*64},headers=headers(client,ids)).status_code==409
    assert client.get(path+'/preview',headers=headers(client,ids,owner=ids['other'])).status_code==404
    changed=document();changed['datasets'][0]['rows'][0]['name']='CHANGED'
    _,new_raw,_=normalize_document(changed)
    with client.app.state.session_factory() as db:
        db.get(RoomResultVersion,(result_id,1)).content=new_raw;db.commit()
    assert client.post(path,json=body,headers=headers(client,ids)).status_code==409


def test_subnormal_numbers_are_literal_text_without_underflow():
    import xml.etree.ElementTree as ET
    values=[1e-320,-1e-320,2.22508e-308,2.2251e-308,-2.2251e-308,0.0]
    doc={'format':'gamja.page.v1','title':'Boundaries','datasets':[{'id':'sample','columns':[{'key':'n','label':'N','type':'number'}],'rows':[{'n':v} for v in values]}],'views':[],'blocks':[{'type':'heading','level':1,'text':'H'}]}
    _,raw,_=normalize_document(doc);data,_,count,_=page_xlsx.workbook(raw)
    assert count==3
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
    ns={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    cells={c.attrib['r']:c for c in root.findall('.//x:c',ns)}
    assert cells['A2'].attrib['t']=='inlineStr' and cells['A2'].find('.//x:t',ns).text=='1e-320'
    assert cells['A3'].attrib['t']=='inlineStr' and cells['A3'].find('.//x:t',ns).text=='-1e-320'
    assert cells['A4'].attrib['t']=='inlineStr' and cells['A4'].find('.//x:t',ns).text=='2.22508e-308'
    assert cells['A5'].attrib['t']=='n' and cells['A6'].attrib['t']=='n' and cells['A7'].attrib['t']=='n'

def chart_document():
    return {'format':'gamja.page.v1','title':'Charts','datasets':[{'id':'chart_1','columns':[
        {'key':'cat','label':'Category','type':'text'},{'key':'n','label':'Number','type':'number'}],
        'rows':[{'cat':'A','n':2},{'cat':'B','n':8},{'cat':'A','n':4},{'cat':'C','n':None}]}],
        'views':[{'id':'v','dataset':'chart_1','filters':[]}],
        'blocks':[{'type':'chart','view':'v','kind':kind,'category':'cat','aggregate':agg,'title':kind,**({'value':'n'} if agg!='count' else {})}
                  for kind,agg in [('bar','sum'),('line','avg'),('pie','count')]]}

def test_charts_are_editable_and_match_full_data_aggregation():
    import xml.etree.ElementTree as ET
    _,raw,_=normalize_document(chart_document());data,_,_,_=page_xlsx.workbook(raw)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        ns={'c':'http://schemas.openxmlformats.org/drawingml/2006/chart','x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        for i,expected in [(1,['6.0','8.0']),(2,['3.0','8.0']),(3,['2','1','1'])]:
            chart=ET.fromstring(archive.read(f'xl/charts/chart{i}.xml'))
            assert [v.text for v in chart.findall('.//c:numCache/c:pt/c:v',ns)]==expected
            assert f"'chart_{i}" in chart.find('.//c:numRef/c:f',ns).text
            assert f'xl/drawings/drawing{i}.xml' in archive.namelist()
        sheet=ET.fromstring(archive.read('xl/worksheets/sheet2.xml'))
        assert [t.text for t in sheet.findall('.//x:t',ns)]==['chart_1.cat','sum (chart_1.n) — snapshot','A','B']
        assert b'chart_1_2' in archive.read('xl/workbook.xml')

def test_unrepresentable_charts_fail_instead_of_disappearing():
    doc=chart_document();doc['datasets'][0]['rows']=[]
    _,raw,_=normalize_document(doc)
    with pytest.raises(page_xlsx.ExportError,match='page_xlsx_chart_unsupported'):page_xlsx.workbook(raw)
    doc=chart_document();doc['blocks']=[{'type':'chart','view':'v','kind':'pie','category':'cat','aggregate':'sum','value':'n','title':'pie'}]
    doc['datasets'][0]['rows'][0]['n']=-20
    _,raw,_=normalize_document(doc)
    with pytest.raises(page_xlsx.ExportError,match='page_xlsx_chart_unsupported'):page_xlsx.workbook(raw)

def test_chart_snapshot_uses_all_rows_and_renderer_number_labels():
    doc={'format':'gamja.page.v1','title':'Filters','datasets':[{'id':'d','columns':[
        {'key':'cat','label':'Category','type':'number'},{'key':'flag','label':'Flag','type':'boolean'}],
        'rows':[{'cat':1e-7,'flag':True},{'cat':1e-7,'flag':False},{'cat':0.000001,'flag':False}]}],
        'views':[{'id':'v','dataset':'d','filters':['f']}],
        'blocks':[{'type':'control','id':'f','view':'v','field':'flag','mode':'toggle','label':'Only true'},
                  {'type':'chart','kind':'bar','view':'v','category':'cat','aggregate':'count','title':'Count'}]}
    _,raw,_=normalize_document(doc);data,_,_,_=page_xlsx.workbook(raw)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        chart=archive.read('xl/charts/chart1.xml').decode()
        assert '<c:v>1e-7</c:v>' in chart and '<c:v>0.000001</c:v>' in chart
        assert '<c:pt idx="0"><c:v>2</c:v></c:pt>' in chart
