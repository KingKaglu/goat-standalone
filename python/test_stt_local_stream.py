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

    async def test_georgian_ear_asked_once_for_the_final_words_not_per_pause(self):
        # 2026-10-26 04:20: every pause's partial took the Georgian detour on
        # the one worker; the final transcript missed its 8 s and was redone.
        audio = np.full(8000, 0.05, dtype=np.float32)
        ka = Mock(return_value="მეგობარო, შენ შედიხარ?")
        with (patch.object(stt_whisper, "_POOL", concurrent.futures.ThreadPoolExecutor(1)),
              patch.object(stt_whisper, "EN_KA_CLOUD", True),
              patch.object(stt_whisper, "transcribe_lang",
                           return_value=("Megobaro", "ka?")) as transcribe,
              patch.object(stt_whisper, "_georgian", ka)):
            stream = stt_whisper.Stream(asyncio.get_running_loop(), "en")
            for _ in range(3):                       # three pauses
                stream.feed(audio)
                stream.soft_commit()
            stream.end()
            result = await stream.result()
        self.assertEqual(result, "მეგობარო, შენ შედიხარ?")
        self.assertEqual(stream.heard, "ka")
        self.assertTrue(all(c.kwargs.get("cloud") is False for c in transcribe.call_args_list))
        ka.assert_called_once()
        self.assertEqual(len(ka.call_args.args[0]), 3 * 8000)   # the whole utterance

    async def test_english_final_words_never_reach_the_georgian_ear(self):
        ka = Mock(return_value="x")
        with (patch.object(stt_whisper, "_POOL", InlineExecutor()),
              patch.object(stt_whisper, "transcribe_lang", return_value=("open notepad", "en")),
              patch.object(stt_whisper, "_georgian", ka)):
            stream = stt_whisper.Stream(asyncio.get_running_loop(), "en")
            stream.feed(np.full(8000, 0.05, dtype=np.float32))
            stream.soft_commit()
            self.assertEqual(await stream.result(), "open notepad")
        ka.assert_not_called()

    async def test_local_english_stream_does_not_prime_georgian_cloud_ear(self):
        live = SimpleNamespace(prime=Mock())
        with (patch.object(stt_whisper, "EN_KA_CLOUD", False),
              patch.object(stt_whisper, "_ka_at", time.monotonic()),
              patch.dict(sys.modules, {"stt_live": live})):
            stt_whisper.Stream(asyncio.get_running_loop(), "en")
        live.prime.assert_not_called()


class WarmGeorgianRaceTests(unittest.IsolatedAsyncioTestCase):
    """2026-11-08: with Georgian lately, Live hears the pause's audio side by
    side with Whisper instead of after it."""

    def _patches(self, whisper, live_job):
        return (patch.object(stt_whisper, "_POOL", concurrent.futures.ThreadPoolExecutor(1)),
                patch.object(stt_whisper, "_KA_POOL", concurrent.futures.ThreadPoolExecutor(2)),
                patch.object(stt_whisper, "EN_KA_CLOUD", True),
                patch.object(stt_whisper, "_ka_at", time.monotonic()),
                patch.object(stt_whisper, "transcribe_lang", side_effect=whisper),
                patch.object(stt_whisper, "_live_job", side_effect=live_job),
                patch.dict(sys.modules, {"stt_live": SimpleNamespace(prime=Mock())}))

    async def _one_pause(self, whisper, live_job):
        p = self._patches(whisper, live_job)
        with p[0], p[1], p[2], p[3], p[4], p[5] as lj, p[6]:
            stream = stt_whisper.Stream(asyncio.get_running_loop(), "en")
            stream.feed(np.full(16000, 0.05, dtype=np.float32))
            stream.soft_commit()
            stream.end()
            t0 = time.monotonic()
            text = await stream.result()
            return text, stream, lj, time.monotonic() - t0

    async def test_live_georgian_wins_without_waiting_for_whisper(self):
        def whisper(*a, **k):
            time.sleep(1.0)
            return "Gamarjoba", "ka?"
        text, stream, lj, took = await self._one_pause(
            whisper, lambda audio: ("გამარჯობა", 0.1))
        self.assertEqual(text, "გამარჯობა")
        self.assertEqual(stream.heard, "ka")
        self.assertLess(took, 0.8)
        lj.assert_called_once()

    async def test_slow_live_is_reused_not_asked_twice(self):
        def live(audio):
            time.sleep(0.3)
            return "როგორ ხარ?", 0.3
        text, stream, lj, _ = await self._one_pause(
            lambda *a, **k: ("Rogor khar", "ka?"), live)
        self.assertEqual(text, "როგორ ხარ?")
        lj.assert_called_once()

    async def test_english_from_whisper_ignores_live(self):
        def live(audio):
            time.sleep(0.3)
            return "open notepad", 0.3
        text, stream, _, _ = await self._one_pause(
            lambda *a, **k: ("open notepad.", "en"), live)
        self.assertEqual(text, "open notepad.")
        self.assertEqual(stream.heard, "en")

    async def test_cold_stream_never_starts_live_early(self):
        lj = Mock(return_value=("x", 0.0))
        with (patch.object(stt_whisper, "_POOL", InlineExecutor()),
              patch.object(stt_whisper, "_ka_at", 0.0),
              patch.object(stt_whisper, "transcribe_lang", return_value=("hi", "en")),
              patch.object(stt_whisper, "_live_job", lj)):
            stream = stt_whisper.Stream(asyncio.get_running_loop(), "en")
            stream.feed(np.full(8000, 0.05, dtype=np.float32))
            stream.soft_commit()
            self.assertEqual(await stream.result(), "hi")
        lj.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
