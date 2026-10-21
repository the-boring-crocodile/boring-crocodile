import unittest


class PackageTest(unittest.TestCase):
    def test_production_entry_point_imports_without_starting_service(self):
        from boring_crocodile.control_plane import create_control_plane, main
        self.assertTrue(callable(create_control_plane))
        self.assertTrue(callable(main))
