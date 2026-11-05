"""Bounded, declarative Page documents. Text is data, never executable markup."""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import date

FORMAT = 'gamja.page.v1'
MAX_BYTES = 128 * 1024
MAX_DEPTH = 6
MAX_BLOCKS = 256
MAX_ROWS = 2000
MAX_RENDER_UNITS = 50_000
ID = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
PAGE_MODEL_GUIDANCE = (
    'Produce {format:"gamja.page.v1",title,datasets,views,blocks}. '
    'datasets are {id,columns:[{key,label,type:text|number|boolean|date}],rows:[{columnKey:scalar|null}]}; '
    'views are {id,dataset,filters:[controlId]}. '
    'Blocks: {type:heading,level:1|2|3,text}; {type:markdown,text}; '
    '{type:metric,view,aggregate:count|sum|avg|min|max,label,field?}; '
    '{type:table,view,columns:[columnKey],title}; '
    '{type:chart,kind:bar|line|pie,view,category,aggregate:count|sum|avg,title,value?}; '
    '{type:control,id,view,field,mode:search|select|toggle,label}; '
    '{type:columns,items:[blocks]}; {type:tabs,tabs:[{label,blocks:[blocks]}]}. '
    'Every control occurs in exactly its declared view filters. Search requires text and toggle boolean. '
    'Count counts rows and omits field/value; other aggregates require a number field/value. '
    'Missing cells are null. Filters are temporary view state combined by AND. '
    'IDs and column keys are lowercase letters, digits, underscores, starting with a letter. '
    'Keep within 128KiB, 2000 rows, 256 blocks, nesting depth 6 and 50000 total render units. '
    'Text and Markdown are inert: no HTML, script, external resource or URL executes.'
)
_ID_SCHEMA={'type':'string','pattern':'^[a-z][a-z0-9_]{0,63}$'}
_COLUMN_SCHEMA={'type':'object','properties':{'key':_ID_SCHEMA,'label':{'type':'string','minLength':1,'maxLength':120},
    'type':{'type':'string','enum':['text','number','boolean','date']}},'required':['key','label','type'],'additionalProperties':False}
_DATASET_SCHEMA={'type':'object','properties':{'id':_ID_SCHEMA,'columns':{'type':'array','minItems':1,'maxItems':40,'items':_COLUMN_SCHEMA},
    'rows':{'type':'array','maxItems':MAX_ROWS,'items':{'type':'object'}}},'required':['id','columns','rows'],'additionalProperties':False}
_VIEW_SCHEMA={'type':'object','properties':{'id':_ID_SCHEMA,'dataset':_ID_SCHEMA,'filters':{'type':'array','maxItems':32,'items':_ID_SCHEMA}},
    'required':['id','dataset','filters'],'additionalProperties':False}
_BLOCK_SCHEMA={'type':'object','properties':{'type':{'type':'string','enum':['heading','markdown','metric','table','chart','control','columns','tabs']},
    'level':{'type':'integer','minimum':1,'maximum':3},'text':{'type':'string'},'view':_ID_SCHEMA,'id':_ID_SCHEMA,'field':_ID_SCHEMA,
    'aggregate':{'type':'string','enum':['count','sum','avg','min','max']},'label':{'type':'string'},'title':{'type':'string'},
    'columns':{'type':'array','items':_ID_SCHEMA},'kind':{'type':'string','enum':['bar','line','pie']},'category':_ID_SCHEMA,'value':_ID_SCHEMA,
    'mode':{'type':'string','enum':['search','select','toggle']},'items':{'type':'array','items':{'type':'object'}},
    'tabs':{'type':'array','items':{'type':'object'}}},'required':['type'],'additionalProperties':False}
PAGE_DOCUMENT_SCHEMA={'type':'object','properties':{'format':{'type':'string','enum':[FORMAT]},'title':{'type':'string','minLength':1,'maxLength':200},
    'datasets':{'type':'array','maxItems':12,'items':_DATASET_SCHEMA},'views':{'type':'array','maxItems':32,'items':_VIEW_SCHEMA},
    'blocks':{'type':'array','minItems':1,'maxItems':MAX_BLOCKS,'items':_BLOCK_SCHEMA}},
    'required':['format','title','datasets','views','blocks'],'additionalProperties':False}



class PageDocumentError(ValueError):
    def __init__(self, code: str = 'page_document_invalid', reason: str = 'invalid', path: str = 'document'):
        self.code = code
        self.reason = reason if reason in {'invalid','object_shape','string_value','identifier','list_shape','scalar_type','duplicate','reference','too_large'} else 'invalid'
        self.path = path if isinstance(path,str) and len(path)<=160 and re.fullmatch(r'(?:document|datasets|views|blocks)(?:\[\d{1,4}\]|\.[a-z_]+)*',path) else 'document'
        super().__init__(code)


