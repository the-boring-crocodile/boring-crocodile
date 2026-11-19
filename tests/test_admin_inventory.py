"""The installer inventory is an admin contract, independent of site-file layout."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from boring_crocodile import admin
from boring_crocodile.site_model import SiteDescription
from test_site_model import description


class AdminInventoryTest(unittest.TestCase):
    def test_list_json_matches_the_shared_installer_example(self):
        main, ponds = description()
        ponds["garden"]["enabled"] = True
        site = SiteDescription.parse(main, ponds)
        example = json.loads((Path(__file__).parent / "fixtures/admin-list.json").read_text())
        self.assertEqual(example, {"ponds": ["pond", "garden"], "pirates": {"parrot": 1300},
                                   "pond_workers": {"pond": 1201, "garden": 1202},
                                   "public_base_url": "https://example.test"})
        out = io.StringIO()
        with patch("boring_crocodile.site_model.SiteDescription.read", return_value=site), \
                patch.object(sys, "argv", ["admin", "list", "--json"]), redirect_stdout(out):
            admin.main()
        self.assertEqual(json.loads(out.getvalue()), example)

    def test_inventory_keeps_parked_worker_identity_without_a_live_socket(self):
        main, ponds = description()
        main["server"]["public_base_url"] = "https://changed.example.test/"
        site = SiteDescription.parse(main, ponds)
        out = io.StringIO()
        with patch("boring_crocodile.site_model.SiteDescription.read", return_value=site), \
                patch.object(sys, "argv", ["admin", "list", "--json"]), redirect_stdout(out):
            admin.main()
        self.assertEqual(json.loads(out.getvalue())["pond_workers"], {"pond": 1201, "garden": 1202})
        self.assertEqual(json.loads(out.getvalue())["ponds"], ["pond"])
        self.assertEqual(json.loads(out.getvalue())["public_base_url"], "https://changed.example.test")

    def test_plain_list_keeps_its_existing_detailed_array(self):
        main, ponds = description()
        settings = SiteDescription.parse(main, ponds).runtime_settings()
        out = io.StringIO()
        with patch.object(admin.Settings, "load", return_value=settings), \
                patch.object(sys, "argv", ["admin", "list"]), redirect_stdout(out):
            admin.main()
        rows = json.loads(out.getvalue())
        self.assertIsInstance(rows, list)
        self.assertEqual([row["pond"] for row in rows], ["pond"])
