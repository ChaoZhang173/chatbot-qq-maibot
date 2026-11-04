"""One local round through real WebSocket and HTTP transports, with simulated ASR/TTS."""
from __future__ import annotations

import asyncio
import json
import secrets
import struct
import tempfile
import threading
from pathlib import Path

import websockets

from . import protocol as P
from .config import Config, token_hash
from .demo_backend import make_server
from .server import GatewayServer


async def run_smoke() -> dict:
    device, token, key = "demo-device", secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    backend = make_server(key, {device}, 0)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="passport-smoke-") as tmp:
            bindings = Path(tmp) / "bindings.json"
            bindings.write_text(json.dumps({"devices": {device: {"enabled": True, "token_sha256": token_hash(token)}}}))
            cfg = Config(port=0, allow_plaintext_loopback=True, bindings_path=str(bindings), test_mode=True,
                         backend_mode="http", backend_url=f"http://127.0.0.1:{backend.server_port}/v1/voice",
                         backend_api_key=key, pace_lead_ms=0)
            gateway = GatewayServer(cfg)
            await gateway.start()
            try:
                async with websockets.connect(f"ws://127.0.0.1:{gateway.port}/v1/voice",
                        additional_headers={"X-Device-Id": device, "Authorization": "Bearer " + token},
                        subprotocols=[P.SUBPROTOCOL], compression=None) as ws:
                    await ws.send(P.dumps({"v": 1, "type": "hello", "fw": "0.3.1"}))
                    ready = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    if ready.get("type") != "ready":
                        raise RuntimeError("gateway handshake failed")
                    # Synthetic non-silent PCM, never a real microphone recording.
                    pcm = struct.pack("<h", 1000) * 8000
                    await ws.send(P.dumps({"v": 1, "type": "start", "turn": 1,
                                           "hz": 16000, "ch": 1, "fmt": "pcm_s16le"}))
                    for seq, offset in enumerate(range(0, len(pcm), P.MAX_UP_PAYLOAD)):
                        await ws.send(P.AUDIO_HDR.pack(P.KIND_UP, 0, 1, seq) + pcm[offset:offset + P.MAX_UP_PAYLOAD])
                    await ws.send(P.dumps({"v": 1, "type": "end", "turn": 1, "bytes": len(pcm)}))
                    reply, audio_bytes = "", 0
                    async with asyncio.timeout(10):
                        while True:
                            raw = await ws.recv()
                            if isinstance(raw, bytes):
                                audio_bytes += len(raw) - P.AUDIO_HDR_LEN
                                continue
                            msg = json.loads(raw)
                            if msg["type"] == "error":
                                raise RuntimeError("gateway error: " + msg["code"])
                            if msg["type"] == "reply_text":
                                reply = msg["text"]
                            if msg["type"] == "audio_end":
                                if not reply.startswith("Demo echo: ") or audio_bytes != msg["bytes"] or not audio_bytes:
                                    raise RuntimeError("incomplete smoke round")
                                return {"ok": True, "transport": "WebSocket -> HTTP -> WebSocket",
                                        "speech": "simulated ASR/TTS", "reply": reply, "audio_bytes": audio_bytes}
            finally:
                await gateway.stop()
    finally:
        await asyncio.to_thread(backend.shutdown)
        backend.server_close()
        thread.join(timeout=2)


def main():
    print(json.dumps(asyncio.run(run_smoke()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