def _fail(code='page_document_invalid', *, reason='invalid'):
    reason = {'page_document_duplicate':'duplicate','page_document_reference':'reference','page_document_too_large':'too_large'}.get(code,reason)
    raise PageDocumentError(code,reason)


def _object(value, required, optional=()):
    if type(value) is not dict:
        error=PageDocumentError(reason='object_shape');error.shape_issue='not_object';raise error
    missing=next((field for field in required if field not in value),None)
    if missing:
        error=PageDocumentError(reason='object_shape')
        # `required` comes exclusively from fixed validator declarations.
        error.field=missing;error.shape_issue='missing_required'
        raise error
    if value.keys() - set(required) - set(optional):
        error=PageDocumentError(reason='object_shape');error.shape_issue='extra_fields';raise error


_BLOCK_FIELDS = {
    'metric':(('type','view','aggregate','label'),('field',)),
    'table':(('type','view','columns','title'),()),
    'chart':(('type','view','kind','category','aggregate','title'),('value',)),
    'control':(('type','id','view','field','mode','label'),()),
}


def safe_block_shape(value):
    # Only fixed validator declarations are exposed, never caller-supplied keys or values.
    if type(value) is not dict or set(value)!={'issue','block_type','missing_field'}:
        return None
    issue=value['issue'];kind=value['block_type'];field=value['missing_field']
    if type(kind) is not str or kind not in _BLOCK_FIELDS or type(issue) is not str or issue not in ('missing_required','extra_fields','not_object'):
        return None
    if issue=='missing_required':
        if field not in _BLOCK_FIELDS[kind][0]: return None
    elif field is not None:
        return None
    return {'issue':issue,'block_type':kind,'missing_field':field}


def _block_object(block, kind, required, optional):
    try:
        _object(block, required, optional)
    except PageDocumentError as error:
        error.block_shape=safe_block_shape({'issue':getattr(error,'shape_issue',None),
            'block_type':kind,'missing_field':getattr(error,'field',None)})
        raise


def _string(value, limit, *, identifier=False):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        _fail(reason='string_value')
    if identifier and not ID.fullmatch(value):
        _fail(reason='identifier')
    return value


def _list(value, maximum, *, nonempty=False):
    if type(value) is not list or len(value) > maximum or (nonempty and not value):
        _fail(reason='list_shape')
    return value


def _unique_names(items):
    names = [unicodedata.normalize('NFKC', name).strip().casefold() for name in items]
    if len(set(names)) != len(names):
        _fail('page_document_duplicate')


def _scalar(value, kind):
    if value is None:
        return
    if kind == 'text':
        if not isinstance(value, str) or len(value) > 4000 or '\x00' in value:
            _fail(reason='scalar_type')
    elif kind == 'date':
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            _fail(reason='scalar_type')
        try:
            if date.fromisoformat(value).isoformat() != value: _fail(reason='scalar_type')
        except ValueError:
            _fail(reason='scalar_type')
    elif kind == 'boolean':
        if type(value) is not bool: _fail(reason='scalar_type')
    elif kind == 'number':
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 9_007_199_254_740_991: _fail(reason='scalar_type')
    else:
        _fail()



def safe_row_shape(value):
    """Only fixed non-content counters may cross the error/event boundary."""
    names={'declared_keys','row_keys','unknown_keys','unique_casefold_matches','ambiguous_casefold_matches'}
    kinds={'null','boolean','number','string','array','object'}
    if (type(value) is not dict or set(value)!=names|{'unknown_value_shapes','saturated'}
            or any(type(value[k]) is not int or not 0<=value[k]<=40 for k in names)
            or type(value['saturated']) is not bool or type(value['unknown_value_shapes']) is not dict
            or set(value['unknown_value_shapes'])!=kinds
            or any(type(v) is not int or not 0<=v<=40 for v in value['unknown_value_shapes'].values())):
        return None
    return {**{k:value[k] for k in sorted(names)}, 'unknown_value_shapes':{k:value['unknown_value_shapes'][k] for k in sorted(kinds)}, 'saturated':value['saturated']}


