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
from postex_integration.settlement_log import apply_blocker, parse_log, payment_state

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


class TestCutover(unittest.TestCase):
	"""A historical export must not re-book courier cost already covered by a
	lump journal entry (25-10 posted 67,319.60 for exactly that)."""

	def test_rows_before_the_cutover_are_ignored(self):
		from postex_integration import settlement_log

		path = write_log(
			[
				["VM1", "111", "a", "1", "Return", "", "", "", "CPR-A", "0", "2026-09-01 00:00:00", "", "", "0", "156", "23.40", "0", "0"],
				["VM2", "222", "b", "1", "Return", "", "", "", "CPR-B", "0", "2026-11-16 00:00:00", "", "", "0", "156", "23.40", "0", "0"],
			]
		)
		try:
			rows = settlement_log.parse_log(path)
			kept = [r for r in rows if (r["cpr_date"] or r["delivered_on"]) >= "2026-11-01"]
			self.assertEqual([r["tracking"] for r in kept], ["222"])
		finally:
			os.unlink(path)


class TestPaymentState(unittest.TestCase):
	"""Paid means the whole COD arrived, not that some money arrived.

	PostEx can pay in two legs (upfront then reserve). Settling an invoice books
	its entire outstanding, so a first leg must not count as paid — but a parcel
	paid only upfront must not look unpaid forever either.
	"""

	def row(self, cod, reserve=0, upfront=0, balance=0):
		return {"cod": cod, "reserve": reserve, "upfront": upfront, "balance": balance}

	def test_the_weekly_reserve_alone_is_paid(self):
		# this merchant's actual shape: reserve == gross COD
		state, legs, cod = payment_state(self.row(3598, reserve=3598))
		self.assertEqual((state, legs, cod), ("paid", 3598.0, 3598.0))

	def test_a_split_payout_is_paid_once_both_legs_land(self):
		state, _, _ = payment_state(self.row(2000, upfront=1000, reserve=1000))
		self.assertEqual(state, "paid")

	def test_the_first_leg_alone_is_partial_not_paid(self):
		state, legs, _ = payment_state(self.row(2000, upfront=1000))
		self.assertEqual(state, "partial")
		self.assertEqual(legs, 1000.0)

	def test_paid_entirely_upfront_is_not_unpaid(self):
		# the bug this replaces: `reserve == 0` used to mean "unpaid" forever
		state, _, _ = payment_state(self.row(1999, upfront=1999))
		self.assertEqual(state, "paid")

	def test_no_money_is_unpaid(self):
		self.assertEqual(payment_state(self.row(1999))[0], "unpaid")

	def test_zero_cod_has_nothing_due(self):
		# free replacement / -COPY orders: never "unpaid", never settled
		self.assertEqual(payment_state(self.row(0))[0], "nothing_due")

	def test_rounding_within_tolerance_is_paid(self):
		self.assertEqual(payment_state(self.row(1999.00, reserve=1998.60))[0], "paid")

	def test_a_real_shortfall_is_partial(self):
		state, _, _ = payment_state(self.row(1999, reserve=1500))
		self.assertEqual(state, "partial")


class TestApplyGuard(unittest.TestCase):
	"""Booking without a cutover would double-book what a lump JE covered.

	The 20-11 export holds 229 parcels (40,999.67) whose courier cost the
	25-10 lump already paid for, with no per-parcel record to match on.
	"""

	def test_a_cutover_allows_apply(self):
		self.assertIsNone(apply_blocker("2026-11-17", False))

	def test_full_history_is_refused_without_the_tick(self):
		message = apply_blocker(None, False)
		self.assertIsNotNone(message)
		self.assertIn("Reconcile From", message)

	def test_the_tick_allows_a_full_replay(self):
		self.assertIsNone(apply_blocker(None, True))
