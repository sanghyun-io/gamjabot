"""Owned local effects; ToolBroker remains the only authorization boundary."""

from app.services.generated_files import GeneratedFiles
from app.services.library_uploads import LibraryUploads
from app.services.voice_memo_artifacts import VoiceMemoArtifacts
from app.services.tools.process import ProcessExecutor
from app.services.tools.files import execute_file
from app.services.tools.browser import BrowserExecutor
from app.services.tools.desktop import DesktopExecutor, NativeDesktopClient
import os
import asyncio
from pathlib import Path
from app.services.tool_broker import ActionResult


class LocalExecutor:
    def __init__(self, sessions=None, *, browser_engine=None, database_path=None,
                 native_endpoint=None, native_key=None, owner_epochs=None):
        app_data = Path(database_path).resolve().parent if database_path and database_path != ':memory:' else None
        self.generated_files = GeneratedFiles(app_data)
        self.library_uploads = LibraryUploads(app_data)
        self.voice_memos = VoiceMemoArtifacts(app_data)
        self.processes = ProcessExecutor(sessions, owner_epochs=owner_epochs)
        self.browser = BrowserExecutor(browser_engine or (
            "bundled" if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") else "installed"), sessions,
            owner_epochs=owner_epochs,
            app_data=Path(database_path).resolve().parent if database_path and database_path != ':memory:' else None)
        endpoint = native_endpoint or os.environ.get("GAMJA_DESKTOP_IPC_ENDPOINT")
        key = native_key or os.environ.get("GAMJA_DESKTOP_IPC_KEY")
        try:
            native = NativeDesktopClient(endpoint, key) if endpoint and key else None
        except (TypeError, ValueError):
            # Computer Use is optional; malformed helper readiness cannot stop
            # browser, process, file, provider, or room initialization.
            native = None
        self.desktop = DesktopExecutor(database_path or ":memory:", native=native,
                                       sessions=sessions, owner_epochs=owner_epochs)
        self.sessions=sessions
        self._scope_calls={}
        self._scope_stops={}

    @property
    def scoped_execution_ready(self):
        return self.desktop.scoped_execution_ready

    async def prepare_scoped_execution(self):
        return await self.desktop.prepare_scoped_execution()

    async def __call__(self, action, grant, cancel):
        from app.services.resource_authority import require_scope_start
        binding=require_scope_start(self.sessions,action,grant)
        if binding is not None and not self.scoped_execution_ready:
            return ActionResult('denied','Scoped local execution is unavailable.',error_code='native_capability_unavailable')
        task=asyncio.current_task()
        if binding is not None:self._scope_calls[id(task)]=(binding,cancel,task)
        try:return await self._execute(action,grant,cancel)
        finally:self._scope_calls.pop(id(task),None)

    async def _execute(self, action, grant, cancel):
        if self.browser.profiles.user_in_control() and (action.family == 'screenshot' or action.family.startswith(('pointer_', 'keyboard_'))):
            return ActionResult('denied', 'Owner controls a browser; desktop input and capture wait until control is returned.')
        if action.family in ('read_file','write_file','move_file','delete_file'):
          for private_root in (self.browser.profiles.root, self.generated_files.root, self.library_uploads.root, self.voice_memos.root):
            if not private_root: continue
            root = private_root.resolve()
            for key in ('path','source','destination'):
                value = action.arguments.get(key)
                if isinstance(value,str):
                    target = Path(value).resolve()
                    if target == root or target.is_relative_to(root) or root.is_relative_to(target):
                        return ActionResult('denied', 'Private application data is not available through file tools.')
        if action.family in ("start_process", "cancel_process"):
            return await self.processes(action, grant, cancel)
        if action.family in ("read_file", "write_file", "move_file", "delete_file"):
            return await execute_file(action, grant, cancel)
        if action.family == 'request_vault_login':
            from app.services.vault_profile_handoff import prepare
            return await prepare(self,action,grant,cancel)
        if action.family.startswith("browser_"):
            return await self.browser(action, grant, cancel)
        if action.family == "screenshot" or action.family.startswith(("pointer_", "keyboard_")):
            return await self.desktop(action, grant, cancel)
        raise ValueError("No executor for tool family")

    async def wait_browser_profile(self,action,cancel):
        await self.browser.profiles.wait_for_agent(action,cancel)

    async def close(self):
        proxies=getattr(self.browser.profiles,'handoff_proxies',{})
        for proxy in list(proxies.values()):await proxy.close()
        proxies.clear()
        await self.processes.close()
        await self.browser.close()
        await self.desktop.close()
        pending=[task for task in self._scope_stops.values() if not task.done()]
        if pending:await asyncio.wait_for(asyncio.shield(asyncio.gather(*pending)),15)

    async def stop_room(self, room_id, agent_id=None):
        await self.processes.stop_room(room_id, agent_id)
        await self.browser.stop_room(room_id, agent_id)

    async def stop_grant(self, grant_id):
        await self.processes.stop_grant(grant_id)
        await self.browser.stop_grant(grant_id)

    async def release_desktop_turn(self, room_id, agent_id, *, generation=None, workflow_id=None):
        await self.desktop.release_turn(room_id, agent_id,generation=generation,workflow_id=workflow_id)

    async def park_completed_browser_turn(self, room_id, agent_id, trigger_event_id,
                                          generation, execution_binding, workflow_ids):
        await self.browser.park_completed(room_id, agent_id, trigger_event_id,
                                          generation, execution_binding, workflow_ids)

    async def stop_scopes(self,bindings):
        from app.services.resource_binding import revocation_map
        revoked=revocation_map(bindings);key=tuple(sorted(revoked.items()))
        if not key:return []
        task=self._scope_stops.get(key)
        if task is not None and task.done() and not task.cancelled():
            try:
                if any(row['state']!='settled' for row in task.result()):task=None
            except Exception:task=None
        if task is None:
            # Withdraw active starts before waiting for native acknowledgement.
            starts=[]
            for proof,cancel,active in tuple(self._scope_calls.values()):
                if proof.affected(revoked):cancel.set();starts.append(active)
            task=asyncio.create_task(self._stop_scopes(key,starts))
            self._scope_stops[key]=task
        try:
            return await asyncio.wait_for(asyncio.shield(task),.5)
        except Exception:
            return [{'scope_id':scope,'epoch':epoch,'state':'unknown','code':'resource_stop_unconfirmed'} for scope,epoch in key]

    async def _stop_scopes(self,key,starts):
        bindings=[{'scope_id':scope,'epoch':epoch} for scope,epoch in key]
        native=asyncio.create_task(self.desktop.stop_scopes(bindings))
        try:
            if starts:await asyncio.gather(*starts,return_exceptions=True)
            await self.processes.stop_scopes(bindings)
            await self.browser.stop_scopes(bindings)
            return await native
        finally:
            if not native.done():await native
