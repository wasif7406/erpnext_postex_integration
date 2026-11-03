"""Guard the stock/revenue helper queries.

The regression these tests pin: the helpers apply `LIMIT` in SQL, so a
quarantine check in Python lets a stuck order keep its slot on every run (its
`modified` is never bumped) until the newest 100 rows are all quarantined and no
document can be created again. Quarantine must therefore be part of the WHERE
clause, and the backlog counter must count exactly the rows the helpers select.

No site data is needed: the predicates are module constants.
"""

import unittest

from postex_integration.stock import (
	DELIVERY_WAITING,
	INVOICE_WAITING,
	PICKUP_WAITING,
	RETURN_WAITING,
	TERMINAL_WAITING,
	_backlog_specs,
	_where_owned,
)
from postex_integration.utils import exclude_quarantined_sql


class TestHelperQueries(unittest.TestCase):
	def test_quarantine_predicate_is_null_safe(self):
		# A plain `= 0` would silently drop every pre-field row (NULL) for good.
		self.assertEqual(
			exclude_quarantined_sql(), "ifnull(custom_postex_needs_attention, 0) = 0"
		)

	def test_shared_scope_excludes_quarantine_in_sql(self):
		where = _where_owned()
		self.assertIn("custom_postex_needs_attention", where)
		self.assertIn("ifnull(", where)

	def test_backlog_scope_can_see_quarantined_rows(self):
		# `blocked` is impossible to count if the counter reuses the helper filter.
		self.assertNotIn("needs_attention", _where_owned(allow_quarantined=True))
		self.assertIn("ifnull(custom_courier, '')", _where_owned(allow_quarantined=True))

	def test_backlog_counts_the_same_sets_the_helpers_select(self):
		specs = _backlog_specs()
		self.assertEqual(
			set(specs), {"pickup", "delivery", "invoice", "return", "terminal"}
		)
		# Identity, not equality: the counter and the helper must share one rule.
		self.assertIs(specs["pickup"][0], PICKUP_WAITING)
		self.assertIs(specs["delivery"][0], DELIVERY_WAITING)
		self.assertIs(specs["invoice"][0], INVOICE_WAITING)
		self.assertIs(specs["return"][0], RETURN_WAITING)
		self.assertIs(specs["terminal"][0], TERMINAL_WAITING)

	def test_work_sets_are_status_bounded(self):
		self.assertIn("custom_postex_status", PICKUP_WAITING)
		self.assertIn("custom_postex_status = 'Delivered'", DELIVERY_WAITING)
		self.assertIn("custom_postex_status = 'Delivered'", INVOICE_WAITING)
		self.assertIn("custom_postex_status = 'Returned'", RETURN_WAITING)

	def test_completed_work_is_never_reselected(self):
		# Each set must require the link field of its own document to be empty,
		# otherwise a helper would re-create what a previous run already made.
		self.assertIn("custom_postex_pickup_entry", PICKUP_WAITING)
		self.assertIn("custom_postex_delivery_note", DELIVERY_WAITING)
		self.assertIn("custom_postex_sales_invoice", INVOICE_WAITING)
		self.assertIn("custom_postex_return_entry", RETURN_WAITING)

	def test_return_set_requires_a_submitted_pickup(self):
		# A parcel that never left cannot come back. Keeping such rows in the set
		# made the loop skip them silently (no failure, no quarantine), so they
		# would have held a return slot on every run forever.
		self.assertIn("`tabStock Entry`", RETURN_WAITING)
		self.assertIn("se.docstatus = 1", RETURN_WAITING)


if __name__ == "__main__":
	unittest.main()
