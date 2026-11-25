"""The return-risk gate for automatic booking.

Merchant rule: auto-book only when the return risk is *below* the account's
limit; everything else is held so the customer can be called first. Manual
booking is never blocked, and an unscored order (the scoring app leaves pct 0
and an empty band when it has no delivery address) counts as unknown, not safe.
"""

import unittest

from postex_integration.risk import risk_gate_blocker


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
