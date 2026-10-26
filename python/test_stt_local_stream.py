"""Checks the local Whisper streaming adapter without opening a STT service."""
from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

import stt_whisper


class InlineExecutor:
    """Run immediately to force the submit-before-Future-assignment race."""

    def submit(self, fn, *args):
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class LocalWhisperStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_cpu_stream_uses_local_whisper_and_returns_partial(self):
        audio = np.full(16000, 0.05, dtype=np.float32)
        with (patch.object(stt_whisper, "_POOL", InlineExecutor()),
              patch.object(stt_whisper, "EN_KA_CLOUD", False),
              patch.object(stt_whisper, "transcribe_lang",
                           return_value=("local transcript", "en")) as transcribe):
            stream = stt_whisper.Stream(asyncio.get_running_loop(), "en")
            stream.feed(audio)
            stream.soft_commit()
            result = await stream.result()

        self.assertEqual(result, "local transcript")
        self.assertEqual(stream.last_partial, "local transcript")
        transcribe.assert_called_once()
        self.assertEqual(transcribe.call_args.args[2], "en")

    async def test_local_english_stream_does_not_prime_georgian_cloud_ear(self):
        live = SimpleNamespace(prime=Mock())
        with (patch.object(stt_whisper, "EN_KA_CLOUD", False),
              patch.object(stt_whisper, "_ka_at", time.monotonic()),
              patch.dict(sys.modules, {"stt_live": live})):
            stt_whisper.Stream(asyncio.get_running_loop(), "en")
        live.prime.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
