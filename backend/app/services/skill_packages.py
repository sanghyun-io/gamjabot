"""Local instruction-only packages. Metadata and digests do not assert authenticity."""
import json
from hashlib import sha256
from uuid import uuid4
from sqlalchemy import text
from app.models.owner_skills import OwnerSkill,OwnerSkillVersion,OwnerSkillCommand
from app.services import owner_skills

BUILTINS = (
    {'package_id':'gamja.meeting-notes','package_version':'1.0.0','author':'Gamja','license':'CC0-1.0',
     'name':'Meeting notes','description':'Turn supplied meeting notes into decisions and follow-ups.',
     'content':'Use only the supplied notes. Summarize decisions, open questions, and follow-ups. Identify owners and deadlines only when stated; mark missing details as unknown.'},
    {'package_id':'gamja.document-outline','package_version':'1.0.0','author':'Gamja','license':'CC0-1.0',
     'name':'Document outline','description':'Make an outline from the material supplied by the Owner.',
     'content':'Organize the supplied material into a clear outline. Preserve the stated purpose and constraints, identify missing information, and ask before inventing facts.'},
)

def canonical(manifest):
    return json.dumps(manifest,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')

def digest(manifest):return sha256(canonical(manifest)).hexdigest()
def preview(value):
    manifest=value.model_dump()
    return {'version':1,'manifest':manifest,'digest':digest(manifest)}
def catalog(q):
    if len(q)>120:owner_skills.fail(422,'skill_package_query_invalid')
    q=q.casefold();return {'version':1,'items':[{'version':1,'manifest':dict(x),'digest':digest(x)} for x in BUILTINS if q in (x['name']+' '+x['description']+' '+x['package_id']).casefold()]}

def install(db,owner_id,value,key):
    owner_skills.validate_key(key);manifest=value.manifest.model_dump();actual=digest(manifest)
    if actual!=value.digest:owner_skills.fail(409,'skill_package_digest_changed')
    signature=sha256(canonical({'operation':'package_install','manifest':manifest,'digest':actual})).hexdigest()
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        old=db.get(OwnerSkillCommand,(owner_id,key))
        if old:
            if old.request_digest!=signature:owner_skills.fail(409,'skill_idempotency_conflict')
            response=old.response;db.commit();return response
        if db.query(OwnerSkill).filter_by(owner_id=owner_id).count()>=200:owner_skills.fail(409,'skill_library_limit')
        origin='builtin' if any(manifest==entry for entry in BUILTINS) else 'manual_manifest'
        provenance={k:manifest[k] for k in ('package_id','package_version','author','license')}
        provenance.update(digest=actual,origin=origin,original_content_digest=sha256(manifest['content'].encode('utf-8')).hexdigest())
        when=owner_skills.now();row=OwnerSkill(skill_id=str(uuid4()),owner_id=owner_id,revision=1,name=manifest['name'],
            description=manifest['description'],content=manifest['content'],active=False,source=None,package_provenance=provenance,
            created_at=when,updated_at=when)
        db.add(row);db.flush();response=owner_skills.dto(row)
        db.add(OwnerSkillVersion(skill_id=row.skill_id,revision=1,snapshot=response))
        db.add(OwnerSkillCommand(owner_id=owner_id,command_key=key,request_digest=signature,response=response))
        db.commit();return response
    except BaseException:db.rollback();raise
