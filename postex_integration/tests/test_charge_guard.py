"""Courier charges must never be booked twice for the same invoice.

Pinned regression (26-11 audit): four settled invoices showed a *negative*
receivable — Irfan Ali, Kashif Khan, Zafar Iqbal at −79.96 each and Muhammad Umar
at −143.92, i.e. exactly the 4% withheld tax. A manual "audit catch-up" journal
entry had already booked that tax; a later settlement entry booked it again,
because the only guard was `custom_postex_charges_entry`, which a hand-made entry
never sets. Each duplicate credit to `Debtors` left the customer in advance.

`charge_shortfall()` / `_booked_charges()` / `_entry_charges()` close that: a
second entry reads the ledger *and* the linked entry and posts only what is still
missing. No site data is needed — the arithmetic is pure and the queries are
replaced by stubs.
"""

import types
import unittest
from unittest.mock import patch

from postex_integration import settlement


class Config:
	delivery_charges = "Delivery Charges - AN"
	finance = "Courier Finance - AN"
	tax_withheld = "PostEx Tax Withheld (Advance Tax) - AN"
	debtors = "Debtors - AN"


class TestChargeShortfall(unittest.TestCase):
	def test_nothing_booked_posts_everything(self):
		self.assertEqual(settlement.charge_shortfall(179.40, 79.96), (179.40, 79.96))

	def test_withholding_already_booked_is_not_posted_again(self):
		# The Irfan Ali / ACC-SINV-2026-02224 shape: fee booked earlier, tax by hand.
		self.assertEqual(settlement.charge_shortfall(0, 79.96, 0, 79.96), (0.0, 0.0))

	def test_only_the_missing_half_is_posted(self):
		self.assertEqual(settlement.charge_shortfall(179.40, 79.96, 179.40, 0), (0.0, 79.96))

	def test_more_booked_than_needed_posts_nothing(self):
		self.assertEqual(settlement.charge_shortfall(179.40, 79.96, 200, 100), (0.0, 0.0))

	def test_rounding_noise_counts_as_booked(self):
		self.assertEqual(settlement.charge_shortfall(79.96, 0, 79.96, 0), (0.0, 0.0))

	def test_a_real_difference_is_kept(self):
		self.assertEqual(settlement.charge_shortfall(79.97, 0, 79.96, 0), (0.01, 0.0))


class FakeDB:
	def __init__(self, rows):
		self.rows = rows
		self.calls = []

	def sql(self, query, params=None, as_dict=False):
		self.calls.append(params)
		return self.rows


class FakeFrappe:
	def __init__(self, sql_rows=None, all_rows=None):
		self.db = FakeDB(sql_rows or [])
		self.all_rows = all_rows or []
		self.get_all_filters = []

	def get_all(self, doctype, **kwargs):
		self.get_all_filters.append(kwargs.get("filters"))
		return self.all_rows


def with_frappe(fn, sql_rows=None, all_rows=None):
	fake = FakeFrappe(sql_rows, all_rows)
	with patch.object(settlement, "frappe", fake):
		return fn(), fake


class TestBookedCharges(unittest.TestCase):
	def test_fee_and_withholding_of_one_entry(self):
		rows = [
			{"entry": "ACC-JV-1", "account": Config.delivery_charges, "debit": 179.40},
			{"entry": "ACC-JV-1", "account": Config.tax_withheld, "debit": 79.96},
		]
		result, _fake = with_frappe(
			lambda: settlement._booked_charges("ACC-SINV-1", Config()), sql_rows=rows
		)
		self.assertEqual(result, (179.40, 79.96, "ACC-JV-1"))

	def test_amounts_are_summed_across_entries(self):
		rows = [
			{"entry": "ACC-JV-1", "account": Config.delivery_charges, "debit": 179.40},
			{"entry": "ACC-JV-2", "account": Config.tax_withheld, "debit": 79.96},
		]
		fee, withheld, entry = with_frappe(
			lambda: settlement._booked_charges("ACC-SINV-1", Config()), sql_rows=rows
		)[0]
		self.assertEqual((fee, withheld), (179.40, 79.96))
		self.assertIn(entry, ("ACC-JV-1", "ACC-JV-2"))

	def test_nothing_booked(self):
		result, _fake = with_frappe(lambda: settlement._booked_charges("ACC-SINV-1", Config()))
		self.assertEqual(result, (0.0, 0.0, None))

	def test_no_invoice_does_not_query_the_ledger(self):
		result, fake = with_frappe(lambda: settlement._booked_charges("", Config()))
		self.assertEqual(result, (0.0, 0.0, None))
		self.assertEqual(fake.db.calls, [])

	def test_query_is_scoped_to_the_invoice_and_the_two_accounts(self):
		_fake_result, fake = with_frappe(
			lambda: settlement._booked_charges("ACC-SINV-2026-02224", Config())
		)
		params = fake.db.calls[0]
		self.assertEqual(params["invoice"], "ACC-SINV-2026-02224")
		self.assertEqual(params["debtors"], Config.debtors)
		self.assertIn(Config.tax_withheld, params["accounts"])
		self.assertIn(Config.delivery_charges, params["accounts"])


