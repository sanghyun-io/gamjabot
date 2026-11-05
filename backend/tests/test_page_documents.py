import copy
import hashlib
import json

import pytest
from app.services.page_documents import PageDocumentError, normalize_document, parse_document


def page():
    return {'format':'gamja.page.v1','title':'Sales dashboard',
        'datasets':[{'id':'sales','columns':[{'key':'name','label':'Name','type':'text'},
            {'key':'total','label':'Total','type':'number'},{'key':'active','label':'Active','type':'boolean'},
            {'key':'date','label':'Date','type':'date'}],
            'rows':[{'name':'<script>ignored()</script>','total':42,'active':True,'date':'2026-10-31'},
                    {'name':'Other','total':None,'active':False}]}],
        'views':[{'id':'main','dataset':'sales','filters':['query','active_only']}],
        'blocks':[{'type':'heading','level':1,'text':'Overview'},
            {'type':'markdown','text':'<img src=x onerror=evil()> is text'},
            {'type':'columns','items':[{'type':'metric','view':'main','aggregate':'count','label':'Count'},
                                      {'type':'metric','view':'main','aggregate':'sum','field':'total','label':'Total'}]},
            {'type':'control','id':'query','view':'main','field':'name','mode':'search','label':'Find'},
            {'type':'control','id':'active_only','view':'main','field':'active','mode':'toggle','label':'Active'},
            {'type':'tabs','tabs':[{'label':'Table','blocks':[{'type':'table','view':'main','columns':['name','total'],'title':'Sales'}]},
                {'label':'Chart','blocks':[{'type':'chart','kind':'bar','view':'main','category':'name','aggregate':'sum','value':'total','title':'Totals'}]}]}]}


def test_page_preserves_data_and_canonical_hash():
    source=page();doc,canonical,sha=normalize_document(source)
    assert doc==source and doc is not source and sha==hashlib.sha256(canonical.encode()).hexdigest()
    assert parse_document(json.dumps(source,ensure_ascii=False))==(doc,canonical,sha)
    assert '<script>ignored()</script>' in canonical and '<img src=x onerror=evil()>' in canonical
    source['datasets'][0]['rows'][0]['name']='changed';assert doc['datasets'][0]['rows'][0]['name']!='changed'


