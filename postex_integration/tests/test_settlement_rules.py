"""Settlement logic that must not regress.

Two audits (20-11 / 21-11) found real money behind these decisions:

* 43 delivered invoices (94,665.25) sat on "Not Paid" although the CPR export
  showed them paid, and 5,188.95 of courier fees plus 653.72 of withheld tax had
  never been booked. Both came from a settlement path that had no idea what the
  CPR actually deducted and that silently gave up once the receivable was zero.
* the withheld tax was computed on the invoice total; PostEx withholds on the
  COD collected, so a split consignment drifted from the log.

No site data is needed: everything asserted here is pure arithmetic or parsing.
"""

import csv
import os
import tempfile
import unittest

from postex_integration.settlement import charge_split, withholding_amount
from postex_integration.settlement_log import parse_log

LOG_HEADER = [
	"ORDER_REF_NUMBER",
	"TRACKING_NUMBER",
	"CUSTOMER_NAME",
	"COD_AMOUNT",
	"TRANSACTION_STATUS",
	"UPFRONT_CPR_NUMBER",
	"UPFRONT_PAYMENT",
	"UPFRONT_CPR_DATE",
	"RESERVE_CPR_NUMBER",
	"RESERVE_PAYMENT",
	"RESERVE_CPR_DATE",
	"ORDER_PICKUP_DATE",
	"ORDER_DELIVERY_DATE",
	"BALANCE_PAYMENT",
	"FEE",
	"GST",
	"ITWH (2%)",
	"STWH (2%)",
]


def write_log(rows):
	fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig", newline="")
	writer = csv.writer(fh)
	writer.writerow(LOG_HEADER)
	for row in rows:
		writer.writerow(row)
	fh.close()
	return fh.name


class TestWithholdingBase(unittest.TestCase):
	def test_withheld_on_the_cod_not_the_invoice(self):
		# VM5017: one consignment of 2 x 1,575 = COD 3,150, one invoice of 1,575.
		# PostEx withheld 126 (4% of 3,150); 4% of the invoice would be 63.
		self.assertEqual(withholding_amount(3150, 4), 126.00)
		self.assertEqual(withholding_amount(1575, 4), 63.00)

	def test_percentage_is_per_account(self):
		self.assertEqual(withholding_amount(1999, 4), 79.96)
		self.assertEqual(withholding_amount(1999, 0), 0.00)

	def test_zero_cod_withholds_nothing(self):
		self.assertEqual(withholding_amount(0, 4), 0.00)


class TestChargeSplit(unittest.TestCase):
	def test_charges_reduce_the_receivable_first(self):
		# 1,999 invoice, 179.40 fee, 79.96 withheld: all of it fits in the AR.
		to_debtors, excess, role = charge_split(259.36, 1999)
		self.assertEqual((to_debtors, excess, role), (259.36, 0.0, "finance"))

	def test_small_invoice_excess_goes_to_the_wallet(self):
		# 4 PKR invoice with a 179.40 fee: 4 against the AR, 175.40 to the wallet.
		to_debtors, excess, role = charge_split(179.40, 4)
		self.assertEqual(to_debtors, 4.0)
		self.assertEqual(excess, 175.40)
		self.assertEqual(role, "finance")

	def test_zero_outstanding_never_touches_debtors(self):
		# The receivable was already cleared elsewhere: booking against Debtors
		# would leave a credit balance, so the cost reduces Unsettled COD.
		to_debtors, excess, role = charge_split(242.40, 0)
		self.assertEqual(to_debtors, 0.0)
		self.assertEqual(excess, 242.40)
		self.assertEqual(role, "unsettled_cod")

	def test_negative_outstanding_behaves_like_zero(self):
		self.assertEqual(charge_split(100, -0.2), (0.0, 100.0, "unsettled_cod"))


class TestParseLog(unittest.TestCase):
	def tearDown(self):
		if getattr(self, "_path", None):
			os.unlink(self._path)

	def test_numbers_are_parsed_and_dates_trimmed(self):
		self._path = write_log(
			[
				[
					"VM4904",
					"23651110002039",
					"Muhammad Umar",
					"3598.00",
					"Delivered",
					"",
					"",
					"",
					"CPR-DG1ZL873141",
					"3598.00",
					"2026-10-26 10:27:53",
					"2026-10-20 17:43:57",
					"2026-10-21 11:02:00",
					"0.00",
					"117.00",
					"17.55",
					"71.96",
					"71.96",
				]
			]
		)
		[item] = parse_log(self._path)
		self.assertEqual(item["ref"], "VM4904")
		self.assertEqual(item["tracking"], "23651110002039")
		self.assertEqual(item["status"], "Delivered")
		self.assertEqual(item["cod"], 3598.00)
		self.assertEqual(item["reserve"], 3598.00)
		self.assertEqual(item["fee"], 117.00)
		self.assertEqual(item["gst"], 17.55)
		self.assertEqual(item["itwh"], 71.96)
		self.assertEqual(item["stwh"], 71.96)
		self.assertEqual(item["cpr"], "CPR-DG1ZL873141")
		self.assertEqual(item["cpr_date"], "2026-10-26")
		self.assertEqual(item["delivered_on"], "2026-10-21")

	def test_rows_without_a_tracking_number_are_skipped(self):
		self._path = write_log([["VM1", "", "x", "1", "Delivered", "", "", "", "", "0", "", "", "", "", "0", "0", "0", "0"]])
		self.assertEqual(parse_log(self._path), [])

	def test_return_rows_keep_their_fee(self):
		# The API returns transactionFee 0 for a returned parcel; the log does not.
		self._path = write_log(
			[
				[
					"VM4848",
					"20651110001993",
					"x",
					"1999.00",
					"Return",
					"",
					"",
					"",
					"CPR-X",
					"0.00",
					"2026-11-02 00:00:00",
					"",
					"",
					"0.00",
					"156.00",
					"23.40",
					"0.00",
					"0.00",
				]
			]
		)
		[item] = parse_log(self._path)
		self.assertEqual(item["status"], "Return")
		self.assertEqual(item["reserve"], 0.0)
		self.assertEqual(item["fee"] + item["gst"], 179.40)

	def test_a_missing_file_raises(self):
		with self.assertRaises(FileNotFoundError):
			parse_log("/nonexistent/settlement.csv")
