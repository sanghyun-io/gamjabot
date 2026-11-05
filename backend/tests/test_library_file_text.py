"""Real disposable PDF workers; no app, account, browser or provider calls."""
import asyncio
import io
import hashlib
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from app.services import library_file_text as service

def text_pdf(text='Synthetic document result 731'):
    stream=('BT /F1 14 Tf 20 80 Td ('+text+') Tj ET').encode()
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 160] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    data=b'%PDF-1.4\n';offsets=[]
    for i,obj in enumerate(objects,1):offsets.append(len(data));data+=str(i).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
    pos=len(data);data+=b'xref\n0 6\n0000000000 65535 f \n'+b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets)
    return data+b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(pos).encode()+b'\n%%EOF\n'

@pytest.mark.asyncio
async def test_utf8_bounds_and_explicit_unsupported_inputs():
    out=await service.extract(('\ufeff'+('가'*40_001)).encode(),'notes.md')
    assert out['format']=='utf8' and len(out['text'])==40_000 and out['truncated']
    assert out['text_sha256']==hashlib.sha256(out['text'].encode()).hexdigest()
    for body,name,code in [(b'\xff','bad.csv','text_invalid'),(b'\x00','bad.txt','text_invalid'),
                            (b'','empty.txt','text_empty'),(b'raw','data.exe','format_unsupported')]:
        with pytest.raises(service.ExtractionError) as e:await service.extract(body,name)
        assert e.value.code==code

@pytest.mark.asyncio
async def test_pdf_extracts_in_real_owned_child_without_app_auth():
    out=await service.extract(text_pdf(),'report.pdf')
    assert 'Synthetic document result 731' in out['text']
    assert out['format']=='pdf' and out['page_count']==1 and not out['truncated']
    assert out['extractor']==service.EXTRACTOR

@pytest.mark.asyncio
@pytest.mark.parametrize('kind,code',[('blank','pdf_no_text'),('encrypted','pdf_encrypted'),('malformed','pdf_invalid')])
async def test_pdf_unsupported_states_are_explicit(kind,code):
    if kind=='malformed':data=b'%PDF-1.4\nbroken'
    else:
        writer=PdfWriter();writer.add_blank_page(width=100,height=100)
        if kind=='encrypted':writer.encrypt('synthetic-password')
        dest=io.BytesIO();writer.write(dest);data=dest.getvalue()
    with pytest.raises(service.ExtractionError) as e:await service.extract(data,'report.pdf')
    assert e.value.code==code

@pytest.mark.asyncio
async def test_compressed_pdf_stream_limit_prevents_large_decode():
    writer=PdfWriter();page=writer.add_blank_page(width=100,height=100)
    stream=DecodedStreamObject();stream.set_data(b' '*2_000_001)
    page[NameObject('/Contents')]=writer._add_object(stream.flate_encode())
    output=io.BytesIO();writer.write(output)
    with pytest.raises(service.ExtractionError) as e:await service.extract(output.getvalue(),'large.pdf')
    assert e.value.code=='pdf_resource_limit'

@pytest.mark.asyncio
async def test_timeout_reaps_the_owned_process(monkeypatch):
    spawned=[];original=asyncio.create_subprocess_exec
    async def create(*args,**kwargs):
        proc=await original(*args,**kwargs);spawned.append(proc);return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',create);monkeypatch.setattr(service,'PDF_DEADLINE',0.001)
    with pytest.raises(service.ExtractionError) as e:await service.extract(text_pdf(),'report.pdf')
    assert e.value.code=='pdf_timeout' and len(spawned)==1 and spawned[0].returncode is not None

@pytest.mark.asyncio
async def test_cancel_reaps_the_owned_process(monkeypatch):
    spawned=[];ready=asyncio.Event();original=asyncio.create_subprocess_exec
    async def create(*args,**kwargs):
        proc=await original(*args,**kwargs);spawned.append(proc);ready.set();return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',create)
    task=asyncio.create_task(service.extract(text_pdf(),'report.pdf'));await ready.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert spawned[0].returncode is not None
