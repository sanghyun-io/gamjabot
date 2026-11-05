"""Bounded OOXML text extraction inside the isolated file worker.

No archive entries are written to disk; no external relationships, macros, formulas,
images, charts or embedded objects are executed or resolved.
"""
import io
import posixpath
import re
import stat
import zipfile
import xml.etree.ElementTree as ET

MAX_MEMBERS = 2000
MAX_MEMBER = 8 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_RATIO = 100
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
P = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'
CT = '{http://schemas.openxmlformats.org/package/2006/content-types}'

class OfficeError(Exception):
    def __init__(self, code):
        self.code = code

class Collector:
    def __init__(self, limit): self.limit=limit; self.parts=[]; self.size=0; self.truncated=False
    def add(self, text):
        if not text: return
        left=self.limit-self.size
        if len(text)>left: self.truncated=True
        if left>0: self.parts.append(text[:left]); self.size+=min(left,len(text))
    def text(self): return ''.join(self.parts)

class Package:
    def __init__(self, data):
        try: self.zip=zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, ValueError): raise OfficeError('office_invalid') from None
        entries=self.zip.infolist()
        if len(entries)>MAX_MEMBERS: raise OfficeError('office_resource_limit')
        self.files={}; seen=set(); total=0
        for info in entries:
            name=info.filename
            if (not name or '\\' in name or name.startswith('/') or '\x00' in name
                    or any(part in ('','.','..') for part in name.rstrip('/').split('/'))
                    or re.match(r'^[A-Za-z]:',name)):
                raise OfficeError('office_invalid')
            key=name.casefold()
            if key in seen: raise OfficeError('office_invalid')
            seen.add(key)
            if info.flag_bits & 1: raise OfficeError('office_encrypted')
            mode=(info.external_attr >> 16) & 0xffff
            if stat.S_ISLNK(mode): raise OfficeError('office_invalid')
            if info.is_dir(): continue
            if info.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED): raise OfficeError('office_invalid')
            if info.file_size>MAX_MEMBER or info.file_size>MAX_RATIO*max(1,info.compress_size): raise OfficeError('office_resource_limit')
            total+=info.file_size
            if total>MAX_EXPANDED: raise OfficeError('office_resource_limit')
            if name.lower().endswith(('vbaproject.bin','vbadata.xml')): raise OfficeError('office_invalid')
            self.files[name]=info
    def xml(self, name):
        info=self.files.get(name)
        if info is None: raise OfficeError('office_invalid')
        try:
            with self.zip.open(info) as stream: raw=stream.read(MAX_MEMBER+1)
            if len(raw)!=info.file_size or len(raw)>MAX_MEMBER: raise OfficeError('office_resource_limit')
            text=raw.decode('utf-8-sig')
            if '\x00' in text or re.search(r'<!\s*(?:DOCTYPE|ENTITY)',text,re.I): raise OfficeError('office_invalid')
            return ET.fromstring(text)
        except OfficeError: raise
        except (UnicodeError,ET.ParseError,zipfile.BadZipFile,RuntimeError,OSError,ValueError): raise OfficeError('office_invalid') from None
    def relationships(self, path, expected):
        root=self.xml(path); out={}; seen=set()
        if root.tag != REL+'Relationships': raise OfficeError('office_invalid')
        for rel in root:
            if rel.tag!=REL+'Relationship': continue
            rid=rel.get('Id'); typ=rel.get('Type',''); target=rel.get('Target','')
            if not rid or rid in seen: raise OfficeError('office_invalid')
            seen.add(rid)
            if rel.get('TargetMode','').lower()=='external': continue
            if not typ.endswith('/'+expected): continue
            if not target or '\\' in target or '?' in target or '#' in target or ':' in target or '%' in target: raise OfficeError('office_invalid')
            base=posixpath.dirname(posixpath.dirname(path)); resolved=posixpath.normpath(posixpath.join(base,target.lstrip('/')) if not target.startswith('/') else target.lstrip('/'))
            if resolved.startswith('../') or resolved not in self.files: raise OfficeError('office_invalid')
            out[rid]=resolved
        return out

def _paragraph_nodes(node):
    for child in node:
        if child.tag == W+'p': continue
        yield child
        yield from _paragraph_nodes(child)

def _cell_text(node):
    return ''.join(x.text or '' for x in node.iter() if x.tag==S+'t')

