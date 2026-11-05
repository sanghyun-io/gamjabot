from types import SimpleNamespace
from app.models.collaboration import RoomEvent
from app.services.routine_persistence import metadata

ID='c8a0e5a5-327a-40d3-bdc6-b2ab600a70e3'
OTHER='e8a0e5a5-327a-40d3-bdc6-b2ab600a70e3'

class Query:
    def __init__(self,rows): self.rows=rows
    def filter_by(self,**kw):
        self.rows=[r for r in self.rows if all(getattr(r,k)==v for k,v in kw.items())];return self
    def order_by(self,*args):return self
    def limit(self,n):self.rows=self.rows[:n];return self
    def all(self):return self.rows

class DB:
    def __init__(self,root):
        self.root=root
        self.rows=[SimpleNamespace(owner_id=1,agent_id=14,deleted_at=None,routine_id=ID,revision=2,name='One',schedule={},enabled=False,instruction='owned instruction'),
                   SimpleNamespace(owner_id=2,agent_id=14,deleted_at=None,routine_id=OTHER,revision=1,name='Other',schedule={},enabled=False,instruction='other instruction')]
    def query(self,model):return Query(self.rows[:])
    def get(self,model,key):return self.root if model is RoomEvent and key=='root' else None

def root(content,actor='user',owner=1,room=17):
    return SimpleNamespace(room_id=room,actor_type=actor,actor_id=owner,kind='message',payload={'content':content})

def catalog(event):return metadata(DB(event),1,14,trigger_event_id='root',room_id=17)

def test_exact_owned_reference_is_bounded_data():
    rows=catalog(root('Please test '+ID));assert len(rows)==1 and rows[0]['instruction']=='owned instruction'
    assert len(rows[0]['instruction'])<=4000

def test_peer_other_owner_unmentioned_and_embedded_identifiers_excluded():
    for event in (root(ID,actor='agent'),root(ID,owner=2),root(ID,room=16),root(OTHER),root('no ID'),root('a'+ID)):
        assert all('instruction' not in item for item in catalog(event))

def test_oversized_instruction_is_not_injected():
    db=DB(root(ID));db.rows[0].instruction='x'*4001
    assert 'instruction' not in metadata(db,1,14,trigger_event_id='root',room_id=17)[0]
