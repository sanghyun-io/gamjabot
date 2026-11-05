import asyncio,json,socket,struct
import pytest
from app.services.mcp_client import MCPClient,MCPChannelUnavailable

async def send(writer,obj):
    data=json.dumps(obj,separators=(',',':')).encode();writer.write(struct.pack('!I',len(data))+data);await writer.drain()
async def receive(reader):
    size=struct.unpack('!I',await reader.readexactly(4))[0];return json.loads(await reader.readexactly(size))

@pytest.mark.asyncio
async def test_authenticated_channel_concurrent_cancel_and_disconnect():
    a,b=socket.socketpair();a.setblocking(False);b.setblocking(False)
    reader,writer=await asyncio.open_connection(sock=b)
    await send(writer,{'version':1,'core_epoch':'a'*64,'max_frame_bytes':1048576,'max_in_flight':4})
    client=await MCPClient.from_fd(a.detach());assert client.available
    first=asyncio.create_task(client.start({'version':1}));req1=await receive(reader);assert req1['operation']=='start'
    second=asyncio.create_task(client.cancel({'version':1}));req2=await receive(reader);assert req2['operation']=='cancel'
    await send(writer,{'version':1,'request_id':req2['request_id'],'state':'cancel_requested'})
    assert (await second)['state']=='cancel_requested'
    await send(writer,{'version':1,'request_id':req1['request_id'],'state':'uncertain'})
    assert (await first)['state']=='uncertain'
    pending=asyncio.create_task(client.discover({'version':1}));await receive(reader);writer.close();await writer.wait_closed()
    with pytest.raises(MCPChannelUnavailable):await pending
    assert not client.available
    assert client.failure_reason=='channel_eof'
    await client.close()

@pytest.mark.asyncio
async def test_channel_protocol_failure_keeps_only_fixed_category():
    a,b=socket.socketpair();a.setblocking(False);b.setblocking(False)
    reader,writer=await asyncio.open_connection(sock=b)
    await send(writer,{'version':1,'core_epoch':'a'*64,'max_frame_bytes':1048576,'max_in_flight':4})
    client=await MCPClient.from_fd(a.detach())
    await send(writer,{'version':1,'request_id':42,'state':'failed','sensitive':'synthetic'})
    await client._reader_task
    assert not client.available and client.failure_reason=='channel_protocol'
    await client.close();writer.close();await writer.wait_closed()
