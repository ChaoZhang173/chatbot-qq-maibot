"""Local-only active owner text delivery, reusing the existing QQ adapter.

No public listener, new credential, passive msg_id, or LLM tool is introduced.
The caller must be root/the adapter service UID; the unified core verifies owner
identity and this gateway independently requires the existing private binding.
"""
import asyncio
import json
import os
from pathlib import Path
import re
import socket
import struct
from urllib.parse import quote

PATH = Path('/srv/maibot/unified/qq-paid-owner.sock')

async def start(adapter):
    if PATH.exists():
        try:
            _, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(PATH)), 1)
        except (ConnectionRefusedError, FileNotFoundError):
            PATH.unlink(missing_ok=True)
        else:
            writer.close()
            await writer.wait_closed()
            raise RuntimeError('paid_owner_gateway_already_running')

    async def handle(reader, writer):
        response = {'status':'rejected','reason':'local_validation_failed'}
        try:
            sock = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in (0, os.getuid()):
                raise PermissionError('peer_denied')
            raw = await asyncio.wait_for(reader.readline(), 3)
            if not raw.endswith(b'\n') or len(raw) > 12000:
                raise ValueError('packet_limit')
            request = json.loads(raw)
            target, text = request.get('target',''), request.get('text','')
            if not isinstance(target,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',target):
                raise ValueError('target_invalid')
            if not isinstance(text,str) or not 1 <= len(text) <= 2200:
                raise ValueError('text_limit')
            gate = await adapter._private_gate({'scene':'private','actor':target,'outbound':True})
            if not gate.get('allow') or not adapter._ready:
                raise ValueError('private_binding_or_gateway_unavailable')
            mode=request.get('mode')
            if mode not in ('active_probe','passive_window'):
                raise ValueError('notification_mode_not_authorized')
            passive={}
            if mode=='passive_window':
                route_target=adapter._id_map.get('user',{}).get(target,target)
                passive=await adapter._claim_passive_fields('private',route_target,'')
                if not passive.get('msg_id'):
                    response={'status':'rejected','reason':'owner_reply_window_unavailable'}
                    raise ValueError('passive_window_required')
            # Once the HTTP call starts, a timeout or other failure is uncertain.
            # Never automatically resend a possibly accepted active message.
            response = {'status':'unknown','reason':'active_send_uncertain'}
            result = await asyncio.wait_for(adapter._api_post(
                '/v2/users/'+quote(target,safe='')+'/messages',
                {'msg_type':0,'content':text,**passive}), 20)
            if result.get('id'):
                response = {'status':'sent','remote_id':str(result['id']),'send_mode':'active' if mode=='active_probe' else 'passive_window'}
        except Exception as exc:
            # The existing adapter raises this exact form on a received HTTP
            # rejection. Timeout, cancellation and HTTP 5xx remain uncertain.
            match=re.match(r'QQ 接口 messages 失败: HTTP (400|401|403|404|429) ',str(exc))
            if match:response={'status':'rejected','reason':'qq_http_'+match[1]}
            # Never log credentials, recipient IDs, or response bodies.
        try:
            writer.write(json.dumps(response).encode()+b'\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, str(PATH), limit=12001)
    PATH.chmod(0o600)
    return server

async def stop(server):
    if server:
        server.close()
        await server.wait_closed()
        PATH.unlink(missing_ok=True)
