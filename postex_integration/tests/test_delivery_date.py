"""The delivery date taken from a PostEx payload, and where it is safe to post.

The invoice is posted on the day the parcel was delivered, so the date has to be
read from the payload (guide 3.16.3 `orderDeliveryDate`, or the delivered entry
of the status history) instead of using "now".

No site data is needed.
"""

import unittest
from datetime import date
from unittest.mock import patch

from postex_integration import stock
from postex_integration.utils import delivery_date, postex_date


def history(code, **dates):
	entry = {"transactionStatusMessageCode": code, "transactionStatusMessage": "..."}
	entry.update(dates)
	return entry


class TestPostexDate(unittest.TestCase):
	def test_parses_date_and_datetime(self):
		self.assertEqual(postex_date("2026-10-24"), date(2026, 10, 24))
		self.assertEqual(postex_date("2026-10-24 15:30:00"), date(2026, 10, 24))

	def test_empty_and_garbage_are_none(self):
		for value in (None, "", "not a date", 0):
			with self.subTest(value=value):
				self.assertIsNone(postex_date(value))


class TestDeliveryDate(unittest.TestCase):
	def test_order_delivery_date_wins(self):
		payload = {
			"orderDeliveryDate": "2026-10-24",
			"transactionStatusHistory": [
				history("0005", transactionStatusMessageDate="2026-10-20")
			],
		}
		self.assertEqual(delivery_date(payload), date(2026, 10, 24))

	def test_falls_back_to_the_delivered_history_entry(self):
		payload = {
			"transactionStatusHistory": [
				history("0003", transactionStatusMessageDate="2026-10-20"),
				history("0005", transactionStatusMessageDate="2026-10-24 18:05:00"),
			]
		}
		self.assertEqual(delivery_date(payload), date(2026, 10, 24))

	def test_other_history_codes_are_ignored(self):
		payload = {
			"transactionStatusHistory": [
				history("0013", transactionStatusMessageDate="2026-10-20"),
				history("0002", transactionStatusMessageDate="2026-10-21"),
			]
		}
		self.assertIsNone(delivery_date(payload))

	def test_tolerates_alternative_history_keys(self):
		payload = {"transactionStatusHistory": [history("0005", date="2026-10-24")]}
		self.assertEqual(delivery_date(payload), date(2026, 10, 24))

	def test_tolerates_alternative_order_keys(self):
		self.assertEqual(delivery_date({"deliveryDate": "2026-10-24"}), date(2026, 10, 24))
		self.assertEqual(delivery_date({"deliveredDate": "2026-10-24"}), date(2026, 10, 24))

	def test_missing_or_unparseable_is_none(self):
		self.assertIsNone(delivery_date({}))
		self.assertIsNone(delivery_date({"orderDeliveryDate": "whenever"}))
		self.assertIsNone(delivery_date(None))
		self.assertIsNone(delivery_date("not a payload"))

	def test_skips_a_bad_value_and_uses_the_next(self):
		payload = {
			"orderDeliveryDate": "whenever",
			"transactionStatusHistory": [history("0005", transactionStatusMessageDate="2026-10-24")],
		}
		self.assertEqual(delivery_date(payload), date(2026, 10, 24))


class FakeDb:
	def __init__(self, found):
		self.found = found
		self.filters = None

	def exists(self, doctype, filters=None):
		self.doctype = doctype
		self.filters = filters
		return self.found


class BoomDb:
	def exists(self, *args, **kwargs):
		raise RuntimeError("database unavailable")


class TestFiscalYearGuard(unittest.TestCase):
	"""Because `from frappe.utils import get_fiscal_year` does not exist on the
	deployed bench (it lives in `erpnext.accounts.utils`), importing it broke
	every sync run before a single helper could execute. The guard must read the
	Fiscal Year table instead of importing an ERPNext helper.
	"""

	def test_reads_the_fiscal_year_table(self):
		db = FakeDb("2026-2027")
		with patch.object(stock.frappe, "db", db):
			self.assertTrue(stock._fiscal_year_covers(date(2026, 10, 24)))
		self.assertEqual(db.doctype, "Fiscal Year")
		self.assertEqual(db.filters["year_start_date"], ("<=", date(2026, 10, 24)))
		self.assertEqual(db.filters["year_end_date"], (">=", date(2026, 10, 24)))

	def test_no_fiscal_year_means_no_backdating(self):
		with patch.object(stock.frappe, "db", FakeDb(None)):
			self.assertFalse(stock._fiscal_year_covers(date(2026, 10, 24)))

	def test_a_broken_lookup_never_blocks_the_delivery(self):
		with patch.object(stock.frappe, "db", BoomDb()):
			self.assertTrue(stock._fiscal_year_covers(date(2026, 10, 24)))


if __name__ == "__main__":
	unittest.main()
