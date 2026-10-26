"""Checks that STT fallbacks explain outages without flagging local English."""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import stt_realtime


class RealtimeOutageStatusTests(unittest.TestCase):
    def test_credit_exhaustion_is_named_for_cloud_language(self):
        with (patch.object(stt_realtime, "_down_until",
                           [time.monotonic() + 60]),
              patch.object(stt_realtime, "_down_reason", "insufficient_funds"),
              patch.object(stt_realtime, "ENGLISH_REALTIME", False)):
            self.assertIn("credits are exhausted",
                          stt_realtime.unavailable_reason("ka"))

    def test_intentional_local_english_is_not_reported_as_an_outage(self):
        with (patch.object(stt_realtime, "_down_until",
                           [time.monotonic() + 60]),
              patch.object(stt_realtime, "_down_reason", "insufficient_funds"),
              patch.object(stt_realtime, "ENGLISH_REALTIME", False)):
            self.assertIsNone(stt_realtime.unavailable_reason("en"))

    def test_opted_in_english_cloud_reports_credit_exhaustion(self):
        with (patch.object(stt_realtime, "_down_until",
                           [time.monotonic() + 60]),
              patch.object(stt_realtime, "_down_reason", "quota_exceeded"),
              patch.object(stt_realtime, "ENGLISH_REALTIME", True)):
            self.assertIn("credits are exhausted",
                          stt_realtime.unavailable_reason("en"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
