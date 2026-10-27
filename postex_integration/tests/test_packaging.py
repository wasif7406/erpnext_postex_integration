"""Packaging guard.

Frappe parses every installed app's `patches.txt` for each patch type. A file
that has *some* sections but not the requested one aborts `bench migrate` with
"Patch type PatchType.pre_model_sync not found in patches.txt" — which pins the
site to the previous release. Keep both sections present even when empty.
"""

import configparser
import unittest
from pathlib import Path

import postex_integration


class TestPatchesFile(unittest.TestCase):
	def setUp(self):
		self.path = Path(postex_integration.__file__).parent / "patches.txt"
		self.parser = configparser.ConfigParser(allow_no_value=True, delimiters="\n")
		self.parser.read(self.path)

	def test_file_exists(self):
		self.assertTrue(self.path.exists(), f"missing {self.path}")

	def test_has_both_patch_sections(self):
		self.assertTrue(self.path.read_text().strip(), "patches.txt is empty")
		for section in ("pre_model_sync", "post_model_sync"):
			self.assertIn(section, self.parser.sections(), f"missing [{section}]")


if __name__ == "__main__":
	unittest.main()
