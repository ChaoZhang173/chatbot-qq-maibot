import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from passport_gateway.birthday import pending, deliver

class BirthdayTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_is_targeted_and_opt_in(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'birthday-events.json'
            now = int(time.time())
            path.write_text(json.dumps({'schema': 1, 'events': {'girlfriend': {'event_id': now, 'expires_at': now + 60}}}))
            sent = []
            async def send(x): sent.append(json.loads(x))
            session = SimpleNamespace(ready=True, closing=False, active=None, fw='0.2.0-shunhuahua',
                cfg=SimpleNamespace(bindings_path=str(Path(d) / 'device-bindings.json')),
                device_id='other', _send_raw=send)
            await deliver(session)
            self.assertEqual(sent, [])
            session.device_id = 'girlfriend'
            session.fw = '0.1.0'
            await deliver(session)
            self.assertEqual(sent, [])
            session.fw = '0.2.0-shunhuahua'
            session.active = object()
            await deliver(session)
            self.assertEqual(sent, [])
            session.active = None
            await deliver(session)
            await deliver(session)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]['event_id'], now)
            self.assertIsNone(pending(path, 'girlfriend', now + 61))
            path.write_text(json.dumps({'schema': 1, 'events': {'girlfriend': {'event_id': now + 1, 'expires_at': 0}}}))
            await deliver(session)
            self.assertEqual(sent[-1]['expires_at'], 0)
    async def test_bad_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'birthday-events.json'
            for text in ('bad', '{}', '[]', '{"schema":1,"events":null}'):
                p.write_text(text)
                self.assertIsNone(pending(p, 'girlfriend', int(time.time())))

if __name__ == '__main__': unittest.main()
