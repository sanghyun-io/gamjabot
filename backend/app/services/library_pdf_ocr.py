"""Bounded macOS system Vision OCR child. stdin PDF, stdout sanitized JSON.

Uses only Python stdlib ctypes and macOS system frameworks; no PyObjC, network,
app data, credentials or inherited IPC. Parent enforces a wall-time/RSS limit.
"""
import ctypes as c
import hashlib
import json
import math
import resource
import sys
import os

MAX_INPUT=20*1024*1024
MAX_TEXT=40000
MAX_FULL_TEXT=200000
MAX_PAGES=8
MAX_DIMENSION=2048
EXTRACTOR='macos-vision-ocr-v1'

class CGPoint(c.Structure):_fields_=[('x',c.c_double),('y',c.c_double)]
class CGSize(c.Structure):_fields_=[('width',c.c_double),('height',c.c_double)]
class CGRect(c.Structure):_fields_=[('origin',CGPoint),('size',CGSize)]
class Affine(c.Structure):_fields_=[('a',c.c_double),('b',c.c_double),('c',c.c_double),('d',c.c_double),('tx',c.c_double),('ty',c.c_double)]

class OCRError(Exception):
    def __init__(self,code):self.code=code

def framework(path):
    try:return c.CDLL(path)
    except OSError:raise OCRError('ocr_unavailable') from None

