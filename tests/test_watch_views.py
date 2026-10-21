"""Human watch views preserve identity and never replay arbitrary controls."""
import base64
import json
import unittest

from boring_crocodile.admin import Display
from boring_crocodile.watch_views import VIEWS, OutputDecoder


class WatchViewsTest(unittest.TestCase):
    def event(self, source="worker", **fields):
        return dict(source=source, sender="a" * 32, sequence=1,
                    job_id="b" * 32, task="job:" + "b" * 32,
                    environment="pond", version=1, **fields)

    def test_multiline_command_job_state_and_each_view(self):
        event = self.event(event="job_state", state="queued",
                           command="cd tests\npytest -q \\\n  test_jobs.py", command_truncated=False)
        for kind, formatter in VIEWS.items():
            with self.subTest(view=kind):
                rows = formatter().render(event)
                self.assertIn("queued", " ".join(rows))
                self.assertTrue(all("job bbbbbbbb" in line for line in rows))
                self.assertIn("test_jobs.py", " ".join(rows))

    def test_split_color_utf8_and_unsafe_terminal_sequences(self):
        for colored in (False, True):
            with self.subTest(color=colored):
                decoder = OutputDecoder(colored)
                self.assertEqual(decoder.feed(b"\x1b[32"), [])
                first = decoder.feed(b"mPASS \xc3")
                self.assertEqual(len(first), 1)
                second = decoder.feed(b"\xa4\n\x1b[2JFAIL\x1b[0m\n")
                joined = "".join(text for text, _ in first + second)
                self.assertIn("PASS", joined)
                self.assertIn("ä", joined)
                self.assertIn("\\u001b[2J", joined)
                self.assertNotIn("\x1b[2J", joined)
                self.assertEqual("\x1b[32m" in joined, colored)
                self.assertNotIn("\x1b[32m", "".join(part for part, _ in OutputDecoder(False).feed(b"\x1b[32mPASS\n")))

    def test_failure_and_nonzero_exit_are_distinct(self):
        view = VIEWS["compact"](color=False)
        nonzero = view.render(self.event(event="job_state", state="completed", exit_code=2))
        failed = view.render(self.event(event="job_state", state="failed",
                                        error="task socket refused\x1b[2J"))
        self.assertIn("exit error", nonzero[0])
        self.assertIn("completed exit=2", nonzero[0])
        self.assertIn("failed", failed[0])
        self.assertIn("\\u001b[2J", failed[0])
        self.assertNotIn("\x1b", failed[0])

    def test_interleaved_streams_keep_job_identity_and_json_gap(self):
        view = VIEWS["compact"]()
        display = Display()
        first = self.event(source="runner", event="output", stream="stdout",
                           data=base64.b64encode(b"PASS\nline two\n").decode())
        item = json.loads(display.format(first))
        lines = view.render(item, base64.b64decode(first["data"]))
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("job bbbbbbbb" in line and "stdout" in line for line in lines))
        second = dict(first, sender="c" * 32, sequence=2, stream="stderr",
                      data=base64.b64encode(b"FAIL\n").decode())
        item = json.loads(display.format(second))
        self.assertTrue(item["observation_gap"])
        lines = view.render(item, base64.b64decode(second["data"]))
        self.assertIn("observation may be incomplete", " ".join(lines))
        self.assertIn("stderr", " ".join(lines))
        self.assertEqual(first["data"], base64.b64encode(b"PASS\nline two\n").decode())

    def test_pirate_and_batch_jobs_name_their_work_without_exit_noise(self):
        view = VIEWS["compact"](color=False)
        queued = view.render(self.event(event="job_state", state="queued", kind="pirate",
                                        pirate="parrot", flavour="paint"))
        batch = view.render(self.event(event="job_state", state="queued", kind="batch", step_count=3))
        done = view.render(self.event(event="job_state", state="completed"))
        refused = view.render(self.event(event="job_state", state="refused",
                                         refusal={"code": "busy", "message": "Parrot is busy\x1b[2J"}))
        self.assertIn("pirate=parrot/paint", queued[0])
        self.assertIn("steps=3", batch[0])
        self.assertNotIn("exit=", done[0])
        self.assertIn("completed", done[0])
        self.assertIn("busy: Parrot is busy", refused[0])
        self.assertNotIn("\x1b", refused[0])

    def test_call_state_shows_names_timing_and_refusal_code(self):
        event = dict(source="worker", sender="a" * 32, sequence=1, version=1,
                     environment="pond", task="service:canvas", event="call_state",
                     method="service_call", target="canvas", tool="draw")
        for kind, formatter in VIEWS.items():
            with self.subTest(view=kind):
                view = formatter(color=False)
                started = view.render(dict(event, state="started"))
                refused = view.render(dict(event, sequence=2, state="refused",
                                           duration_ms=12, code="invalid_request"))
                self.assertIn("service:canvas", started[0])
                self.assertIn("service_call draw", started[0])
                self.assertIn("12ms code=invalid_request", refused[0])
