"""Build guarded additions against the inspected baseline; never edit it in place."""
import argparse
import ast
import difflib
import hashlib
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]

def replace_once(text, old, new):
    if text.count(old)!=1:raise ValueError('baseline_anchor_changed')
    return text.replace(old,new,1)

def build(baseline, output):
    hashes=json.loads((ROOT/'evidence/server-parity.json').read_text())['source_hashes']
    changes={
        'maibot_unified_core/service.py':[
            ('self.commands=None;self.command_worker=None;self.media=None;self.private_access=None',
             'self.commands=None;self.command_worker=None;self.media=None;self.private_access=None;self.paid=None'),
            ('        self.commands=CommandQueue(self)\n',
             '        self.commands=CommandQueue(self)\n        from .paid_bridge import PaidBridge\n        self.paid=PaidBridge(self)\n'),
            ("        op=r.get('op')\n        if op=='qq_private_gate':",
             "        op=r.get('op')\n        if isinstance(op,str) and op.startswith('paid_'):\n            if not admin:raise PermissionError('admin_only')\n            return await self.paid.dispatch(r)\n        if op=='qq_private_gate':"),
            ("        if self.media:self.media.capture(m)\n",
             "        if self.paid and await self.paid.capture(m):return\n        if self.media:self.media.capture(m)\n"),
            ("    async def record_qq_pending(self,message):\n        m,info,user,session=self.qq_fields(message)\n",
             "    async def record_qq_pending(self,message):\n        m,info,user,session=self.qq_fields(message)\n        if self.paid and session in self.paid.in_flight:return\n"),
            ("    async def record_qq_sent(self,message,sent):\n        m,info,user,session=self.qq_fields(message)\n",
             "    async def record_qq_sent(self,message,sent):\n        m,info,user,session=self.qq_fields(message)\n        if self.paid and session in self.paid.in_flight:return\n"),
        ],
        'qq_official_adapter/plugin.py':[
            ('        self._assign_admin_ids: List[str] = []\n',
             '        self._assign_admin_ids: List[str] = []\n        self._paid_owner_server = None\n        self._paid_owner_start = None\n'),
            ('        self._worker = asyncio.create_task(self._run(), name="qq-official-adapter")\n',
             '        self._worker = asyncio.create_task(self._run(), name="qq-official-adapter")\n        self._paid_owner_start = asyncio.create_task(self._start_paid_owner_gateway())\n'),
            ('    async def _stop(self) -> None:\n',
             '''    async def _start_paid_owner_gateway(self):
        try:
            from .paid_owner_gateway import start
            self._paid_owner_server = await start(self)
        except Exception:
            self.ctx.logger.warning('paid_owner_gateway_unavailable')

    async def _stop(self) -> None:
        if self._paid_owner_start:
            self._paid_owner_start.cancel()
            with suppress(asyncio.CancelledError):
                await self._paid_owner_start
            self._paid_owner_start = None
        if self._paid_owner_server:
            from .paid_owner_gateway import stop
            await stop(self._paid_owner_server)
            self._paid_owner_server = None
'''),
        ],
    }
    output.mkdir(parents=True,exist_ok=True)
    manifest={};patch=[]
    for name, replacements in changes.items():
        source=baseline/'extensions'/name
        raw=source.read_bytes();expected=hashes['extensions/'+name]
        if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('source_hash_changed:'+name)
        old=raw.decode();new=old
        for a,b in replacements:new=replace_once(new,a,b)
        ast.parse(new)
        target=output/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(new)
        manifest[name]={'before_sha256':expected,'after_sha256':hashlib.sha256(new.encode()).hexdigest()}
        patch.extend(difflib.unified_diff(old.splitlines(True),new.splitlines(True),'a/extensions/'+name,'b/extensions/'+name))
    for source,name in [('paid_bridge.py','maibot_unified_core/paid_bridge.py'),('paid_owner_gateway.py','qq_official_adapter/paid_owner_gateway.py')]:
        raw=(ROOT/'integration'/source).read_bytes();ast.parse(raw)
        (output/name).write_bytes(raw)
        manifest[name]={'before_sha256':None,'after_sha256':hashlib.sha256(raw).hexdigest()}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    (ROOT/'integration/changes.patch').write_text(''.join(patch))
    return manifest

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('baseline',type=Path);p.add_argument('output',type=Path)
    a=p.parse_args();print(json.dumps({'files':len(build(a.baseline,a.output)),'checked':True}))
