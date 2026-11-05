"""Bounded authenticated inherited Core channel for MCP operations."""
import asyncio,json,re,socket,struct

class MCPChannelUnavailable(RuntimeError):pass

class MCPClient:
    def __init__(self,reader,writer,epoch,max_frame,max_in_flight):
        self.reader=reader;self.writer=writer;self.core_epoch=epoch;self.max_frame=max_frame
        self.available=True;self.failure_reason=None;self._counter=0;self._pending={};self._write_lock=asyncio.Lock()
        # Reserve one negotiated in-flight slot for cancellation/revocation,
        # even when ordinary short controls or long workers are stalled.
        self._ordinary=asyncio.Semaphore(max_in_flight-1)
        self._long=asyncio.Semaphore(max(1,max_in_flight-1));self._control=asyncio.Lock()
        self._urgent=asyncio.Semaphore(1)
        self._reader_task=asyncio.create_task(self._read_loop())
    @classmethod
    async def from_fd(cls,fd):
        sock=socket.socket(fileno=fd);sock.setblocking(False);writer=None
        try:
            reader,writer=await asyncio.open_connection(sock=sock)
            async with asyncio.timeout(3):
                length=struct.unpack('!I',await reader.readexactly(4))[0]
                if not 1<=length<=4096:raise ValueError()
                greeting=json.loads(await reader.readexactly(length))
            if (not isinstance(greeting,dict) or set(greeting)!={'version','core_epoch','max_frame_bytes','max_in_flight'}
                or type(greeting['version']) is not int or greeting['version']!=1 or not isinstance(greeting['core_epoch'],str)
                or not re.fullmatch(r'[a-f0-9]{64}',greeting['core_epoch'])
                or type(greeting['max_frame_bytes']) is not int or not 1024<=greeting['max_frame_bytes']<=1048576
                or type(greeting['max_in_flight']) is not int or not 2<=greeting['max_in_flight']<=4):raise ValueError()
            return cls(reader,writer,greeting['core_epoch'],greeting['max_frame_bytes'],greeting['max_in_flight'])
        except BaseException:
            if writer is not None:
                writer.close()
                try:await writer.wait_closed()
                except Exception:pass
            else:sock.close()
            raise
    async def _read_loop(self):
        try:
            while self.available:
                size=struct.unpack('!I',await self.reader.readexactly(4))[0]
                if not 1<=size<=self.max_frame:raise ValueError()
                msg=json.loads(await self.reader.readexactly(size))
                if not isinstance(msg,dict) or msg.get('version')!=1 or type(msg.get('request_id')) is not int or not isinstance(msg.get('state'),str) or set(msg)-{'version','request_id','state','code','nonce','result'}:raise ValueError()
                future=self._pending.pop(msg['request_id'],None)
                if future is None:raise ValueError()
                if not future.done():future.set_result({k:v for k,v in msg.items() if k not in ('version','request_id')})
        except (Exception,asyncio.CancelledError) as error:
            # Retain only an allowlisted category, never frames, payloads or exception text.
            self.failure_reason=('channel_eof' if isinstance(error,asyncio.IncompleteReadError) else
                'channel_protocol' if isinstance(error,ValueError) else
                'channel_closed' if isinstance(error,asyncio.CancelledError) else 'channel_io')
            self.available=False
            for future in self._pending.values():
                if not future.done():future.set_exception(MCPChannelUnavailable('Core MCP channel closed'))
            self._pending.clear()
            self.writer.close()
    async def _call(self,operation,payload,wire=None):
        if not self.available:raise MCPChannelUnavailable('Core MCP channel unavailable')
        self._counter+=1;rid=self._counter
        frame=json.dumps({'version':1,'request_id':rid,'operation':operation,'payload':payload},ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
        if len(frame)>self.max_frame:raise MCPChannelUnavailable('Core MCP request too large')
        future=asyncio.get_running_loop().create_future();self._pending[rid]=future
        sent=False
        try:
            async with self._write_lock:
                if not self.available:raise MCPChannelUnavailable('Core MCP channel unavailable')
                # Treat any failure after write begins as potentially sent.
                sent=True
                if wire is not None:wire['sent']=True
                self.writer.write(struct.pack('!I',len(frame))+frame);await self.writer.drain()
            return await asyncio.shield(future)
        except BaseException:
            # A cancelled waiter keeps the request ID until the reply arrives;
            # this avoids treating a legitimate late reply as channel corruption.
            if not sent or not self.available:
                self._pending.pop(rid,None)
                if not future.done():future.cancel()
            elif not future.done():
                future.add_done_callback(lambda item: None if item.cancelled() else item.exception())
            raise
    async def _with_slot(self,slot,operation,payload):
        await slot.acquire()
        wire={'sent':False}
        try:
            task=asyncio.create_task(self._call(operation,payload,wire))
        except BaseException:
            slot.release()
            raise
        release_later=False
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancellation does not prove the Core has finished. Keep this
            # slot occupied until its late response or channel shutdown.
            release_later=True
            if not wire['sent']:task.cancel()
            def done(future):
                slot.release()
                if not future.cancelled():future.exception()
            task.add_done_callback(done)
            raise
        finally:
            if not release_later:slot.release()

    async def call(self,operation,payload):
        if operation in ('speech_cancel','scope_revoke','shared_revoke'):
            return await self._with_slot(self._urgent,operation,payload)
        guard=self._long if operation in ('start','discover','speech_start','slack_reply') else self._control
        async with guard:
            return await self._with_slot(self._ordinary,operation,payload)
    async def configure(self,p):return await self.call('configure',p)
    async def bind(self,p):return await self.call('bind',p)
    async def revoke(self,p):return await self.call('revoke',p)
    async def issue(self,p):return await self.call('issue',p)
    async def start(self,p):return await self.call('start',p)
    async def cancel(self,p):return await self.call('cancel',p)
    async def discover(self,p):return await self.call('discover',p)
    async def credential_status(self,p):return await self.call('credential_status',p)
    async def speech_capabilities(self,p):return await self.call('speech_capabilities',p)
    async def speech_issue(self,p):return await self.call('speech_issue',p)
    async def speech_start(self,p):return await self.call('speech_start',p)
    async def speech_read(self,p):return await self.call('speech_read',p)
    async def speech_cancel(self,p):return await self.call('speech_cancel',p)
    async def scope_revoke(self,p):return await self.call('scope_revoke',p)
    async def shared_capabilities(self,p):return await self.call('shared_capabilities',p)
    async def shared_authorize(self,p):return await self.call('shared_authorize',p)
    async def shared_revoke(self,p):return await self.call('shared_revoke',p)
    async def slack_configure(self,p):return await self.call('slack_configure',p)
    async def slack_status(self,p):return await self.call('slack_status',p)
    async def slack_poll(self,p):return await self.call('slack_poll',p)
    async def slack_ack(self,p):return await self.call('slack_ack',p)
    async def slack_reply(self,p):return await self.call('slack_reply',p)
    async def close(self):
        self.available=False;self.writer.close();self._reader_task.cancel()
        await asyncio.gather(self._reader_task,return_exceptions=True)
        try:await self.writer.wait_closed()
        except Exception:pass
