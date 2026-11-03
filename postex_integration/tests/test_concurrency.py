"""Concurrency guards: the link-field claim and the background sync lock.

Pinned regressions (both found in the 04-11 audit):

* the old "verify-after-create" wrote the link field unconditionally and then
  read it back, so two runs each saw their own value and both kept their
  document (the loser's document stayed behind with its stock/GL effect);
* `api.sync_now(background=1)` enqueued the *unlocked* sync, so a manual
  background run could race the 15-minute cron into the same page.

No site data is needed: the database is a small in-memory stand-in.
"""

import inspect
import unittest
from unittest.mock import patch

from postex_integration import api, utils


class FakeDB:
	"""Just enough of `frappe.db` for `utils.claim_field`."""

	def __init__(self, value=None):
		self.value = value
		self.updates = 0

	def sql(self, query, params):
		value = params[0]
		allow = params[3] if len(params) > 3 else None
		# UPDATE … WHERE ifnull(field,'') = '' [OR field = <allow>]
		if not self.value or (allow is not None and self.value == allow):
			self.value = value
			self.updates += 1
			return 1
		return 0

	def get_value(self, doctype, name, fieldname):
		return self.value

	def commit(self):
		return None


class TestClaimField(unittest.TestCase):
	def claim(self, db, value, allow=None):
		with patch.object(utils.frappe, "db", db):
			return utils.claim_field(
				"Sales Order", "SAL-ORD-1", "custom_postex_delivery_note", value, allow=allow
			)

	def test_first_writer_wins(self):
		db = FakeDB()
		self.assertTrue(self.claim(db, "DN-A"))
		self.assertEqual(db.value, "DN-A")

	def test_second_writer_cannot_steal_the_claim(self):
		# This is exactly what the old write-then-read guard allowed.
		db = FakeDB()
		self.assertTrue(self.claim(db, "DN-A"))
		self.assertFalse(self.claim(db, "DN-B"))
		self.assertEqual(db.value, "DN-A")
		self.assertEqual(db.updates, 1)

	def test_claiming_the_same_value_again_is_idempotent(self):
		db = FakeDB()
		self.assertTrue(self.claim(db, "DN-A"))
		self.assertTrue(self.claim(db, "DN-A"))

	def test_a_stale_claim_needs_to_be_allowed_explicitly(self):
		db = FakeDB("DN-CANCELLED")
		self.assertFalse(self.claim(db, "DN-NEW"))
		self.assertEqual(db.value, "DN-CANCELLED")
		self.assertTrue(self.claim(db, "DN-NEW", allow="DN-CANCELLED"))
		self.assertEqual(db.value, "DN-NEW")

	def test_unsafe_field_name_is_rejected(self):
		with patch.object(utils.frappe, "db", FakeDB()):
			with self.assertRaises(ValueError):
				utils.claim_field("Sales Order", "SAL-ORD-1", "x = 1; --", "DN-A")


class TestBackgroundSync(unittest.TestCase):
	def test_background_sync_goes_through_the_lock(self):
		source = inspect.getsource(api.sync_now)
		self.assertIn("sync_account_locked", source)
		# The unlocked function must never be the enqueue target again.
		self.assertNotIn('"postex_integration.sync.sync_account"', source)


if __name__ == "__main__":
	unittest.main()
