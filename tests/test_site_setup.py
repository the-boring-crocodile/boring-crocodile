"""Configuration input logic; activation journeys share the common fake host."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from boring_crocodile import site_setup


class ExposureTest(unittest.TestCase):
    def test_common_review_names_non_loopback_exposure(self):
        for host in ("0.0.0.0", "::", "192.0.2.1"):
            proposal = dict(state="running", listen=dict(host=host, port=8503), changes=[],
                            stop=[], tasks=[], disable=[], tail=[])
            with self.subTest(host=host):
                text = site_setup.review_summary(proposal)
                self.assertIn("plaintext HTTP", text)
                self.assertIn("firewall", text)


class RuntimeRootTest(unittest.TestCase):
    def test_a_deployed_release_names_current(self):
        from boring_crocodile.site_setup import runtime_root
        self.assertEqual(runtime_root("/opt/boring-crocodile/releases/0.4.7/app/bin/python3"),
                         "/opt/boring-crocodile/current")
        self.assertEqual(runtime_root("/srv/croc/releases/1.0.0/app/bin/python3.14"), "/srv/croc/current")
        self.assertEqual(runtime_root("/opt/venv/bin/python3"), "/opt/venv")



class SiteWizardTest(unittest.TestCase):
    @patch("boring_crocodile.site_setup.defaults", return_value=dict(
        bind_host="127.0.0.1", bind_port=8503, memory_bytes=960 * 1024**2, swap_bytes=0,
        tasks_max=256, task_tasks_max=128, runtime_root="/opt/boring-crocodile/current"))
    def test_each_answer_is_checked_and_asked_again(self, _defaults):
        values = iter(["", "http://mcp.example.org", "https://mcp.example.org/path",
                       "https://mcp.example.org/", "localhost", "192.0.2.1", "", "80", "port",
                       "8080", "", "lots", "1024G", "2g"])
        told = []

        def bind(host, port):
            if host != "127.0.0.1" or port == 8080:
                raise OSError(99, "Cannot assign requested address")

        with tempfile.TemporaryDirectory() as state, \
                patch.object(site_setup, "STATE", Path(state)), \
                patch.object(site_setup, "_bind", bind), \
                patch.object(site_setup.os, "sysconf", side_effect=lambda name: {
                    "SC_PHYS_PAGES": 1024**2, "SC_PAGE_SIZE": 4096}[name]):
            policy = site_setup.wizard(lambda _: next(values), told.append)
        self.assertEqual((policy.public_base_url, policy.bind_host, policy.bind_port, policy.memory_bytes),
                         ("https://mcp.example.org", "127.0.0.1", 8503, 2 * 1024**3))
        again = [line for line in told if "Again" in line]
        self.assertEqual(len(again), 10)
        self.assertTrue(any("no address 192.0.2.1" in line for line in again))
        self.assertTrue(any("Port 8080 is taken" in line for line in again))
        self.assertTrue(any("This host has 4G of memory" in line for line in again))
        self.assertTrue(any("a quarter of this host's memory" in line for line in told))

    @patch("boring_crocodile.site_setup.defaults", return_value=dict(
        bind_host="127.0.0.1", bind_port=8503, memory_bytes=960 * 1024**2, swap_bytes=0,
        tasks_max=256, task_tasks_max=128, runtime_root="/home/someone/venv"))
    def test_a_runtime_outside_opt_stops_before_the_first_question(self, _defaults):
        asked = []
        with self.assertRaisesRegex(ValueError, "below /opt"):
            site_setup.wizard(lambda prompt: asked.append(prompt) or "", lambda _: None)
        self.assertEqual(asked, [])
