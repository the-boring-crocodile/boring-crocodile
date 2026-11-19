import unittest


class PackageTest(unittest.TestCase):
    def test_production_entry_point_imports_without_starting_service(self):
        from boring_crocodile.control_plane import create_control_plane, main
        self.assertTrue(callable(create_control_plane))
        self.assertTrue(callable(main))

    def test_controller_does_not_import_pillow(self):
        import os
        import subprocess
        import sys
        result = subprocess.run([sys.executable, "-c",
            "import sys; import boring_crocodile.control_plane; "
            "assert not any(x == 'PIL' or x.startswith('PIL.') for x in sys.modules)"],
            env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pillow_distribution_retains_its_notices(self):
        import importlib.metadata
        distribution = importlib.metadata.distribution("Pillow")
        self.assertEqual(distribution.metadata["License-Expression"], "MIT-CMU")
        notices = [f for f in distribution.files if str(f).endswith(".dist-info/licenses/LICENSE")]
        self.assertEqual(len(notices), 1)
        text = distribution.locate_file(notices[0]).read_text()
        for marker in ("MIT-CMU", "LIBJPEG", "LIBPNG", "LIBWEBP", "ZLIB"):
            self.assertIn(marker, text)
