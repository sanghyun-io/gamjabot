"""Bounded local file interpretation. This module never calls a model."""
import asyncio
import hashlib
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

EXTRACTOR = 'utf8-pdf-v1/pypdf6.21.4'
OFFICE_EXTRACTOR = 'ooxml-text-v1/stdlib'
MAX_INPUT = 20 * 1024 * 1024
MAX_TEXT = 40_000
MAX_FULL_TEXT = 200_000
FILE_RANGE = {"max_chars":MAX_FULL_TEXT,"pdf_max_pages":50,"ocr_max_pages":8}
PDF_DEADLINE = 10
_pdf_slot = asyncio.Semaphore(1)
_active_ocr = None
_normal_waiters = 0
OCR_ADMISSION_SECONDS = 1
_TEXT_EXTENSIONS = {'.txt','.md','.csv','.tsv','.json','.yaml','.yml','.log','.py','.js',
                    '.ts','.tsx','.jsx','.sql','.html','.htm','.css','.xml','.rst','.toml'}

class ExtractionError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def result(text, kind, pages=None, truncated=False, limit=MAX_TEXT):
    limited = text[:limit]
    return {'text':limited, 'text_sha256':hashlib.sha256(limited.encode('utf-8')).hexdigest(),
            'format':kind, 'page_count':pages, 'truncated':truncated or len(text)>limit,
            'extractor':OFFICE_EXTRACTOR if kind in ('docx','xlsx','pptx') else EXTRACTOR}

def _worker_environment():
    # No app/session/provider credentials, inherited IPC descriptors or Python path overrides.
    allowed = {'LANG','LC_ALL','LC_CTYPE','SYSTEMROOT','WINDIR','TMPDIR',
               '_PYI_APPLICATION_HOME_DIR','_PYI_PARENT_PROCESS_LEVEL','_PYI_ARCHIVE_FILE'}
    return {**{k:v for k,v in os.environ.items() if k in allowed},
            'PYTHONDONTWRITEBYTECODE':'1','PYTHONUTF8':'1'}

async def _watch_memory(proc,limit_kib=256*1024):
    """Darwin RSS threshold; sampling can overshoot, so parser limits also apply."""
    while proc.returncode is None:
        probe=None
        try:
            probe=await asyncio.create_subprocess_exec('/bin/ps','-o','rss=','-p',str(proc.pid),
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
                env=_worker_environment(),close_fds=True)
            output,_=await asyncio.wait_for(probe.communicate(),1)
            if proc.returncode is not None: return
            if probe.returncode or not output.strip().isdigit():
                raise ExtractionError('pdf_limits_unavailable')
            if int(output)>limit_kib:
                raise ExtractionError('pdf_resource_limit')
        except (OSError,asyncio.TimeoutError):
            raise ExtractionError('pdf_limits_unavailable') from None
        finally:
            if probe is not None and probe.returncode is None:
                try: probe.kill()
                except ProcessLookupError: pass
                await asyncio.shield(probe.wait())
        await asyncio.sleep(0.025)

@asynccontextmanager
async def _extraction_slot(ocr):
    """Keep one child total and give waiting normal extraction priority."""
    global _active_ocr, _normal_waiters
    if ocr:
        if _normal_waiters:raise ExtractionError('ocr_busy')
        try:await asyncio.wait_for(_pdf_slot.acquire(),OCR_ADMISSION_SECONDS)
        except asyncio.TimeoutError:raise ExtractionError('ocr_busy') from None
        if _normal_waiters:
            _pdf_slot.release()
            raise ExtractionError('ocr_busy')
    else:
        _normal_waiters+=1
        try:
            if _active_ocr is not None:_active_ocr.set()
            await _pdf_slot.acquire()
        finally:_normal_waiters-=1
    token=asyncio.Event() if ocr else None
    if ocr:_active_ocr=token
    try:yield token
    finally:
        if ocr and _active_ocr is token:_active_ocr=None
        _pdf_slot.release()

async def _bounded_io(proc, data, maximum):
    async def feed():
        try:
            for pos in range(0,len(data),65536):
                proc.stdin.write(data[pos:pos+65536])
                await proc.stdin.drain()
        except (BrokenPipeError,ConnectionResetError):
            pass
        finally:
            proc.stdin.close()
    writer=asyncio.create_task(feed())
    output=bytearray()
    try:
        while True:
            chunk=await proc.stdout.read(min(65536,maximum+1-len(output)))
            if not chunk:break
            output.extend(chunk)
            if len(output)>maximum:raise ExtractionError('file_output_limit')
        await writer
        await proc.wait()
        return bytes(output),None
    finally:
        if not writer.done():writer.cancel()
        await asyncio.gather(writer,return_exceptions=True)

