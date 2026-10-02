import asyncio
import time
from fractions import Fraction

import av
import mss
import numpy as np
from aiortc.mediastreams import MediaStreamTrack


class ScreenCaptureTrack(MediaStreamTrack):
    """Captures the primary monitor and exposes it as a video track."""

    kind = "video"

    def __init__(self, fps: int = 30) -> None:
        super().__init__()
        self._fps = fps
        self._sct: mss.base.MSSBase | None = None
        self._start: float | None = None
        self._frame_count = 0

    async def recv(self) -> av.VideoFrame:
        if self._sct is None:
            self._sct = mss.mss()
            self._start = time.time()
        self._frame_count += 1

        target_time = self._start + self._frame_count / self._fps
        delay = target_time - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

        monitor = self._sct.monitors[1]
        img = self._sct.grab(monitor)
        bgra = getattr(img, "bgra", None) or getattr(img, "BGRA", None)
        arr = np.frombuffer(bgra, np.uint8).reshape((img.height, img.width, 4))
        frame = av.VideoFrame.from_ndarray(arr, format="bgra")
        frame.pts = self._frame_count
        frame.time_base = Fraction(1, self._fps)
        return frame

    def stop(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None
        super().stop()
