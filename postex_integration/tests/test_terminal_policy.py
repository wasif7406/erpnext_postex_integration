"""Terminal outcomes: which courier statuses finish the order, and how.

Pinned regression (04-11 audit): only Delivered and Returned were handled, so a
lost / damaged / stolen / cancelled / expired parcel left its stock in the
transit warehouse and its Sales Order open forever, and a return that arrived
after invoicing left the revenue booked.

No site data is needed: the policy is a set of constants plus the SQL predicate.
"""

import unittest

from postex_integration.constants import (
	CLOSABLE_STATUSES,
	LOSS_STATUSES,
	STATUS_OPTIONS,
)
from postex_integration.stock import TERMINAL_WAITING


class TestTerminalPolicy(unittest.TestCase):
	def test_every_closable_status_is_a_stored_option(self):
		for status in CLOSABLE_STATUSES:
			with self.subTest(status=status):
				self.assertIn(status, STATUS_OPTIONS)

	def test_delivered_is_never_closed_by_the_terminal_handler(self):
		# Delivered is finished by the delivery note + invoice flow; ERPNext marks
		# it Completed itself. Closing it early would block the invoice.
		self.assertNotIn("Delivered", CLOSABLE_STATUSES)

	def test_losses_are_a_subset_of_the_terminal_set(self):
		self.assertTrue(set(LOSS_STATUSES).issubset(set(CLOSABLE_STATUSES)))

	def test_every_loss_status_is_a_stored_option(self):
		for status in LOSS_STATUSES:
			with self.subTest(status=status):
				self.assertIn(status, STATUS_OPTIONS)

	def test_terminal_scan_only_returns_unclosed_orders(self):
		# Without this the handler would re-run the stock leg / credit note forever.
		self.assertIn("custom_postex_closed_on", TERMINAL_WAITING)
		self.assertIn("custom_postex_status in %(closable)s", TERMINAL_WAITING)

	def test_a_booking_cancelled_from_this_app_is_never_closed(self):
		# The merchant cancelled the booking, not the sale: cancelling or shipping
		# the Sales Order with another courier is their decision.
		self.assertIn("custom_postex_booking_cancelled", TERMINAL_WAITING)

	def test_a_sale_with_documents_is_never_closed(self):
		# A cancelled/lost order that already has a delivery note or invoice is a
		# data contradiction; closing it would hide a real sale. Only `Returned`
		# is exempt, because that is the case the credit note exists for.
		self.assertIn("custom_postex_sales_invoice", TERMINAL_WAITING)
		self.assertIn("custom_postex_delivery_note", TERMINAL_WAITING)
		self.assertIn("per_delivered", TERMINAL_WAITING)
		self.assertIn("custom_postex_status = 'Returned'", TERMINAL_WAITING)


if __name__ == "__main__":
	unittest.main()
