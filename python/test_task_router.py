"""task_router: chat vs small vs big, and speed (his order 2026-11-01).

Run:  py -3.13 test_task_router.py
"""
from __future__ import annotations

import time
import unittest

import task_router as tr

CHAT = [
    "What's 2+2",
    "tell me a joke",
    "how does git work?",
    "Can you explain how a car engine works?",
    "I think the new design looks great",
    "what's the capital of Georgia",
    "who are you",
]
SMALL = [
    "open spotify",
    "find my CV",
    "search the web for the weather in Tbilisi",
    "write a note that says buy milk",
    "email mom that I'm late",
    "set an alarm for 7am",
    "type continue into terminal Claude",
    "show me my downloads folder",
    "what is the Claude in my terminal doing",
]
BIG = [
    "Find my largest files in Downloads",
    "Delete the test folder",
    "can you fix the bug in goat's phone page",
    "install git for me",
    "why is my laptop so slow",
    "Clean up my desktop",
    "go through my downloads and delete duplicates",
    "run the tests and then commit",
    "move all the photos from my desktop into a new folder",
    "check my disk space",
    "uninstall spotify",
]
# General knowledge (his order 2026-11-05): always the voice, never Claude.
KNOWLEDGE = [
    "how many Georgian lari is 434 euros",
    "434 euros in lari",
    "convert 434 euros to lari",
    "convert 10 miles to kilometers",
    "compare the BMW M3 and the Audi RS4",
    "calculate 15% of 240",
    "translate thank you into German",
    "define photosynthesis",
    "what is the speed of light",
    "who was Napoleon",
    "how far is the moon",
    "why does the sky look blue",
    "how does a turbocharger work",
    "how do I fix a flat tire",
    "how to install a ceiling fan",
    "how much is 100 dollars in euros",
    "explain how vaccines work",
    "name the planets",
    "Sir what's the tallest mountain in Georgia",
]
# ...but a "knowledge" verb on a thing of his is still a job.
KNOWLEDGE_VERB_JOBS = [
    "convert this video to mp4",
    "compare these two files",
    "convert my photos to png",
]
JOB_TALK = ["What's it doing?", "Cancel that", "change it to blue", "stop that"]


class RouteTests(unittest.TestCase):
    def test_chat(self):
        for s in CHAT:
            with self.subTest(s=s):
                self.assertEqual(tr.route(s)["type"], tr.CHAT)

    def test_small_stays_with_the_voice(self):
        for s in SMALL:
            with self.subTest(s=s):
                r = tr.route(s)
                self.assertEqual((r["type"], r["size"]), (tr.TASK, tr.SMALL))
                self.assertFalse(r["needs_confirmation"])

    def test_big_goes_to_claude(self):
        for s in BIG:
            with self.subTest(s=s):
                r = tr.route(s)
                self.assertEqual((r["type"], r["size"]), (tr.TASK, tr.BIG))
                self.assertTrue(r["task_summary"])

    def test_general_knowledge_is_the_voices(self):
        for s in KNOWLEDGE:
            with self.subTest(s=s):
                r = tr.route(s)
                self.assertEqual((r["type"], r["size"]), (tr.CHAT, tr.SMALL), r)

    def test_knowledge_verbs_on_his_things_stay_jobs(self):
        for s in KNOWLEDGE_VERB_JOBS:
            with self.subTest(s=s):
                self.assertEqual(tr.route(s)["size"], tr.BIG)

    def test_confirmation_only_for_destructive(self):
        self.assertTrue(tr.route("Delete the test folder")["needs_confirmation"])
        self.assertTrue(tr.route("uninstall spotify")["needs_confirmation"])
        self.assertFalse(tr.route("Find my largest files in Downloads")["needs_confirmation"])
        self.assertIn("confirm", tr.job_text(tr.route("Delete the test folder"), ""))

    def test_job_talk_goes_to_the_voice_while_a_job_runs(self):
        for s in JOB_TALK:
            with self.subTest(s=s):
                self.assertEqual(tr.route(s, job_running=True)["type"], tr.CHAT)

    def test_fast(self):
        """Budget is 300 ms; the router must cost well under a millisecond."""
        all_s = CHAT + SMALL + BIG + JOB_TALK + KNOWLEDGE
        t0 = time.perf_counter()
        for _ in range(100):
            for s in all_s:
                tr.route(s)
        per = (time.perf_counter() - t0) * 1000 / (100 * len(all_s))
        print(f"\n  route(): {per:.3f} ms per call")
        self.assertLess(per, 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
