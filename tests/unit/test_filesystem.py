import unittest

from tests.fixtures.workspace import registry_for, settings_for, temp_project


class FileSystemToolTests(unittest.TestCase):
    def setUp(self):
        self._context = temp_project(git=False)
        self.root = self._context.__enter__()
        self.registry = registry_for(settings_for(self.root))

    def tearDown(self):
        self._context.__exit__(None, None, None)

    def test_write_then_read(self):
        result = self.registry.execute(
            "write_file", {"path": "pkg/a.txt", "content": "hello\nworld"}
        )
        self.assertTrue(result.get("success"))
        self.assertIsNone(result.get("backup"))

        read = self.registry.execute("read_file", {"path": "pkg/a.txt"})
        self.assertEqual(read["total_lines"], 2)
        self.assertEqual(read["content"], "1: hello\n2: world")

    def test_overwrite_creates_backup(self):
        self.registry.execute("write_file", {"path": "a.txt", "content": "one"})
        result = self.registry.execute(
            "write_file", {"path": "a.txt", "content": "two"}
        )
        self.assertTrue(result.get("success"))
        self.assertTrue(result.get("backup"))
        self.assertTrue((self.root / result["backup"]).is_file())

    def test_atomic_write_leaves_no_temp_file(self):
        self.registry.execute("write_file", {"path": "a.txt", "content": "one"})
        self.assertFalse((self.root / "a.txt.toner-tmp").exists())

    def test_edit_requires_unique_match(self):
        self.registry.execute(
            "write_file", {"path": "a.txt", "content": "x x"}
        )
        result = self.registry.execute(
            "edit_file", {"path": "a.txt", "old_text": "x", "new_text": "y"}
        )
        self.assertEqual(result["code"], "edit_not_unique")
        self.assertEqual((self.root / "a.txt").read_text(), "x x")

    def test_edit_replaces_single_match(self):
        self.registry.execute(
            "write_file", {"path": "a.txt", "content": "alpha beta"}
        )
        result = self.registry.execute(
            "edit_file",
            {"path": "a.txt", "old_text": "beta", "new_text": "gamma"},
        )
        self.assertTrue(result.get("success"))
        self.assertEqual((self.root / "a.txt").read_text(), "alpha gamma")

    def test_delete_disabled_by_default(self):
        self.registry.execute("write_file", {"path": "a.txt", "content": "x"})
        result = self.registry.execute("delete_file", {"path": "a.txt"})
        self.assertEqual(result["code"], "deletion_disabled")
        self.assertTrue((self.root / "a.txt").is_file())

    def test_file_size_limit(self):
        registry = registry_for(settings_for(self.root, max_file_bytes=10))
        result = registry.execute(
            "write_file", {"path": "big.txt", "content": "x" * 50}
        )
        self.assertEqual(result["code"], "file_too_large")

    def test_secret_and_state_paths_blocked(self):
        secret = self.registry.execute(
            "write_file", {"path": "." + "env", "content": "X=1"}
        )
        self.assertEqual(secret["code"], "secret_file")

        state = self.registry.execute(
            "write_file",
            {"path": ".toner/tasks/TASK-999999.json", "content": "{}"},
        )
        self.assertEqual(state["code"], "state_access")

    def test_path_traversal_blocked(self):
        result = self.registry.execute(
            "read_file", {"path": "../../etc/passwd"}
        )
        self.assertEqual(result["code"], "path_outside_root")

    def test_missing_file(self):
        result = self.registry.execute("read_file", {"path": "nope.txt"})
        self.assertEqual(result["code"], "not_found")

    def test_listing_skips_ignored_and_state(self):
        self.registry.execute("write_file", {"path": "keep.txt", "content": "x"})
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "dep.js").write_text("x", encoding="utf-8")

        listing = self.registry.execute("list_files", {"directory": "."})
        self.assertIn("keep.txt", listing["files"])
        self.assertFalse(
            any(item.startswith("node_modules") for item in listing["files"])
        )
        self.assertFalse(any(item.startswith(".toner") for item in listing["files"]))

    def test_search_code(self):
        self.registry.execute(
            "write_file", {"path": "src/a.py", "content": "def needlex():\n    pass\n"}
        )
        result = self.registry.execute("search_code", {"query": "needlex"})
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["line"], 1)

    def test_non_utf8_read_is_reported(self):
        (self.root / "bin.dat").write_bytes(b"\xff\xfe\x00")
        result = self.registry.execute("read_file", {"path": "bin.dat"})
        self.assertEqual(result["code"], "not_utf8")


if __name__ == "__main__":
    unittest.main()
