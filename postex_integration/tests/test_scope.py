"""Unit tests for the state-based sync scope (no site data needed).

The scope rules are the whole point of the scaling fix: an order is polled
because of what state it is in, never because it happens to be among the N most
recently modified rows. These tests pin the table from the implementation brief.
"""

import unittest
from datetime import date, datetime

from postex_integration.constants import (
	ACTIVE_STATUSES,
	LATE_RETURN_STATUSES,
	SCAN_ACTIVE,
	SCAN_LATE_RETURNS,
)
from postex_integration.scope import (
	age_bucket,
	in_late_window,
	is_active,
	is_our_courier,
	scan_mode,
)

TODAY = date(2026, 11, 2)


class TestActiveScope(unittest.TestCase):
	def test_moving_statuses_are_polled_every_run(self):
		for status in ACTIVE_STATUSES:
			with self.subTest(status=status):
				self.assertTrue(is_active(status))
				self.assertEqual(scan_mode(status), SCAN_ACTIVE)

	def test_unknown_or_blank_is_polled(self):
		# Never matched / unmapped text: polling is the only way to learn the state.
		for status in ("", None, "Unknown"):
			with self.subTest(status=status):
				self.assertEqual(scan_mode(status), SCAN_ACTIVE)

	def test_cancelled_is_polled_while_the_order_is_open(self):
		self.assertEqual(scan_mode("Cancelled", "To Deliver and Bill"), SCAN_ACTIVE)

	def test_terminal_statuses_are_never_active(self):
		for status in ("Delivered", "Returned", "Lost", "Damage", "Stolen", "Un-Assigned By Me"):
			with self.subTest(status=status):
				self.assertFalse(is_active(status))


class TestLateReturnScope(unittest.TestCase):
	def test_delivered_goes_to_the_daily_sweep_not_the_poll(self):
		# Delivered is terminal for the 15-minute poll but still swept for 45 days.
		self.assertFalse(is_active("Delivered"))
		self.assertEqual(scan_mode("Delivered"), SCAN_LATE_RETURNS)

	def test_returned_is_a_late_candidate(self):
		self.assertEqual(scan_mode("Returned"), SCAN_LATE_RETURNS)

	def test_window_is_45_days(self):
		self.assertTrue(in_late_window(datetime(2026, 10, 20, 9, 0), TODAY))
		self.assertTrue(in_late_window("2026-09-18", TODAY))  # exactly 45 days
		self.assertFalse(in_late_window("2026-09-17", TODAY))  # 46 days
		self.assertFalse(in_late_window(None, TODAY))

	def test_late_return_statuses_are_the_terminal_ones(self):
		self.assertEqual(set(LATE_RETURN_STATUSES), {"Delivered", "Returned"})


class TestCourierScope(unittest.TestCase):
	def test_blank_courier_may_still_be_claimed(self):
		self.assertTrue(is_our_courier(""))
		self.assertTrue(is_our_courier(None))
		self.assertTrue(is_our_courier("PostEx"))

	def test_other_couriers_are_excluded(self):
		for courier in ("Quick Delivery", "Fast And Quick", "Missing Data"):
			with self.subTest(courier=courier):
				self.assertFalse(is_our_courier(courier))


class TestAgeing(unittest.TestCase):
	def test_buckets(self):
		cases = {
			0: "0-7",
			7: "0-7",
			8: "8-14",
			14: "8-14",
			15: "15-30",
			30: "15-30",
			31: "30+",
			120: "30+",
		}
		for days, expected in cases.items():
			with self.subTest(days=days):
				self.assertEqual(age_bucket(days), expected)

	def test_bad_input_is_not_an_error(self):
		self.assertEqual(age_bucket(None), "0-7")
		self.assertEqual(age_bucket("nonsense"), "0-7")


if __name__ == "__main__":
	unittest.main()
