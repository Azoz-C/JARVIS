from __future__ import annotations

import math
import struct
from collections import deque


class Resampler:
    """Stateful linear PCM16 mono conversion; no removed audioop dependency."""
    def __init__(self, source: int, target: int):
        if source <= 0 or target <= 0:
            raise ValueError("Invalid sample rate")
        self.source, self.target = source, target
        self.samples: list[int] = []
        self.position = 0.0

    def push(self, pcm: bytes) -> bytes:
        if len(pcm) % 2:
            raise ValueError("PCM16 must have an even byte count")
        if self.source == self.target:
            return pcm
        self.samples.extend(v[0] for v in struct.iter_unpack("<h", pcm))
        output = []
        step = self.source / self.target
        while self.position + 1 < len(self.samples):
            i = int(self.position)
            fraction = self.position - i
            output.append(round(self.samples[i] * (1 - fraction) + self.samples[i + 1] * fraction))
            self.position += step
        consumed = min(int(self.position), len(self.samples))
        del self.samples[:consumed]
        self.position -= consumed
        return struct.pack(f"<{len(output)}h", *output)


class Playback:
    """Bound queued playback to avoid seconds of speech after interruption."""
    def __init__(self, rate: int = 48000, max_seconds: float = 0.25):
        self.frame_bytes = rate // 100 * 2
        self.limit = int(rate * 2 * max_seconds)
        self.buffer = bytearray()
        self.dropped_bytes = 0

    def push(self, pcm: bytes):
        if len(pcm) % 2:
            raise ValueError("Invalid PCM")
        # Backpressure belongs at the producer: never accumulate unbounded audio.
        if len(self.buffer) + len(pcm) > self.limit:
            raise BufferError("Playback backpressure")
        self.buffer.extend(pcm)

    def clear(self):
        self.buffer.clear()

    def frame(self) -> bytes:
        chunk = bytes(self.buffer[:self.frame_bytes])
        del self.buffer[:self.frame_bytes]
        return chunk.ljust(self.frame_bytes, b"\0")


def rms(pcm: bytes) -> float:
    samples = [v[0] for v in struct.iter_unpack("<h", pcm)]
    return math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))


class Utterances:
    def __init__(self, rate: int = 16000, threshold: float = 450, silence: float = 0.7):
        self.rate, self.threshold = rate, threshold
        self.silence_limit = int(rate * 2 * silence)
        self.buffer = bytearray()
        self.trailing = 0
        self.pre = deque(maxlen=10)

    def push(self, pcm: bytes) -> bytes | None:
        speech = rms(pcm) >= self.threshold
        if speech:
            if not self.buffer:
                self.buffer.extend(b"".join(self.pre))
            self.buffer.extend(pcm)
            self.trailing = 0
        elif self.buffer:
            self.buffer.extend(pcm)
            self.trailing += len(pcm)
        self.pre.append(pcm)
        if self.buffer and (self.trailing >= self.silence_limit or len(self.buffer) >= self.rate * 2 * 20):
            result = bytes(self.buffer)
            self.buffer.clear()
            self.trailing = 0
            return result if len(result) >= self.rate else None
        return None
