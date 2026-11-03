"""Booking safety: never retry a POST blindly, and pick the right booking back.

Pinned regressions (04-11 audit):

* `add-order` used to be retried like a GET, so a timeout after PostEx created
  the order booked a second parcel;
* the recovery lookup must never adopt a cancelled/expired parcel, a booking
  without a tracking number, or another order's reference.

No site data is needed.
"""

import unittest
from unittest.mock import MagicMock, patch

import requests

from postex_integration import client as client_module
from postex_integration.booking import _choose_booking
from postex_integration.client import PostExClient
from postex_integration.exceptions import PostExAPIError


class FakeAccount(dict):
	"""The bits of a PostEx Account that `PostExClient` reads."""

	def __init__(self):
		super().__init__()
		self.name = "PEX-ACC-1"
		self.company = "Aqreno Nova"

	def get_password(self, fieldname, raise_exception=False):
		return "token"


def response(status_code, body=None):
	resp = MagicMock()
	resp.status_code = status_code
	resp.json.return_value = body if body is not None else {"statusCode": status_code}
	return resp


class TestPostRetryPolicy(unittest.TestCase):
	def setUp(self):
		self.client = PostExClient(FakeAccount())

	def send(self, method):
		return self.client._send(method, "/services/integration/api/order/v1/add-order", json={})

	def test_post_is_never_retried_on_5xx(self):
		with patch.object(client_module.requests, "request", return_value=response(500)) as call:
			with patch("time.sleep"):
				with self.assertRaises(PostExAPIError) as ctx:
					self.send("POST")
		self.assertEqual(call.call_count, 1)
		self.assertTrue(ctx.exception.ambiguous)

	def test_post_is_never_retried_on_a_timeout(self):
		with patch.object(
			client_module.requests, "request", side_effect=requests.exceptions.ReadTimeout("x")
		) as call:
			with patch("time.sleep"):
				with self.assertRaises(PostExAPIError) as ctx:
					self.send("POST")
		self.assertEqual(call.call_count, 1)
		# The request may have reached PostEx: the caller must reconcile.
		self.assertTrue(ctx.exception.ambiguous)

	def test_post_rejection_is_not_ambiguous(self):
		with patch.object(
			client_module.requests,
			"request",
			return_value=response(400, {"statusCode": 400, "statusMessage": "bad order"}),
		) as call:
			with patch("time.sleep"):
				with self.assertRaises(PostExAPIError) as ctx:
					self.send("POST")
		self.assertEqual(call.call_count, 1)
		self.assertFalse(ctx.exception.ambiguous)

	def test_get_is_still_retried(self):
		with patch.object(client_module.requests, "get", return_value=response(500)) as call:
			with patch("time.sleep"):
				with self.assertRaises(PostExAPIError):
					self.client.get("/services/integration/api/order/v1/get-all-order")
		self.assertEqual(call.call_count, 3)

	def test_put_is_still_retried(self):
		with patch.object(client_module.requests, "request", return_value=response(500)) as call:
			with patch("time.sleep"):
				with self.assertRaises(PostExAPIError):
					self.client._send("PUT", "/services/integration/api/order/v1/cancel-order", json={})
		self.assertEqual(call.call_count, 3)


def item(ref, tracking=None, status="UnBooked", date="2026-11-03 10:00:00"):
	return {
		"orderRefNumber": ref,
		"trackingNumber": tracking,
		"transactionStatus": status,
		"transactionDate": date,
	}


class TestChooseBooking(unittest.TestCase):
	def test_matches_the_reference_case_insensitively(self):
		chosen = _choose_booking([item("vm5318", "T1")], "VM5318")
		self.assertEqual(chosen["trackingNumber"], "T1")

	def test_ignores_other_references(self):
		self.assertIsNone(_choose_booking([item("VM0001", "T1")], "VM5318"))

	def test_ignores_a_booking_without_a_tracking_number(self):
		self.assertIsNone(_choose_booking([item("VM5318", None)], "VM5318"))

	def test_ignores_cancelled_and_expired_parcels(self):
		items = [
			item("VM5318", "T-OLD", status="Cancelled", date="2026-11-01 10:00:00"),
			item("VM5318", "T-EXP", status="Expired", date="2026-11-02 10:00:00"),
		]
		self.assertIsNone(_choose_booking(items, "VM5318"))

	def test_prefers_the_newest_live_booking(self):
		items = [
			item("VM5318", "T-OLD", date="2026-11-01 10:00:00"),
			item("VM5318", "T-NEW", date="2026-11-03 10:00:00"),
			item("VM5318", "T-MID", date="2026-11-02 10:00:00"),
		]
		self.assertEqual(_choose_booking(items, "VM5318")["trackingNumber"], "T-NEW")

	def test_empty_reference_never_matches(self):
		self.assertIsNone(_choose_booking([item("", "T1")], ""))


if __name__ == "__main__":
	unittest.main()
