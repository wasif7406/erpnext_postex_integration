"""Reference matching: canonical keys and ambiguous PostEx references.

Pinned regressions (04-11 audit):

* `norm()` only upper-cased and trimmed, so `#3144` never matched `3144` and
  `VM#0012` never matched `VM12`;
* the feed map was `ref -> payload` (last one wins), so when PostEx held two
  bookings for one reference one of them silently disappeared.

No site data is needed.
"""

import unittest

from postex_integration.sync import _pick_payload
from postex_integration.utils import norm


class TestNorm(unittest.TestCase):
	def test_separators_are_irrelevant(self):
		self.assertEqual(norm("#3144"), norm("3144"))
		self.assertEqual(norm("VM#0012"), norm("VM0012"))
		self.assertEqual(norm("VM-0012"), norm("VM 0012"))
		self.assertEqual(norm(" vm_5314 "), norm("VM5314"))

	def test_zero_padding_is_irrelevant(self):
		self.assertEqual(norm("VM0012"), norm("VM12"))
		self.assertEqual(norm("#003144"), norm("3144"))

	def test_a_number_is_not_mangled(self):
		self.assertEqual(norm("1002"), "1002")
		self.assertEqual(norm("VM1002"), "VM1002")
		self.assertEqual(norm("11790017"), "11790017")

	def test_old_references_keep_their_shape(self):
		self.assertEqual(norm("june19/2"), "JUNE19/2")
		self.assertNotEqual(norm("june19/2"), norm("june19/3"))
		self.assertNotEqual(norm("aug27/8"), norm("aug27/9"))

	def test_empty_values(self):
		self.assertEqual(norm(None), "")
		self.assertEqual(norm(""), "")
		self.assertEqual(norm("   "), "")


class TestPickPayload(unittest.TestCase):
	def test_one_candidate_needs_no_tracking(self):
		item = {"trackingNumber": "T1"}
		self.assertIs(_pick_payload([item], ""), item)

	def test_several_candidates_are_resolved_by_the_tracking(self):
		first = {"trackingNumber": "T1"}
		second = {"trackingNumber": "T2"}
		self.assertIs(_pick_payload([first, second], "T2"), second)

	def test_several_candidates_without_a_tracking_are_ambiguous(self):
		# Guessing here is how a parcel's status landed on the wrong order.
		self.assertIsNone(_pick_payload([{"trackingNumber": "T1"}, {"trackingNumber": "T2"}], ""))

	def test_a_tracking_that_matches_nothing_is_ambiguous(self):
		self.assertIsNone(
			_pick_payload([{"trackingNumber": "T1"}, {"trackingNumber": "T2"}], "T9")
		)

	def test_whitespace_in_the_tracking_is_ignored(self):
		item = {"trackingNumber": " T2 "}
		self.assertIs(_pick_payload([{"trackingNumber": "T1"}, item], "T 2"), item)

	def test_no_candidates(self):
		self.assertIsNone(_pick_payload([], "T1"))
		self.assertIsNone(_pick_payload(None, "T1"))


if __name__ == "__main__":
	unittest.main()
