"""Minimal WAV (RIFF/PCM) wrapper for turn-end ASR upload."""
import struct


def pcm_to_wav(pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    if len(pcm) % 2:
        pcm = pcm[:-1]
    byte_rate = sample_rate * channels * 2
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, channels * 2, 16) +
            b"data" + struct.pack("<I", len(pcm)) + pcm)
