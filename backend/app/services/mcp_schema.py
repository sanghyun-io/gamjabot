"""Bounded same-document MCP schema references; no external retrieval."""
import re
from jsonschema import Draft7Validator,Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource

_MAX_DEPTH = 64
_MAX_EXPANSION = 131_072  # Derived from two 65,536-byte schema budgets.
_DIALECT = 'https://json-schema.org/draft/2020-12/schema'
_DRAFT7 = 'http://json-schema.org/draft-07/schema#'
_FORBIDDEN = frozenset({'$id','$anchor','$dynamicAnchor','$dynamicRef','$recursiveRef','$recursiveAnchor'})
_DENIED = frozenset({'$dynamicRef','$recursiveRef'})


def _deny_retrieve(uri):
    raise NoSuchResource(ref=uri)


_NO_RETRIEVAL = Registry(retrieve=_deny_retrieve)


def is_mcp_family(name):
    return isinstance(name,str) and re.fullmatch(r'mcp_[0-9a-f]{20}',name) is not None


def _pointer(root,ref):
    if not isinstance(ref,str) or not ref.startswith('#/') or '%' in ref:
        raise ValueError('invalid MCP schema reference')
    parts=ref[2:].split('/')
    if len(parts)>_MAX_DEPTH:raise ValueError('invalid MCP schema reference')
    current=root
    for token in parts:
        if re.search(r'~(?![01])',token):raise ValueError('invalid MCP schema reference')
        token=token.replace('~1','/').replace('~0','~')
        if isinstance(current,dict) and token in current:current=current[token]
        elif isinstance(current,list) and re.fullmatch(r'0|[1-9][0-9]*',token) and len(token)<=10 and int(token)<len(current):current=current[int(token)]
        else:raise ValueError('invalid MCP schema reference')
    if not isinstance(current,(dict,bool)):raise ValueError('invalid MCP schema reference')
    return current


def safe_mcp_schema(schema):
    """Accept bounded local static pointer graphs; leave original schema untouched."""
    if not isinstance(schema,dict):return False
    dialect=schema.get('$schema',_DIALECT)
    if dialect not in (_DIALECT,_DRAFT7):return False
    stack=[(schema,0)];has_refs=False;forbidden=False;nodes=0
    while stack:
        value,depth=stack.pop();nodes+=1
        if depth>_MAX_DEPTH or nodes>_MAX_EXPANSION:return False
        if isinstance(value,dict):
            if any(k in value for k in _DENIED):return False
            if '$ref' in value:has_refs=True
            if '$schema' in value and value['$schema']!=dialect:return False
            if any(k in value for k in _FORBIDDEN):forbidden=True
            stack.extend((v,depth+1) for v in value.values())
        elif isinstance(value,list):stack.extend((v,depth+1) for v in value)
    if not has_refs:return True
    if forbidden:return False
    active=set();memo={}
    def visit(value,depth=0):
        if depth>_MAX_DEPTH:raise ValueError('MCP schema expansion limit')
        if not isinstance(value,(dict,list)):return (1,0)
        key=id(value)
        if key in active:raise ValueError('cyclic MCP schema reference')
        if key in memo:
            if depth+memo[key][1]>_MAX_DEPTH:raise ValueError('MCP schema expansion limit')
            return memo[key]
        active.add(key);cost=1;height=0
        children=value.values() if isinstance(value,dict) else value
        for child in children:
            sub,subheight=visit(child,depth+1);cost+=sub;height=max(height,subheight+1)
            if cost>_MAX_EXPANSION or height>_MAX_DEPTH:raise ValueError('MCP schema expansion limit')
        if isinstance(value,dict) and '$ref' in value:
            target=_pointer(schema,value['$ref']);sub,subheight=visit(target,depth+1);cost+=sub;height=max(height,subheight+1)
            if cost>_MAX_EXPANSION or height>_MAX_DEPTH:raise ValueError('MCP schema expansion limit')
        active.remove(key);memo[key]=(cost,height);return memo[key]
    try:visit(schema)
    except (ValueError,RecursionError):return False
    return True


def validate_mcp_arguments(schema,arguments):
    if not safe_mcp_schema(schema):raise ValueError('invalid MCP schema')
    validator=Draft7Validator if schema.get('$schema')==_DRAFT7 else Draft202012Validator
    validator(schema,registry=_NO_RETRIEVAL).validate(arguments)


def validate_mcp_meta(schema):
    if not safe_mcp_schema(schema):raise ValueError('invalid MCP schema')
    validator=Draft7Validator if schema.get('$schema')==_DRAFT7 else Draft202012Validator
    validator(validator.META_SCHEMA,registry=_NO_RETRIEVAL).validate(schema)
