"""Normalize provider bodies before any bytes reach the PCM-only device."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from .base import ProviderError

log = logging.getLogger('passport.audio_format')
MAX_ENCODED_BYTES = 8 * 1024 * 1024
MAX_PCM_BYTES = 60 * 24000 * 2


def encoded_format(prefix: bytes, content_type: str) -> str | None:
    mime = content_type.lower().split(';', 1)[0].strip()
    if prefix.startswith(b'ID3'):
        return 'mp3'
    if prefix.startswith(b'RIFF') and prefix[8:12] == b'WAVE':
        return 'wav'
    if prefix.startswith(b'OggS'):
        return 'ogg'
    if prefix.startswith(b'fLaC'):
        return 'flac'
    known = {'audio/mpeg': 'mp3', 'audio/mp3': 'mp3', 'audio/wav': 'wav',
             'audio/x-wav': 'wav', 'audio/wave': 'wav', 'audio/ogg': 'ogg',
             'audio/opus': 'ogg', 'audio/flac': 'flac'}
    if mime in known:
        return known[mime]
    if mime not in ('', 'application/octet-stream', 'binary/octet-stream',
                    'audio/pcm', 'audio/x-pcm', 'audio/raw'):
        raise ProviderError('tts_failed', 'unexpected speech response content type')
    return None


async def decode_audio(data: bytes, fmt: str) -> bytes:
    """Bounded subprocess, no file/network inputs, stopped on cancellation."""
    try:
        proc = await asyncio.create_subprocess_exec(
            '/usr/bin/ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-protocol_whitelist', 'pipe', '-f', fmt, '-i', 'pipe:0',
            '-vn', '-t', '61', '-ac', '1', '-ar', '24000', '-f', 's16le', 'pipe:1',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except OSError as exc:
        raise ProviderError('tts_failed', 'audio decoder unavailable') from exc
    try:
        pcm, _ = await asyncio.wait_for(proc.communicate(data), 8)
        if proc.returncode != 0 or not pcm or len(pcm) > MAX_PCM_BYTES or len(pcm) % 2:
            raise ProviderError('tts_failed', 'invalid or oversized encoded speech')
        return pcm
    except asyncio.TimeoutError as exc:
        raise ProviderError('tts_timeout', 'audio decoding timed out') from exc
    finally:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


async def pcm_chunks(response):
    """Preserve raw streaming; decode a bounded encoded response when needed.

    A live CosyVoice request for PCM returned an ID3/MP3 body on 2026-11-11.
    Even a successful HTTP response must not be treated as proof of PCM.
    """
    prefix = bytearray()
    encoded = bytearray()
    fmt = None
    decided = False
    carry = b''
    total = 0
    async for chunk in response.aiter_bytes(4096):
        total += len(chunk)
        if total > MAX_ENCODED_BYTES:
            raise ProviderError('tts_failed', 'speech response too large')
        if not decided:
            prefix.extend(chunk)
            if len(prefix) < 12:
                continue
            fmt = encoded_format(bytes(prefix[:12]), response.headers.get('content-type', ''))
            chunk = bytes(prefix)
            prefix.clear()
            decided = True
        if fmt:
            encoded.extend(chunk)
        else:
            data = carry + chunk
            carry = data[-1:] if len(data) % 2 else b''
            data = data[:len(data) & ~1]
            if data:
                yield data
    if not decided:
        fmt = encoded_format(bytes(prefix), response.headers.get('content-type', ''))
        if fmt:
            encoded.extend(prefix)
        elif prefix:
            if len(prefix) % 2:
                raise ProviderError('tts_failed', 'incomplete PCM sample')
            yield bytes(prefix)
    if fmt:
        log.warning('speech response is %s despite PCM request; converting to s16le/24000/mono', fmt)
        pcm = await decode_audio(bytes(encoded), fmt)
        for start in range(0, len(pcm), 4096):
            yield pcm[start:start + 4096]
    elif carry:
        raise ProviderError('tts_failed', 'incomplete PCM sample')