def native(data, limit=MAX_TEXT):
    if sys.platform!='darwin':raise OCRError('ocr_unavailable')
    if not data.startswith(b'%PDF-') or len(data)>MAX_INPUT:raise OCRError('ocr_invalid')
    cf=framework('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    cg=framework('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    framework('/System/Library/Frameworks/Foundation.framework/Foundation')
    framework('/System/Library/Frameworks/Vision.framework/Vision')
    objc=framework('/usr/lib/libobjc.A.dylib')
    P=c.c_void_p; I=c.c_long; U=c.c_ulong; D=c.c_double; B=c.c_bool
    def fn(lib,name,restype,*args):
        fun=getattr(lib,name);fun.restype=restype;fun.argtypes=list(args);return fun
    cfdata=fn(cf,'CFDataCreate',P,P,P,I);release=fn(cf,'CFRelease',None,P)
    provider_create=fn(cg,'CGDataProviderCreateWithCFData',P,P)
    provider_release=fn(cg,'CGDataProviderRelease',None,P)
    doc_create=fn(cg,'CGPDFDocumentCreateWithProvider',P,P)
    doc_release=fn(cg,'CGPDFDocumentRelease',None,P)
    encrypted=fn(cg,'CGPDFDocumentIsEncrypted',B,P)
    page_count=fn(cg,'CGPDFDocumentGetNumberOfPages',c.c_size_t,P)
    get_page=fn(cg,'CGPDFDocumentGetPage',P,P,c.c_size_t)
    box=fn(cg,'CGPDFPageGetBoxRect',CGRect,P,c.c_int)
    transform=fn(cg,'CGPDFPageGetDrawingTransform',Affine,P,c.c_int,CGRect,c.c_int,B)
    color_create=fn(cg,'CGColorSpaceCreateDeviceRGB',P);color_release=fn(cg,'CGColorSpaceRelease',None,P)
    bitmap=fn(cg,'CGBitmapContextCreate',P,P,c.c_size_t,c.c_size_t,c.c_size_t,c.c_size_t,P,c.c_uint)
    fill_color=fn(cg,'CGContextSetRGBFillColor',None,P,D,D,D,D)
    fill=fn(cg,'CGContextFillRect',None,P,CGRect)
    concat=fn(cg,'CGContextConcatCTM',None,P,Affine)
    draw=fn(cg,'CGContextDrawPDFPage',None,P,P)
    image_create=fn(cg,'CGBitmapContextCreateImage',P,P)
    ctx_release=fn(cg,'CGContextRelease',None,P);image_release=fn(cg,'CGImageRelease',None,P)
    getclass=fn(objc,'objc_getClass',P,c.c_char_p);selector=fn(objc,'sel_registerName',P,c.c_char_p)
    address=c.cast(objc.objc_msgSend,P).value
    def msg(target,name,ret=P,args=(),types=()):
        if not target:raise OCRError('ocr_processing_failed')
        return c.CFUNCTYPE(ret,P,P,*types)(address)(target,selector(name.encode()),*args)
    def klass(name):
        value=getclass(name.encode())
        if not value:raise OCRError('ocr_unavailable')
        return value
    def string(value):return msg(klass('NSString'),'stringWithUTF8String:',P,(value.encode(),),(c.c_char_p,))
    def array(values):
        ptrs=(P*len(values))(*values)
        return msg(klass('NSArray'),'arrayWithObjects:count:',P,(ptrs,len(values)),(c.POINTER(P),U))
    raw=(c.c_ubyte*len(data)).from_buffer_copy(data);d=provider=doc=None
    try:
        d=cfdata(None,raw,len(data));provider=provider_create(d) if d else None;doc=doc_create(provider) if provider else None
        if not doc:raise OCRError('ocr_invalid')
        if encrypted(doc):raise OCRError('ocr_encrypted')
        count=page_count(doc)
        if count<1:raise OCRError('ocr_no_text')
        pieces=[];size=0;truncated=count>MAX_PAGES
        for index in range(min(count,MAX_PAGES)):
            pool=msg(klass('NSAutoreleasePool'),'new');ctx=img=colors=request=handler=None
            try:
                page=get_page(doc,index+1)
                if not page:raise OCRError('ocr_invalid')
                rect=box(page,1);width=rect.size.width;height=rect.size.height
                if not all(math.isfinite(x) and 0<x<=100000 for x in (width,height)):raise OCRError('ocr_resource_limit')
                scale=MAX_DIMENSION/max(width,height)
                w=max(1,math.ceil(width*scale));h=max(1,math.ceil(height*scale))
                if w*h>MAX_DIMENSION*MAX_DIMENSION:raise OCRError('ocr_resource_limit')
                pixels=(c.c_ubyte*(w*h*4))();colors=color_create()
                ctx=bitmap(pixels,w,h,8,w*4,colors,0x2000|1) if colors else None
                if not ctx:raise OCRError('ocr_resource_limit')
                dest=CGRect(CGPoint(0,0),CGSize(w,h));fill_color(ctx,1,1,1,1);fill(ctx,dest)
                concat(ctx,transform(page,1,dest,0,True));draw(ctx,page);img=image_create(ctx)
                if not img:raise OCRError('ocr_processing_failed')
                request=msg(klass('VNRecognizeTextRequest'),'new')
                msg(request,'setRecognitionLevel:',None,(0,),(I,))
                msg(request,'setUsesLanguageCorrection:',None,(True,),(B,))
                msg(request,'setRecognitionLanguages:',None,(array([string('ko-KR'),string('en-US')]),),(P,))
                handler=msg(msg(klass('VNImageRequestHandler'),'alloc'),'initWithCGImage:options:',P,(img,None),(P,P))
                error=P();ok=msg(handler,'performRequests:error:',B,(array([request]),c.byref(error)),(P,c.POINTER(P)))
                if not ok:raise OCRError('ocr_processing_failed')
                observations=msg(request,'results');n=msg(observations,'count',U) if observations else 0
                if n>10000:raise OCRError('ocr_resource_limit')
                lines=[]
                for i in range(n):
                    obs=msg(observations,'objectAtIndex:',P,(i,),(U,))
                    candidates=msg(obs,'topCandidates:',P,(1,),(U,))
                    if not candidates or msg(candidates,'count',U)==0:continue
                    candidate=msg(candidates,'objectAtIndex:',P,(0,),(U,));value=msg(candidate,'string')
                    text=msg(value,'UTF8String',c.c_char_p)
                    if text:lines.append(text.decode('utf-8','strict'))
                part='\n'.join(lines)
                if pieces and size<limit:pieces.append('\n');size+=1
                remaining=limit-size;pieces.append(part[:remaining]);size+=min(len(part),remaining)
                if len(part)>remaining or (size>=limit and index+1<min(count,MAX_PAGES)):
                    truncated=True;break
            finally:
                if handler:msg(handler,'release',None)
                if request:msg(request,'release',None)
                if img:image_release(img)
                if ctx:ctx_release(ctx)
                if colors:color_release(colors)
                if pool:msg(pool,'drain',None)
        result=''.join(pieces)
        if not result.strip():raise OCRError('ocr_no_text')
        return {'text':result,'text_sha256':hashlib.sha256(result.encode()).hexdigest(),'format':'pdf',
                'page_count':count,'truncated':truncated,'extractor':EXTRACTOR}
    finally:
        if doc:doc_release(doc)
        if provider:provider_release(provider)
        if d:release(d)

def main():
    try:
        resource.setrlimit(resource.RLIMIT_CPU,(45,45))
        resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,1024*1024))
        value=native(sys.stdin.buffer.read(MAX_INPUT+1),limit=MAX_FULL_TEXT if os.environ.get('GAMJA_FILE_TEXT_LIMIT')==str(MAX_FULL_TEXT) else MAX_TEXT)
    except OCRError as error:value={'error':error.code}
    except Exception:value={'error':'ocr_processing_failed'}
    sys.stdout.buffer.write(json.dumps(value,ensure_ascii=True,separators=(',',':')).encode('ascii'))
    return 0