class TestEntryCharges(unittest.TestCase):
	def test_a_fee_only_entry_leaves_the_tax_unbooked(self):
		# The historical shape: 02155 booked 179.40 and nothing else, so the
		# withheld tax must still be posted.
		rows = [{"account": Config.delivery_charges, "debit_in_account_currency": 179.40}]
		result, _fake = with_frappe(
			lambda: settlement._entry_charges("ACC-JV-2026-02155", Config()), all_rows=rows
		)
		self.assertEqual(result, (179.40, 0.0))

	def test_both_parts_are_read(self):
		rows = [
			{"account": Config.delivery_charges, "debit_in_account_currency": 179.40},
			{"account": Config.tax_withheld, "debit_in_account_currency": 79.96},
		]
		result, _fake = with_frappe(
			lambda: settlement._entry_charges("ACC-JV-1", Config()), all_rows=rows
		)
		self.assertEqual(result, (179.40, 79.96))

	def test_a_blank_entry_reads_nothing(self):
		result, fake = with_frappe(lambda: settlement._entry_charges("", Config()))
		self.assertEqual(result, (0.0, 0.0))
		self.assertEqual(fake.get_all_filters, [])




class TestReturnShortfall(unittest.TestCase):
	"""The return leg gets the same treatment as the settlement charges.

	A return produces no invoice, so a manual catch-up entry cannot be found by
	reference — only by its remark. Without the ledger read, re-uploading a
	settlement log booked the same courier cost twice.
	"""

	def test_nothing_booked_posts_everything(self):
		self.assertEqual(settlement.return_shortfall(179.40), 179.40)

	def test_fully_booked_by_hand_posts_nothing(self):
		self.assertEqual(settlement.return_shortfall(179.40, 179.40), 0.0)

	def test_a_partial_entry_leaves_the_rest(self):
		self.assertEqual(settlement.return_shortfall(179.40, 156.00), 23.40)

	def test_more_booked_than_needed_posts_nothing(self):
		self.assertEqual(settlement.return_shortfall(179.40, 200), 0.0)

	def test_sub_paisa_noise_counts_as_booked_but_a_paisa_is_kept(self):
		# Same convention as charge_shortfall: only < 0.01 is rounded away.
		self.assertEqual(settlement.return_shortfall(179.40, 179.395), 0.0)
		self.assertEqual(settlement.return_shortfall(179.40, 179.39), 0.01)

	def test_zero_total_is_zero(self):
		self.assertEqual(settlement.return_shortfall(0, 0), 0.0)


class TestBookedReturnCost(unittest.TestCase):
	def test_one_entry_is_summed(self):
		rows = [{"entry": "ACC-JV-9", "debit": 179.40}]
		result, _fake = with_frappe(
			lambda: settlement.booked_return_cost("SAL-ORD-1", ("2665", "VM5522"), Config()),
			sql_rows=rows,
		)
		self.assertEqual(result, (179.40, "ACC-JV-9"))

	def test_several_entries_are_added(self):
		rows = [{"entry": "ACC-JV-9", "debit": 156.00}, {"entry": "ACC-JV-10", "debit": 23.40}]
		booked, entry = with_frappe(
			lambda: settlement.booked_return_cost("SAL-ORD-1", ("VM5522",), Config()),
			sql_rows=rows,
		)[0]
		self.assertEqual(booked, 179.40)
		self.assertIn(entry, ("ACC-JV-9", "ACC-JV-10"))

	def test_nothing_booked(self):
		result, _fake = with_frappe(
			lambda: settlement.booked_return_cost("SAL-ORD-1", ("VM5522",), Config())
		)
		self.assertEqual(result, (0.0, None))

	def test_no_keys_does_not_query_the_ledger(self):
		result, fake = with_frappe(
			lambda: settlement.booked_return_cost("SAL-ORD-1", (None, "", "  "), Config())
		)
		self.assertEqual(result, (0.0, None))
		self.assertEqual(fake.db.calls, [])

	def test_query_matches_the_remark_and_the_charges_account(self):
		_fake_result, fake = with_frappe(
			lambda: settlement.booked_return_cost("SAL-ORD-1", ("VM5522", "2665"), Config())
		)
		params = fake.db.calls[0]
		self.assertEqual(params["charges"], Config.delivery_charges)
		self.assertEqual(params["finance"], "Courier Finance - AN")
		self.assertEqual(params["debtors"], Config.debtors)
		self.assertEqual(sorted([params["needle0"], params["needle1"]]), ["%2665%", "%VM5522%"])


if __name__ == "__main__":
	unittest.main()