def _row_shape(columns,row):
    unknown=[(k,v) for k,v in row.items() if k not in columns]
    shapes={'null':0,'boolean':0,'number':0,'string':0,'array':0,'object':0}
    unique=ambiguous=0
    for key,value in unknown:
        folded=key.casefold()
        count=sum(1 for name in columns if name.casefold()==folded)
        unique+=count==1;ambiguous+=count>1
        kind=('null' if value is None else 'boolean' if type(value) is bool else 'number' if type(value) in (int,float)
              else 'string' if type(value) is str else 'array' if type(value) is list else 'object')
        shapes[kind]+=1
    counts={'declared_keys':len(columns),'row_keys':len(row),'unknown_keys':len(unknown),
            'unique_casefold_matches':unique,'ambiguous_casefold_matches':ambiguous}
    saturated=any(v>40 for v in (*counts.values(),*shapes.values()))
    return {**{k:min(v,40) for k,v in counts.items()},'unknown_value_shapes':{k:min(v,40) for k,v in shapes.items()},'saturated':saturated}

def _canonicalize_ids(doc):
    """Rewrite declared internal identifiers and exact references, never values."""
    path='document'
    try:
        _object(doc,('format','title','datasets','views','blocks'))
        maps={}; dataset_names={}; view_names={}; control_names={}; seen_blocks=[]
        def declare(mapping,raw,namespace):
            _string(raw,64)
            if raw in mapping: _fail('page_document_duplicate')
            canonical=raw if ID.fullmatch(raw) else 'p_'+hashlib.sha256((namespace+'\0'+raw).encode('utf-8')).hexdigest()[:20]
            if canonical in mapping.values(): _fail('page_document_duplicate')
            mapping[raw]=canonical
            return canonical
        def ref(mapping,raw):
            if not isinstance(raw,str) or raw not in mapping: _fail('page_document_reference')
            return mapping[raw]
        for i,dataset in enumerate(_list(doc['datasets'],12)):
            path=f'datasets[{i}]';_object(dataset,('id','columns','rows'))
            path=f'datasets[{i}].id';raw=dataset['id'];declare(dataset_names,raw,'dataset')
            columns={};maps[raw]=columns
            for j,column in enumerate(_list(dataset['columns'],40,nonempty=True)):
                path=f'datasets[{i}].columns[{j}]';_object(column,('key','label','type'))
                path=f'datasets[{i}].columns[{j}].key';declare(columns,column['key'],'column:'+dataset_names[raw])
        view_datasets={}
        for i,view in enumerate(_list(doc['views'],32)):
            path=f'views[{i}]';_object(view,('id','dataset','filters'))
            path=f'views[{i}].id';raw=view['id'];declare(view_names,raw,'view')
            path=f'views[{i}].dataset';ref(dataset_names,view['dataset']);view_datasets[raw]=view['dataset']
        def collect(block,where,depth):
            nonlocal path
            path=where
            if depth>MAX_DEPTH:_fail('page_document_too_large')
            if type(block) is not dict or not isinstance(block.get('type'),str):_fail(reason='object_shape')
            seen_blocks.append((block,where))
            if block['type']=='control':
                path=where+'.id';declare(control_names,block.get('id'),'control')
            elif block['type']=='columns':
                path=where+'.items'
                for j,child in enumerate(_list(block.get('items'),4,nonempty=True)):collect(child,f'{where}.items[{j}]',depth+1)
            elif block['type']=='tabs':
                path=where+'.tabs'
                for j,tab in enumerate(_list(block.get('tabs'),12,nonempty=True)):
                    path=f'{where}.tabs[{j}]';_object(tab,('label','blocks'))
                    for k,child in enumerate(_list(tab['blocks'],MAX_BLOCKS,nonempty=True)):collect(child,f'{where}.tabs[{j}].blocks[{k}]',depth+1)
        for i,block in enumerate(_list(doc['blocks'],MAX_BLOCKS,nonempty=True)):collect(block,f'blocks[{i}]',1)
        for i,dataset in enumerate(doc['datasets']):
            raw=dataset['id'];columns=maps[raw];dataset['id']=dataset_names[raw]
            for column in dataset['columns']:column['key']=columns[column['key']]
            for j,row in enumerate(_list(dataset['rows'],MAX_ROWS)):
                path=f'datasets[{i}].rows[{j}].cells'
                if type(row) is not dict:_fail(reason='object_shape')
                replaced={}
                for key,value in row.items():
                    if key not in columns:
                        error=PageDocumentError('page_document_reference','reference',path)
                        error.row_shape=_row_shape(columns,row)
                        raise error
                    new_key=ref(columns,key)
                    if new_key in replaced:_fail('page_document_duplicate')
                    replaced[new_key]=value
                dataset['rows'][j]=replaced
        for i,view in enumerate(doc['views']):
            raw=view['id'];view['id']=view_names[raw];view['dataset']=ref(dataset_names,view['dataset'])
            path=f'views[{i}].filters';view['filters']=[ref(control_names,x) for x in _list(view['filters'],32)]
        for block,where in seen_blocks:
            kind=block['type']
            if kind not in ('metric','table','chart','control'):continue
            path=where+'.view';raw_view=block.get('view');canonical_view=ref(view_names,raw_view)
            columns=maps[view_datasets[raw_view]];block['view']=canonical_view
            if kind=='control':
                path=where+'.id';block['id']=ref(control_names,block.get('id'))
            fields={'control':('field',),'table':(),'metric':('field',),'chart':('category','value')}[kind]
            for field in fields:
                if field in block:
                    path=where+'.'+field;block[field]=ref(columns,block[field])
            if kind=='table':
                path=where+'.columns';block['columns']=[ref(columns,x) for x in _list(block.get('columns'),40,nonempty=True)]
        return doc
    except PageDocumentError as error:
        field=getattr(error,'field',None)
        error.path=PageDocumentError(error.code,error.reason,path+('.'+field if field else '')).path
        raise
    except (KeyError,TypeError,ValueError,OverflowError,RecursionError,UnicodeError):
        raise PageDocumentError(path=path) from None


