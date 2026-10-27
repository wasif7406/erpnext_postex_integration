"""Unit tests for the net-rate helper.

ERPNext recomputes `amount = qty x rate` on every line, so a line must be passed
with its net rate. Passing `rate` alone let ERPNext re-price a zero rate from the
price list, which turned a free (zero-COD) order into a 2,500 invoice.
"""

import unittest

from postex_integration.utils import net_rate


class TestNetRate(unittest.TestCase):
	def test_zero_amount_stays_zero(self):
		self.assertEqual(net_rate(0, 1), 0)
		self.assertEqual(net_rate(None, 1), 0)

	def test_unit_line_keeps_amount(self):
		self.assertEqual(net_rate(2500, 1), 2500)

	def test_split_across_quantity(self):
		self.assertEqual(net_rate(2500, 2), 1250)
		self.assertEqual(net_rate(4500, 3), 1500)

	def test_rounds_to_two_decimals(self):
		self.assertEqual(net_rate(100, 3), 33.33)

	def test_guards_zero_quantity(self):
		self.assertEqual(net_rate(2500, 0), 0)
		self.assertEqual(net_rate(2500, None), 0)

	def test_reproduces_line_amount(self):
		for amount, qty in ((2500, 1), (2500, 2), (100, 3), (1999, 7)):
			with self.subTest(amount=amount, qty=qty):
				rebuilt = round(net_rate(amount, qty) * qty, 2)
				self.assertLessEqual(abs(round(rebuilt - amount, 2)), 0.01)


if __name__ == "__main__":
	unittest.main()