def parse(data, kind, limit):
    package=Package(data); content=package.xml('[Content_Types].xml')
    if content.tag!=CT+'Types': raise OfficeError('office_invalid')
    types=[x.get('ContentType','') for x in content]
    if any('macroenabled' in t.lower() or 'vba' in t.lower() for t in types): raise OfficeError('office_invalid')
    expected={'docx':('/word/document.xml','application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'),
              'xlsx':('/xl/workbook.xml','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'),
              'pptx':('/ppt/presentation.xml','application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml')}[kind]
    if not any(x.get('PartName')==expected[0] and x.get('ContentType')==expected[1] for x in content): raise OfficeError('office_invalid')
    out=Collector(limit); count=None; has_cell_value=False
    if kind=='docx':
        root=package.xml('word/document.xml')
        if root.tag!=W+'document': raise OfficeError('office_invalid')
        body=root.find(W+'body')
        if body is None: raise OfficeError('office_invalid')
        for para in body.iter(W+'p'):
            for node in _paragraph_nodes(para):
                if node.tag==W+'t': out.add(node.text or '')
                elif node.tag==W+'tab': out.add('\t')
                elif node.tag in (W+'br',W+'cr'): out.add('\n')
            out.add('\n')
    elif kind=='xlsx':
        root=package.xml('xl/workbook.xml')
        if root.tag!=S+'workbook': raise OfficeError('office_invalid')
        rels=package.relationships('xl/_rels/workbook.xml.rels','worksheet')
        sheets=root.find(S+'sheets')
        if sheets is None: raise OfficeError('office_invalid')
        sheets=list(sheets);count=len(sheets)
        if not 1<=count<=500: raise OfficeError('office_resource_limit')
        shared=[]
        shared_rels=package.relationships('xl/_rels/workbook.xml.rels','sharedStrings')
        if len(shared_rels)>1: raise OfficeError('office_invalid')
        if shared_rels:
            sr=package.xml(next(iter(shared_rels.values())))
            if sr.tag!=S+'sst': raise OfficeError('office_invalid')
            for si in sr.findall(S+'si'):
                shared.append(_cell_text(si))
                if len(shared)>100000: raise OfficeError('office_resource_limit')
        for sheet in sheets:
            if sheet.tag!=S+'sheet' or sheet.get(R+'id') not in rels: raise OfficeError('office_invalid')
            out.add((sheet.get('name') or '')+'\n'); xml=package.xml(rels[sheet.get(R+'id')])
            if xml.tag!=S+'worksheet': raise OfficeError('office_invalid')
            last_row=0
            for row in xml.iter(S+'row'):
                number=row.get('r','')
                if not re.fullmatch(r'[1-9][0-9]{0,6}',number): raise OfficeError('office_invalid')
                row_number=int(number)
                if row_number<=last_row or row_number>1048576: raise OfficeError('office_invalid')
                last_row=row_number; last_column=0; cells=[]
                for cell in row.findall(S+'c'):
                    address=cell.get('r',''); match=re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})',address)
                    if not match or int(match.group(2))!=row_number: raise OfficeError('office_invalid')
                    column=0
                    for char in match.group(1): column=column*26+ord(char)-64
                    if column<=last_column or column>16384: raise OfficeError('office_invalid')
                    last_column=column
                    typ=cell.get('t'); value=cell.find(S+'v'); raw=value.text if value is not None and value.text is not None else ''
                    if typ=='s':
                        if not raw.isdigit() or int(raw)>=len(shared): raise OfficeError('office_invalid')
                        raw=shared[int(raw)]
                    elif typ=='inlineStr': raw=_cell_text(cell)
                    elif typ not in (None,'n','str','b','e','d'): raw=''
                    if raw.strip():
                        has_cell_value=True; cells.append(address+': '+raw)
                if cells: out.add('\t'.join(cells)+'\n')
    else:
        root=package.xml('ppt/presentation.xml')
        if root.tag!=P+'presentation': raise OfficeError('office_invalid')
        rels=package.relationships('ppt/_rels/presentation.xml.rels','slide'); slides=root.find(P+'sldIdLst')
        if slides is None: raise OfficeError('office_invalid')
        slides=list(slides);count=len(slides)
        if not 1<=count<=500: raise OfficeError('office_resource_limit')
        for slide in slides:
            if slide.tag!=P+'sldId' or slide.get(R+'id') not in rels: raise OfficeError('office_invalid')
            xml=package.xml(rels[slide.get(R+'id')])
            if xml.tag!=P+'sld': raise OfficeError('office_invalid')
            for para in xml.iter(A+'p'):
                for node in para.iter(A+'t'): out.add(node.text or '')
                out.add('\n')
    if kind=='xlsx':
        count=None
        if not has_cell_value: raise OfficeError('office_no_text')
    text=out.text()
    if not text.strip(): raise OfficeError('office_no_text')
    return text,count,out.truncated