def normalize_document(value: object) -> tuple[dict, str, str]:
    """Return a validated deep copy, canonical JSON and its SHA-256; never log input."""
    path='document'
    try:
        def json_keys(item, depth=0):
            if depth > 32: _fail('page_document_too_large')
            if type(item) is dict:
                if any(type(key) is not str for key in item): _fail()
                for child in item.values(): json_keys(child, depth+1)
            elif type(item) is list:
                for child in item: json_keys(child, depth+1)
        json_keys(value)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(raw.encode('utf-8')) > MAX_BYTES: _fail('page_document_too_large')
        doc = _canonicalize_ids(json.loads(raw))
        raw = json.dumps(doc,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(raw.encode('utf-8')) > MAX_BYTES:_fail('page_document_too_large')
        _object(doc, ('format', 'title', 'datasets', 'views', 'blocks'))
        path='document.format'
        if doc['format'] != FORMAT: _fail()
        path='document.title';_string(doc['title'], 200)
        datasets = {}
        total_rows = 0
        for i,dataset in enumerate(_list(doc['datasets'], 12)):
            path=f'datasets[{i}]'
            _object(dataset, ('id', 'columns', 'rows'))
            identifier = _string(dataset['id'], 64, identifier=True)
            if identifier in datasets: _fail('page_document_duplicate')
            columns = {}
            labels = []
            for j,column in enumerate(_list(dataset['columns'], 40, nonempty=True)):
                path=f'datasets[{i}].columns[{j}]'
                _object(column, ('key', 'label', 'type'))
                key = _string(column['key'], 64, identifier=True)
                path=f'datasets[{i}].columns[{j}].label';label = _string(column['label'], 120)
                if key in columns: _fail('page_document_duplicate')
                path=f'datasets[{i}].columns[{j}].type'
                if column['type'] not in ('text', 'number', 'boolean', 'date'): _fail()
                columns[key] = column['type']; labels.append(label)
            _unique_names(labels)
            rows = _list(dataset['rows'], MAX_ROWS)
            total_rows += len(rows)
            if total_rows > MAX_ROWS: _fail('page_document_too_large')
            for j,row in enumerate(rows):
                path=f'datasets[{i}].rows[{j}].cells'
                if type(row) is not dict or row.keys() - columns.keys(): _fail()
                for key, cell in row.items(): _scalar(cell, columns[key])
            datasets[identifier] = (columns, len(rows))
        views = {}
        for i,view in enumerate(_list(doc['views'], 32)):
            path=f'views[{i}]'
            _object(view, ('id', 'dataset', 'filters'))
            identifier = _string(view['id'], 64, identifier=True)
            if identifier in views: _fail('page_document_duplicate')
            if view['dataset'] not in datasets: _fail('page_document_reference')
            filters = _list(view['filters'], 32)
            for control in filters: _string(control, 64, identifier=True)
            _unique_names(filters)
            views[identifier] = (*datasets[view['dataset']], filters)
        controls = {}
        block_count = 0
        render_units = 0
        def charge(units):
            nonlocal render_units
            render_units += units
            if render_units > MAX_RENDER_UNITS: _fail('page_document_too_large')
        def visit(block, depth, where):
            nonlocal block_count,path
            path=where
            block_count += 1
            charge(1)
            if depth > MAX_DEPTH or block_count > MAX_BLOCKS: _fail('page_document_too_large')
            if type(block) is not dict or not isinstance(block.get('type'), str): _fail()
            kind = block['type']
            if kind == 'heading':
                _object(block, ('type', 'level', 'text'))
                path=where+'.level'
                if type(block['level']) is not int or block['level'] not in (1, 2, 3): _fail()
                path=where+'.text';_string(block['text'], 500)
            elif kind == 'markdown':
                _object(block, ('type', 'text')); path=where+'.text';_string(block['text'], 16000)
            elif kind in ('metric', 'table', 'chart', 'control'):
                required, optional = _BLOCK_FIELDS[kind]
                _block_object(block, kind, required, optional)
                view_id = block['view']
                if not isinstance(view_id,str) or view_id not in views: _fail('page_document_reference')
                columns, row_count, _ = views[view_id]
                if kind == 'control':
                    identifier = _string(block['id'],64,identifier=True)
                    if identifier in controls: _fail('page_document_duplicate')
                    path=where+'.label';_string(block['label'],120)
                    field = block['field']; mode = block['mode']
                    if not isinstance(field,str) or field not in columns: _fail('page_document_reference')
                    path=where+'.mode'
                    if mode not in ('search','select','toggle'): _fail()
                    if mode == 'search' and columns[field] != 'text' or mode == 'toggle' and columns[field] != 'boolean': _fail()
                    if mode == 'select': charge(row_count + 1)
                    controls[identifier] = view_id
                elif kind == 'table':
                    path=where+'.title';_string(block['title'],200)
                    selected = _list(block['columns'],40,nonempty=True)
                    if any(not isinstance(c,str) or c not in columns for c in selected): _fail('page_document_reference')
                    _unique_names(selected)
                    charge((row_count + 1) * len(selected))
                else:
                    _string(block['label' if kind == 'metric' else 'title'],200)
                    path=where+'.aggregate';aggregate = block['aggregate']
                    if aggregate not in (('count','sum','avg','min','max') if kind == 'metric' else ('count','sum','avg')): _fail()
                    if kind == 'chart':
                        charge(row_count)
                        if block['kind'] not in ('bar','line','pie'): _fail()
                        if not isinstance(block['category'],str) or block['category'] not in columns: _fail('page_document_reference')
                    key = 'field' if kind == 'metric' else 'value'
                    if aggregate == 'count':
                        if key in block: _fail()
                    elif key not in block or not isinstance(block[key],str) or columns.get(block[key]) != 'number':
                        _fail('page_document_reference')
            elif kind == 'columns':
                _object(block, ('type','items'))
                for j,child in enumerate(_list(block['items'],4,nonempty=True)): visit(child,depth+1,f'{where}.items[{j}]')
            elif kind == 'tabs':
                _object(block, ('type','tabs'))
                tabs = _list(block['tabs'],12,nonempty=True); labels=[]
                for j,tab in enumerate(tabs):
                    _object(tab, ('label','blocks')); labels.append(_string(tab['label'],120))
                    for k,child in enumerate(_list(tab['blocks'],MAX_BLOCKS,nonempty=True)): visit(child,depth+1,f'{where}.tabs[{j}].blocks[{k}]')
                _unique_names(labels)
            else: _fail()
        for i,block in enumerate(_list(doc['blocks'],MAX_BLOCKS,nonempty=True)): visit(block,1,f'blocks[{i}]')
        seen = set()
        for view_id, (_, _, filters) in views.items():
            for control in filters:
                if controls.get(control) != view_id or control in seen: _fail('page_document_reference')
                seen.add(control)
        if seen != set(controls): _fail('page_document_reference')
        return doc, raw, hashlib.sha256(raw.encode('utf-8')).hexdigest()
    except PageDocumentError as error:
        if error.path=='document':
            field=getattr(error,'field',None)
            error.path=PageDocumentError(error.code,error.reason,path+('.'+field if field else '')).path
        raise
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError, AttributeError):
        raise PageDocumentError() from None


def parse_document(raw: str) -> tuple[dict, str, str]:
    if not isinstance(raw,str): _fail()
    try:
        if len(raw.encode('utf-8')) > MAX_BYTES: _fail('page_document_too_large')
        def pairs(items):
            result={}
            for key,value in items:
                if key in result: _fail('page_document_duplicate')
                result[key]=value
            return result
        return normalize_document(json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: _fail()))
    except PageDocumentError:
        raise
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise PageDocumentError() from None
