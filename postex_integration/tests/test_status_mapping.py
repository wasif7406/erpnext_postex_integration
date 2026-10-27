"""Unit tests for the status mapping (no site data needed)."""

import unittest

from postex_integration.constants import PICKUP_STATUSES, STATUS_OPTIONS
from postex_integration.utils import map_status, match_status, norm


class TestStatusMapping(unittest.TestCase):
	def test_live_strings_map_to_stored_options(self):
		cases = {
			"In Stock": "PostEx Warehouse",
			"Delivery En-Route": "Out For Delivery",
			"En-Route to PostEx warehouse": "En-Route to PostEx Warehouse",
			"Under Verification": "Delivery Under Review",
			"Return In-Transit": "Out For Return",
			"Return": "Returned",
			"Refused by Consignee": "Returned",
			"Un-Assigned": "Un-Assigned By Me",
			"UNBOOKED": "Unbooked",
			"Booked": "Booked",
			"Delivered": "Delivered",
			"Attempt Made": "Attempted",
			"Transferred": "Transferred",
			"Expired": "Expired",
		}
		for raw, expected in cases.items():
			with self.subTest(raw=raw):
				self.assertEqual(match_status(raw), expected)

	def test_specific_needles_win(self):
		# "en route to postex" must not become "PostEx Warehouse"/"Out For Delivery"
		self.assertEqual(match_status("En-Route to PostEx Warehouse"), "En-Route to PostEx Warehouse")
		# "return in transit" must not become "Returned"
		self.assertEqual(match_status("Return In-Transit"), "Out For Return")
		# "unbooked" must not become "Booked"
		self.assertEqual(match_status("Unbooked"), "Unbooked")

	def test_unknown_text_is_not_guessed_from_history(self):
		mapped = map_status("Something New", [{"transactionStatusMessageCode": "0005"}])
		self.assertEqual(mapped, "Unknown")

	def test_history_used_only_when_status_empty(self):
		self.assertEqual(
			map_status("", [{"transactionStatusMessageCode": "0005"}]), "Delivered"
		)
		self.assertEqual(
			map_status(None, [{"transactionStatusMessageCode": "0001"}]), "Booked"
		)
		# last known code wins
		self.assertEqual(
			map_status(
				"",
				[
					{"transactionStatusMessageCode": "0001"},
					{"transactionStatusMessageCode": "0013"},
				],
			),
			"Attempted",
		)

	def test_history_message_fallback(self):
		self.assertEqual(
			map_status("", [{"transactionStatusMessage": "Package Delivered"}]), "Delivered"
		)

	def test_every_mapped_value_is_a_stored_option(self):
		for raw in (
			"In Stock",
			"Delivery En-Route",
			"En-Route to PostEx warehouse",
			"Under Verification",
			"Return In-Transit",
			"Return",
			"Picked",
			"Lost",
			"Damage",
			"Stolen",
			"Cancelled",
		):
			self.assertIn(match_status(raw), STATUS_OPTIONS)

	def test_pickup_statuses_are_stored_options(self):
		for status in PICKUP_STATUSES:
			self.assertIn(status, STATUS_OPTIONS)

	def test_norm_reference(self):
		self.assertEqual(norm(" vm5314 "), "VM5314")
		self.assertEqual(norm(None), "")


if __name__ == "__main__":
	unittest.main()