async def _isolated(data, kind, *, full_access=False):
    limit=MAX_FULL_TEXT if full_access else MAX_TEXT
    office=kind in ('docx','xlsx','pptx');ocr=kind=='pdf_ocr'
    prefix='office' if office else 'ocr' if ocr else 'pdf'
    deadline=60 if ocr else PDF_DEADLINE
    command = ([sys.executable] if getattr(sys,'frozen',False) else
               [sys.executable,str(Path(__file__).resolve().parents[2]/'run.py')])
    proc = None
    io_task = monitor = preempt_task = None
    async with _extraction_slot(ocr) as preempt:
        try:
            if preempt is not None and preempt.is_set():raise ExtractionError('ocr_interrupted')
            proc = await asyncio.create_subprocess_exec(*command,('--extract-library-office-'+kind if office else '--extract-library-pdf-ocr' if ocr else '--extract-library-file'),
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,env={**_worker_environment(),'GAMJA_FILE_TEXT_LIMIT':str(limit)},close_fds=True)
            try:
                io_task=asyncio.create_task(_bounded_io(proc,data,2_500_000 if full_access else 512_000))
                if sys.platform=='darwin':
                    monitor=asyncio.create_task(_watch_memory(proc,512*1024 if ocr else 256*1024))
                    if preempt is not None:preempt_task=asyncio.create_task(preempt.wait())
                    watched={io_task,monitor}|({preempt_task} if preempt_task else set())
                    done,_=await asyncio.wait(watched,timeout=deadline,return_when=asyncio.FIRST_COMPLETED)
                    if not done: raise asyncio.TimeoutError()
                    if preempt_task is not None and preempt_task in done:raise ExtractionError('ocr_interrupted')
                    if monitor in done:
                        try: await monitor
                        except ExtractionError as exc: raise ExtractionError(exc.code.replace('pdf_',prefix+'_',1)) from None
                output,_ = await asyncio.wait_for(asyncio.shield(io_task),deadline)
            except asyncio.TimeoutError:
                raise ExtractionError(prefix+'_timeout') from None
            if proc.returncode != 0 or len(output)>(2_500_000 if full_access else 512_000):
                raise ExtractionError(prefix+'_processing_failed')
            try: value=json.loads(output)
            except (ValueError,UnicodeError): raise ExtractionError(prefix+'_processing_failed') from None
            if isinstance(value,dict) and set(value)=={'error'}:
                code=value['error']
                if code not in {prefix+'_limits_unavailable',prefix+'_encrypted',prefix+'_no_text',prefix+'_invalid',prefix+'_resource_limit',prefix+'_unavailable'}:
                    code=prefix+'_processing_failed'
                raise ExtractionError(code)
            if (not isinstance(value,dict) or set(value)!={'text','text_sha256','format','page_count','truncated','extractor'}
                    or not isinstance(value['text'],str) or not value['text'].strip()
                    or len(value['text'])>limit or value['format']!=('pdf' if ocr else kind)
                    or (kind in ('pdf','pdf_ocr','pptx') and (type(value['page_count']) is not int or value['page_count']<1))
                    or (kind in ('docx','xlsx') and value['page_count'] is not None)
                    or type(value['truncated']) is not bool or value['extractor']!=('macos-vision-ocr-v1' if ocr else OFFICE_EXTRACTOR if office else EXTRACTOR)
                    or hashlib.sha256(value['text'].encode()).hexdigest()!=value['text_sha256']):
                raise ExtractionError(prefix+'_processing_failed')
            return value
        except ExtractionError: raise
        except (OSError,ValueError): raise ExtractionError(prefix+'_processing_failed') from None
        finally:
            if preempt_task is not None:
                preempt_task.cancel()
                await asyncio.gather(preempt_task,return_exceptions=True)
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor,return_exceptions=True)
            if proc is not None and proc.returncode is None:
                try: proc.kill()
                except ProcessLookupError: pass
                # Reap the owned child even if the caller cancels its request.
                await asyncio.shield(proc.wait())
            if io_task is not None:
                if not io_task.done(): io_task.cancel()
                await asyncio.gather(io_task,return_exceptions=True)

async def extract(data: bytes, filename: str, *, full_access=False) -> dict:
    if type(data) is not bytes or len(data)>MAX_INPUT: raise ExtractionError('file_too_large')
    if not isinstance(filename,str): raise ExtractionError('format_unsupported')
    suffix=Path(filename).suffix.lower()
    if data.startswith(b'%PDF-'): return await _isolated(data,'pdf',full_access=full_access)
    if suffix in {'.docx','.xlsx','.pptx'}: return await _isolated(data,suffix[1:],full_access=full_access)
    if suffix=='.pdf': raise ExtractionError('pdf_invalid')
    if suffix not in _TEXT_EXTENSIONS: raise ExtractionError('format_unsupported')
    try: text=data.decode('utf-8-sig')
    except UnicodeError: raise ExtractionError('text_invalid') from None
    if any(ord(c)<32 and c not in '\r\n\t' for c in text): raise ExtractionError('text_invalid')
    if not text.strip(): raise ExtractionError('text_empty')
    return result(text,'utf8',limit=MAX_FULL_TEXT if full_access else MAX_TEXT)

async def extract_ocr(data: bytes, filename: str, *, full_access=False) -> dict:
    if type(data) is not bytes or len(data)>MAX_INPUT: raise ExtractionError('file_too_large')
    if not isinstance(filename,str) or not data.startswith(b'%PDF-'): raise ExtractionError('ocr_invalid')
    if sys.platform!='darwin': raise ExtractionError('ocr_unavailable')
    return await _isolated(data,'pdf_ocr',full_access=full_access)
