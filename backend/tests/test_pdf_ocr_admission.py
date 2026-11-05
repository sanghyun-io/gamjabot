"""Concurrency regression: one extractor child; ordinary PDF wins over OCR."""
import asyncio
import io
import sys
import zipfile
import pytest
from app.services import library_file_text as service
from test_library_file_text import text_pdf

def docx():
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Synthetic document result</w:t></w:r></w:p></w:body></w:document>')
    return output.getvalue()

@pytest.mark.parametrize('kind',['pdf','docx'])
@pytest.mark.asyncio
async def test_ocr_busy_and_ordinary_preempts_owned_child(monkeypatch,kind):
    monkeypatch.setattr(service,'_pdf_slot',asyncio.Semaphore(1))
    original=asyncio.create_subprocess_exec
    started=asyncio.Event();children=[]
    async def spawn(*args,**kwargs):
        if '--extract-library-pdf-ocr' in args:
            proc=await original(sys.executable,'-c','import time; time.sleep(30)',**kwargs)
            children.append(proc);started.set();return proc
        if '--extract-library-file' in args or '--extract-library-office-docx' in args:
            assert children and children[0].returncode is not None
        return await original(*args,**kwargs)
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(service,'OCR_ADMISSION_SECONDS',0.05)
    first=asyncio.create_task(service.extract_ocr(text_pdf(),'sample.pdf'))
    await asyncio.wait_for(started.wait(),5)
    with pytest.raises(service.ExtractionError) as busy:
        await service.extract_ocr(text_pdf(),'sample.pdf')
    assert busy.value.code=='ocr_busy'
    normal=asyncio.create_task(service.extract(text_pdf() if kind=='pdf' else docx(),'sample.'+kind))
    with pytest.raises(service.ExtractionError) as interrupted:
        await first
    assert interrupted.value.code=='ocr_interrupted'
    result=await normal
    assert result['format']==kind and 'Synthetic document result' in result['text']
    assert len(children)==1 and children[0].returncode is not None

@pytest.mark.asyncio
async def test_waiting_normal_cannot_be_overtaken_by_queued_ocr(monkeypatch):
    monkeypatch.setattr(service,'_pdf_slot',asyncio.Semaphore(1))
    original=asyncio.create_subprocess_exec
    started=asyncio.Event();children=[]
    async def spawn(*args,**kwargs):
        if '--extract-library-pdf-ocr' in args:
            proc=await original(sys.executable,'-c','import time; time.sleep(30)',**kwargs)
            children.append(proc);started.set();return proc
        return await original(*args,**kwargs)
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(service,'OCR_ADMISSION_SECONDS',1)
    first=asyncio.create_task(service.extract_ocr(text_pdf(),'sample.pdf'))
    await asyncio.wait_for(started.wait(),5)
    second=asyncio.create_task(service.extract_ocr(text_pdf(),'sample.pdf'))
    await asyncio.sleep(0)
    normal=asyncio.create_task(service.extract(text_pdf(),'sample.pdf'))
    with pytest.raises(service.ExtractionError) as interrupted:await first
    assert interrupted.value.code=='ocr_interrupted'
    with pytest.raises(service.ExtractionError) as blocked:await second
    assert blocked.value.code=='ocr_busy'
    assert (await normal)['format']=='pdf'
    assert len(children)==1 and children[0].returncode is not None
