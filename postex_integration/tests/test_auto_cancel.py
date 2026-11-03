"""Auto-cancel on Sales Order cancel (opt-in, off by default).

The hook must never cancel anything by accident: only a PostEx booking, only
when the account switched `enable_auto_cancel` on, and never for an order that
is already cancelled/untracked/non-PostEx.
"""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from postex_integration import booking, hooks


class FakeDoc(dict):
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		self.name = kwargs.get("name", "SAL-ORD-2026-0001")
		self.company = kwargs.get("company", "Aqreno Nova")


class FakeAccount(dict):
	def __init__(self, enabled=1, **kwargs):
		super().__init__(**kwargs)
		self.name = "PEX-ACC-0054"
		self.enabled = enabled


def run_hook(doc, account=None, companies=True):
	"""Call the hook with the config lookups and the queue replaced."""
	enqueued = []
	account = account if account is not None else FakeAccount(enable_auto_cancel=1)
	with (
		patch(
			"postex_integration.config.accounts_for_company",
			return_value=[object] if companies else [],
		),
		patch.object(booking, "account_for_document", return_value=account),
		patch.object(booking.frappe, "enqueue", create=True, side_effect=lambda *a, **k: enqueued.append((a, k))),
	):
		booking.on_sales_order_cancel(doc)
	return enqueued


def order(**overrides):
	data = {
		"custom_courier": "PostEx",
		"custom_postex_tracking_number": "11790017",
		"custom_postex_booking_cancelled": 0,
	}
	data.update(overrides)
	return FakeDoc(**data)


class TestAutoCancelSetting(unittest.TestCase):
	def test_the_toggle_defaults_to_off(self):
		path = (
			Path(booking.__file__).parent
			/ "postex_integration"
			/ "doctype"
			/ "postex_account"
			/ "postex_account.json"
		)
		meta = json.loads(path.read_text())
		field = next(f for f in meta["fields"] if f["fieldname"] == "enable_auto_cancel")
		self.assertEqual(str(field.get("default", "0")), "0")

	def test_the_sales_order_cancel_hook_is_registered(self):
		self.assertEqual(
			hooks.doc_events["Sales Order"]["on_cancel"],
			"postex_integration.booking.on_sales_order_cancel",
		)


class TestOnSalesOrderCancel(unittest.TestCase):
	def test_enqueues_the_cancel_when_switched_on(self):
		enqueued = run_hook(order())
		self.assertEqual(len(enqueued), 1)
		method, kwargs = enqueued[0]
		self.assertEqual(method[0], "postex_integration.booking.cancel_booking")
		self.assertEqual(kwargs["sales_order"], "SAL-ORD-2026-0001")
		self.assertEqual(kwargs["source"], "auto")

	def test_does_nothing_when_the_toggle_is_off(self):
		enqueued = run_hook(order(), account=FakeAccount(enable_auto_cancel=0))
		self.assertEqual(enqueued, [])

	def test_ignores_another_courier(self):
		self.assertEqual(run_hook(order(custom_courier="Quick Delivery")), [])

	def test_ignores_an_order_without_a_booking(self):
		self.assertEqual(run_hook(order(custom_postex_tracking_number=None)), [])

	def test_ignores_an_already_cancelled_booking(self):
		self.assertEqual(run_hook(order(custom_postex_booking_cancelled=1)), [])

	def test_ignores_a_company_without_a_postex_account(self):
		self.assertEqual(run_hook(order(), companies=False), [])

	def test_ignores_a_disabled_account(self):
		enqueued = run_hook(order(), account=FakeAccount(enabled=0, enable_auto_cancel=1))
		self.assertEqual(enqueued, [])


if __name__ == "__main__":
	unittest.main()
