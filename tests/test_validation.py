import tempfile
import unittest
from pathlib import Path

import hostonion


class SiteNameValidationTests(unittest.TestCase):
    def test_accepts_documented_site_names(self):
        for name in ("blog", "site_1", "my-shop", "A", "a" * 64):
            with self.subTest(name=name):
                self.assertEqual(hostonion.validate_site_name(name), name)

    def test_trims_surrounding_whitespace(self):
        self.assertEqual(hostonion.validate_site_name("  blog  "), "blog")

    def test_rejects_invalid_names(self):
        for name in ("", "-site", "_site", "a" * 65, "two words", "../site"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    hostonion.validate_site_name(name)


class PortValidationTests(unittest.TestCase):
    def test_accepts_port_boundaries(self):
        self.assertEqual(hostonion.validate_port(1), 1)
        self.assertEqual(hostonion.validate_port(65535), 65535)

    def test_rejects_out_of_range_ports(self):
        for port in (0, -1, 65536):
            with self.subTest(port=port):
                with self.assertRaises(ValueError):
                    hostonion.validate_port(port)


class SiteDirectoryValidationTests(unittest.TestCase):
    def test_resolves_existing_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            expected = Path(temp_dir).resolve()
            self.assertEqual(hostonion.safe_resolve_site(Path(temp_dir)), expected)

    def test_rejects_missing_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing"
            with self.assertRaises(FileNotFoundError):
                hostonion.safe_resolve_site(missing)

    def test_rejects_system_directory(self):
        with self.assertRaises(PermissionError):
            hostonion.safe_resolve_site(Path("/etc"))


if __name__ == "__main__":
    unittest.main()
