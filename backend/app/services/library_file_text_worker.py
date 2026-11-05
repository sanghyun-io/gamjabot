"""Fixed PDF stdin/stdout worker. No app initialization, files, network or credentials."""
import io
import json
import logging
import sys
import os
from app.services.library_file_text import MAX_INPUT, MAX_TEXT, MAX_FULL_TEXT, result

def text_limit():
    return MAX_FULL_TEXT if os.environ.get('GAMJA_FILE_TEXT_LIMIT') == str(MAX_FULL_TEXT) else MAX_TEXT

def _limits():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,(5,5))
        # Darwin rejects RLIMIT_DATA/AS. The parent watches this owned
        # worker's RSS there; this is a threshold monitor, not a hard cap.
        if sys.platform != 'darwin':
            resource.setrlimit(resource.RLIMIT_DATA,(256*1024*1024,256*1024*1024))
        resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,1024*1024))
    except (ImportError,OSError,ValueError): return False
    return True

def parse(data, limit=MAX_TEXT):
    from pypdf import PdfReader, apply_configuration
    if not data.startswith(b'%PDF-') or len(data)>MAX_INPUT: return {'error':'pdf_invalid'}
    with apply_configuration(maximum_declared_stream_length=2_000_000,
        array_based_stream_maximum_output_length=2_000_000,zlib_maximum_output_length=2_000_000,
        lzw_maximum_output_length=2_000_000,run_length_maximum_output_length=2_000_000,
        brotli_maximum_output_length=2_000_000,stream_decoding_work_maximum_length=8_000_000,
        stream_parsing_work_maximum_count=150_000,array_maximum_element_count=10_000,
        dictionary_maximum_element_count=10_000,page_tree_maximum_entries=2_000,
        page_tree_maximum_depth=50,page_tree_maximum_page_entries=10_000,
        xform_maximum_invocations_per_extraction=256,jbig2dec_binary=None):
        reader=PdfReader(io.BytesIO(data),strict=True,root_object_recovery_limit=0,
                         xref_stream_maximum_entries=100_000)
        if reader.is_encrypted: return {'error':'pdf_encrypted'}
        count=len(reader.pages)
        if count<1: return {'error':'pdf_no_text'}
        parts=[];size=0;truncated=count>50
        for index in range(min(count,50)):
            page=reader.pages[index]
            stream=page.get_contents()
            if stream is not None and len(stream.get_data())>2_000_000: return {'error':'pdf_resource_limit'}
            text=page.extract_text() or ''
            available=limit-size
            fragment=text[:available]
            parts.append(fragment);size+=len(fragment)
            if len(text)>available or (size>=limit and index<count-1):
                truncated=True;break
            if size<limit and index<min(count,50)-1:
                parts.append('\n');size+=1
        text=''.join(parts)
        if not text.strip(): return {'error':'pdf_no_text'}
        return result(text,'pdf',count,truncated,limit=limit)

def main(kind=None):
    logging.disable(logging.CRITICAL)
    if kind in ('docx','xlsx','pptx'):
        return office_main(kind)
    if not _limits(): value={'error':'pdf_limits_unavailable'}
    else:
        try:
            from pypdf.errors import LimitReachedError
            try: value=parse(sys.stdin.buffer.read(MAX_INPUT+1),limit=text_limit())
            except LimitReachedError: value={'error':'pdf_resource_limit'}
        except MemoryError: value={'error':'pdf_resource_limit'}
        except Exception: value={'error':'pdf_invalid'}
    sys.stdout.buffer.write(json.dumps(value,ensure_ascii=True,separators=(',',':')).encode('ascii'))
    return 0


def office_main(kind):
    from app.services.library_office_text import OfficeError, parse as parse_office
    if not _limits(): value={'error':'office_limits_unavailable'}
    else:
        try:
            data=sys.stdin.buffer.read(MAX_INPUT+1)
            if len(data)>MAX_INPUT: raise OfficeError('office_resource_limit')
            text,count,truncated=parse_office(data,kind,text_limit())
            value=result(text,kind,count,truncated,limit=text_limit())
        except OfficeError as exc: value={'error':exc.code}
        except MemoryError: value={'error':'office_resource_limit'}
        except Exception: value={'error':'office_invalid'}
    sys.stdout.buffer.write(json.dumps(value,ensure_ascii=True,separators=(',',':')).encode('ascii'))
    return 0
