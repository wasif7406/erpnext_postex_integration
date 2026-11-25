"""The return-risk gate for automatic booking.

Merchant rule: auto-book only when the return risk is *below* the account's
limit; everything else is held so the customer can be called first. Manual
booking is never blocked, and an unscored order (the scoring app leaves pct 0
and an empty band when it has no delivery address) counts as unknown, not safe.
"""

import unittest

from postex_integration.gates import (
	auto_booking_blocker,
	gates_enabled,
	product_gate_blocker,
	risk_gate_blocker,
)


class Account(dict):
	pass


class Order(dict):
	pass


def account(enabled=1, limit=35):
	return Account(enable_risk_gate=enabled, auto_book_max_return_risk=limit)


def order(pct=0, band=""):
	return Order(custom_return_risk_pct=pct, custom_return_risk_band=band)


class TestRiskGate(unittest.TestCase):
	def test_gate_off_books_anything(self):
		self.assertIsNone(risk_gate_blocker(account(enabled=0), order(90, "Very High")))

	def test_high_risk_is_held(self):
		reason = risk_gate_blocker(account(), order(74.4, "Very High"))
		self.assertIsNotNone(reason)
		self.assertIn("74.4", reason)
		self.assertIn("35", reason)

	def test_low_risk_is_booked(self):
		self.assertIsNone(risk_gate_blocker(account(), order(12, "Low")))

	def test_the_limit_is_exclusive(self):
		# "less than entered amount": exactly at the limit is NOT booked.
		self.assertIsNone(risk_gate_blocker(account(limit=35), order(34.9, "Medium")))
		self.assertIsNotNone(risk_gate_blocker(account(limit=35), order(35, "Medium")))

	def test_no_score_is_held_not_assumed_safe(self):
		reason = risk_gate_blocker(account(), order(0, ""))
		self.assertIsNotNone(reason)
		self.assertIn("no return-risk score", reason)

	def test_band_without_pct_still_counts_as_scored(self):
		self.assertIsNone(risk_gate_blocker(account(), order(0, "Low")))

	def test_zero_limit_behaves_as_off(self):
		# Gate ticked before a limit was entered: do not block every booking.
		self.assertIsNone(risk_gate_blocker(account(limit=0), order(90, "Very High")))

	def test_missing_account_or_order_is_safe(self):
		self.assertIsNone(risk_gate_blocker(None, order(90, "Very High")))


class Item(dict):
	pass


def order_with(items, pct=0, band=""):
	o = Order(custom_return_risk_pct=pct, custom_return_risk_band=band)
	o["items"] = items
	return o


class TestProductGate(unittest.TestCase):
	"""Auto-book only the products ERPNext is supposed to book.

	Empty list = no restriction (a ticked box with nothing behind it must not
	stop every booking). Every line has to match: a mixed parcel is manual.
	"""

	def acc(self, enabled=1, rows=None):
		a = Account(enable_item_gate=enabled)
		a["auto_booking_items"] = rows or []
		return a

	def test_gate_off_ignores_products(self):
		a = self.acc(enabled=0, rows=[{"item_code": "A"}])
		self.assertIsNone(product_gate_blocker(a, order_with([{"item_code": "B"}])))

	def test_empty_list_means_no_restriction(self):
		self.assertIsNone(product_gate_blocker(self.acc(rows=[]), order_with([{"item_code": "B"}])))

	def test_listed_item_is_booked(self):
		a = self.acc(rows=[{"item_code": "NEW-1"}])
		self.assertIsNone(product_gate_blocker(a, order_with([{"item_code": "NEW-1"}])))

	def test_unlisted_item_holds_the_order(self):
		a = self.acc(rows=[{"item_code": "NEW-1"}])
		reason = product_gate_blocker(a, order_with([{"item_code": "8907288772696"}]))
		self.assertIn("8907288772696", reason)

	def test_every_line_must_match(self):
		a = self.acc(rows=[{"item_code": "NEW-1"}])
		mixed = [{"item_code": "NEW-1"}, {"item_code": "8907288772696"}]
		self.assertIsNotNone(product_gate_blocker(a, order_with(mixed)))

	def test_item_group_matches_too(self):
		a = self.acc(rows=[{"item_group": "Bulbs"}])
		self.assertIsNone(
			product_gate_blocker(a, order_with([{"item_code": "X", "item_group": "Bulbs"}]))
		)

	def test_order_without_items_is_held(self):
		self.assertIsNotNone(product_gate_blocker(self.acc(rows=[{"item_code": "A"}]), order_with([])))


class TestCombinedGate(unittest.TestCase):
	def test_risk_wins_when_both_gates_apply(self):
		a = Account(enable_risk_gate=1, auto_book_max_return_risk=35,
		            enable_item_gate=1, auto_booking_items=[{"item_code": "A"}])
		reason = auto_booking_blocker(a, order_with([{"item_code": "B"}], pct=90, band="Very High"))
		self.assertIn("return risk", reason)

	def test_product_gate_applies_when_risk_is_fine(self):
		a = Account(enable_risk_gate=1, auto_book_max_return_risk=35,
		            enable_item_gate=1, auto_booking_items=[{"item_code": "A"}])
		reason = auto_booking_blocker(a, order_with([{"item_code": "B"}], pct=5, band="Low"))
		self.assertIn("not in the auto-booking list", reason)

	def test_nothing_enabled_is_cheap(self):
		self.assertFalse(gates_enabled(Account(enable_risk_gate=0, enable_item_gate=0)))
		self.assertTrue(gates_enabled(Account(enable_risk_gate=0, enable_item_gate=1)))