def test_internal_ids_are_deterministically_remapped_without_changing_values():
    source=page();baseline=copy.deepcopy(source)
    legacy,canonical,digest=normalize_document(source)
    assert legacy==baseline and canonical==json.dumps(baseline,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    assert hashlib.sha256(canonical.encode()).hexdigest()==digest
    d=source['datasets'][0];d['id']='Sales-2026';d['columns'][0]['key']='이름';d['columns'][1]['key']='1-total'
    for row in d['rows']:
        if 'name' in row:row['이름']=row.pop('name')
        if 'total' in row:row['1-total']=row.pop('total')
    view=source['views'][0];view.update(id='MAIN-VIEW',dataset='Sales-2026',filters=['Find Text','ACTIVE-ONLY'])
    for block in source['blocks']:
        if block.get('view')=='main':block['view']='MAIN-VIEW'
    source['blocks'][2]['items'][0]['view']='MAIN-VIEW'
    source['blocks'][2]['items'][1].update(view='MAIN-VIEW',field='1-total')
    source['blocks'][3].update(id='Find Text',field='이름')
    source['blocks'][4].update(id='ACTIVE-ONLY')
    source['blocks'][-1]['tabs'][0]['blocks'][0].update(view='MAIN-VIEW',columns=['이름','1-total'])
    source['blocks'][-1]['tabs'][1]['blocks'][0].update(view='MAIN-VIEW',category='이름',value='1-total')
    original=copy.deepcopy(source);doc,encoded,sha=normalize_document(source);again=normalize_document(source)
    assert source==original and again==(doc,encoded,sha)
    ds=doc['datasets'][0];keys=[x['key'] for x in ds['columns']]
    assert ds['id'].startswith('p_') and keys[0].startswith('p_') and keys[1].startswith('p_')
    assert ds['rows'][0][keys[0]]==baseline['datasets'][0]['rows'][0]['name']
    assert ds['rows'][0][keys[1]]==42 and ds['columns'][0]['label']=='Name'
    assert doc['views'][0]['dataset']==ds['id']
    assert doc['blocks'][2]['items'][1]['field']==keys[1]
    assert doc['blocks'][-1]['tabs'][0]['blocks'][0]['columns']==keys[:2]
    assert doc['blocks'][-1]['tabs'][1]['blocks'][0]['category']==keys[0]


def test_id_collision_unknown_reference_and_safe_diagnostic_path_rejected():
    source=page();raw='Invalid-ID';collision='p_'+hashlib.sha256(('dataset\0'+raw).encode()).hexdigest()[:20]
    source['datasets'].append({'id':raw,'columns':[{'key':'x','label':'X','type':'text'}],'rows':[]})
    source['datasets'][0]['id']=collision
    with pytest.raises(PageDocumentError) as error:normalize_document(source)
    assert error.value.code=='page_document_duplicate' and error.value.path=='datasets[1].id'
    source=page();source['views'][0]['dataset']='private-secret-id'
    with pytest.raises(PageDocumentError) as error:normalize_document(source)
    assert error.value.code=='page_document_reference' and error.value.path=='views[0].dataset'
    assert 'private-secret-id' not in str(error.value) and 'private-secret-id' not in error.value.path
    source=page();source['blocks'][0]['private-secret-key']='private-secret-value'
    with pytest.raises(PageDocumentError) as error:normalize_document(source)
    assert error.value.path=='blocks[0]'
    assert 'private-secret' not in repr(vars(error.value))


def test_remapped_document_rechecks_byte_limit():
    source={'format':'gamja.page.v1','title':'Bounded','datasets':[{'id':'a','columns':[{'key':'A','label':'Name','type':'text'},
        {'key':'B','label':'Other','type':'text'},{'key':'C','label':'Third','type':'text'}],
        'rows':[{'A':'x','B':'y','C':'z'} for _ in range(2000)]}],'views':[],'blocks':[{'type':'heading','level':1,'text':'Report'}]}
    assert len(json.dumps(source,ensure_ascii=False).encode())<128*1024
    with pytest.raises(PageDocumentError) as error:normalize_document(source)
    assert error.value.code=='page_document_too_large'


def test_bad_refs_ambiguous_names_and_controls_fail_closed():
    for mutate in (
        lambda d:d['views'][0].update(dataset='missing'),
        lambda d:d['views'][0].update(filters=['query']),
        lambda d:d['blocks'][3].update(field='active'),
        lambda d:d['datasets'][0]['columns'][1].update(label='  Ｎａｍｅ  '),
        lambda d:d['blocks'][2]['items'][1].update(field='name'),
        lambda d:d['blocks'][-1]['tabs'][1].update(label='table'),
        lambda d:d['blocks'][0].update(onclick='evil()'),
    ):
        invalid=copy.deepcopy(page());mutate(invalid)
        with pytest.raises(PageDocumentError):normalize_document(invalid)
    with pytest.raises(PageDocumentError) as error:parse_document('{"format":"a","format":"b"}')
    assert error.value.code=='page_document_duplicate'


def test_limits_and_scalar_validation_fail_closed():
    invalid=page();invalid['datasets'][0]['rows'][0]['total']=float('nan')
    with pytest.raises(PageDocumentError):normalize_document(invalid)
    invalid=page();invalid['datasets'][0]['rows'][0]['date']='2026-02-30'
    with pytest.raises(PageDocumentError):normalize_document(invalid)
    invalid=page();invalid['datasets'][0]['rows']=[{'name':'x'*4000} for _ in range(40)]
    with pytest.raises(PageDocumentError) as error:normalize_document(invalid)
    assert error.value.code=='page_document_too_large'
    invalid=page();block={'type':'heading','level':1,'text':'nested'}
    for _ in range(6):block={'type':'columns','items':[block]}
    invalid['blocks']=[block]
    with pytest.raises(PageDocumentError):normalize_document(invalid)
    invalid=page();invalid['datasets'][0]['rows'][0][1]='bad'
    with pytest.raises(PageDocumentError):normalize_document(invalid)


def test_render_budget_counts_repeated_views_without_truncating():
    document=page()
    document['datasets'][0]['rows']=[{'name':'A','total':1,'active':True} for _ in range(2000)]
    document['views'][0]['filters']=[]
    table={'type':'table','view':'main','columns':['name','total','active'],'title':'Rows'}
    document['blocks']=[table]
    normalized,_,_=normalize_document(document)
    assert len(normalized['datasets'][0]['rows'])==2000 and len(normalized['blocks'])==1
    document['blocks']=[copy.deepcopy(table) for _ in range(9)]
    with pytest.raises(PageDocumentError) as error:normalize_document(document)
    assert error.value.code=='page_document_too_large'


def test_row_shape_diagnostic_is_bounded_and_contains_no_names_or_values():
    source=page();row=source['datasets'][0]['rows'][0]
    row['NAME']='private-value';row['extra']={'secret':'never log'}
    with pytest.raises(PageDocumentError) as error:normalize_document(source)
    shape=error.value.row_shape
    assert error.value.code=='page_document_reference' and error.value.path=='datasets[0].rows[0].cells'
    assert shape['declared_keys']==4 and shape['row_keys']==6 and shape['unknown_keys']==2
    assert shape['unique_casefold_matches']==1 and shape['ambiguous_casefold_matches']==0
    assert shape['unknown_value_shapes']['string']==1 and shape['unknown_value_shapes']['object']==1
    assert shape['saturated'] is False
    assert all(secret not in json.dumps(shape) for secret in ('NAME','extra','private-value','never log'))
    source=page();source['datasets'][0]['rows'][0].update({f'unknown_{i}':i for i in range(55)})
    with pytest.raises(PageDocumentError) as error:normalize_document(source)
    shape=error.value.row_shape
    assert shape['saturated'] is True and shape['unknown_keys']==40 and shape['unknown_value_shapes']['number']==40
