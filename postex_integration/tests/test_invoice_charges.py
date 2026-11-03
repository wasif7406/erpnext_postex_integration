"""Copying the Sales Order tax block onto its Delivery Note / invoice.

Pinned regression (04-11 audit): the invoice was built by hand-picking five tax
fields (`charge_type / account_head / description / rate / cost_center`) and
omitting `tax_amount`. An `Actual` row keeps its amount in `tax_amount` with
`rate = 0`, so the charge was dropped — and the "invoice must equal the
delivery" check then deleted the invoice and raised, forever, for any Sales
Order with a non-zero tax.

No site data is needed: `get_meta` is replaced by a tiny field list.
"""

import types
import unittest
from unittest.mock import patch

from postex_integration import stock


class Row:
	def __init__(self, data):
		self.data = data

	def get(self, key):
		return self.data.get(key)


class Doc:
	def __init__(self, doctype, data=None, taxes=None):
		self.doctype = doctype
		self.data = dict(data or {})
		self.taxes = [Row(t) for t in (taxes or [])]
		self.appended = []
		self.values = {}

	def get(self, key):
		if key == "taxes":
			return self.taxes
		return self.data.get(key)

	def append(self, fieldname, values):
		self.appended.append((fieldname, values))

	def set(self, fieldname, value):
		self.values[fieldname] = value


class Meta:
	def __init__(self, fields):
		self.fields = [types.SimpleNamespace(fieldname=fieldname) for fieldname in fields]

	def has_field(self, name):
		return any(df.fieldname == name for df in self.fields)


TAX_FIELDS = (
	"charge_type",
	"account_head",
	"description",
	"rate",
	"tax_amount",
	"cost_center",
	"row_id",
	"included_in_print_rate",
	"name",
	"parent",
	"parentfield",
	"parenttype",
	"idx",
	"doctype",
	"owner",
)


def fake_meta(doctype):
	if doctype == "Sales Taxes and Charges":
		return Meta(TAX_FIELDS)
	if doctype == "Sales Invoice":
		return Meta(("apply_discount_on", "additional_discount_percentage", "discount_amount"))
	return Meta(())


class TestCopyCharges(unittest.TestCase):
	def copy(self, source, target):
		with patch.object(stock.frappe, "get_meta", fake_meta):
			stock._copy_charges(source, target)

	def test_an_actual_amount_is_copied(self):
		so = Doc(
			"Sales Order",
			taxes=[
				{
					"charge_type": "Actual",
					"account_head": "Indirect Income - AN",
					"description": "Shipping",
					"rate": 0,
					"tax_amount": 350,
					"name": "row-1",
					"parent": "SAL-ORD-1",
					"idx": 1,
				}
			],
		)
		si = Doc("Sales Invoice")
		self.copy(so, si)

		self.assertEqual(len(si.appended), 1)
		fieldname, values = si.appended[0]
		self.assertEqual(fieldname, "taxes")
		self.assertEqual(values["tax_amount"], 350)
		self.assertEqual(values["charge_type"], "Actual")

	def test_frappe_plumbing_is_not_copied(self):
		so = Doc("Sales Order", taxes=[{"charge_type": "Actual", "tax_amount": 1, "name": "r", "parent": "p", "idx": 2, "doctype": "Sales Taxes and Charges"}])
		si = Doc("Sales Invoice")
		self.copy(so, si)

		values = si.appended[0][1]
		for key in ("name", "parent", "idx", "doctype", "owner"):
			with self.subTest(key=key):
				self.assertNotIn(key, values)

	def test_the_document_discount_is_copied_where_the_field_exists(self):
		so = Doc(
			"Sales Order",
			{
				"apply_discount_on": "Grand Total",
				"additional_discount_percentage": 5,
				"discount_amount": 150,
			},
		)
		si = Doc("Sales Invoice")
		self.copy(so, si)

		self.assertEqual(si.values["discount_amount"], 150)
		self.assertEqual(si.values["additional_discount_percentage"], 5)

	def test_a_delivery_note_without_discount_fields_is_not_given_any(self):
		so = Doc("Sales Order", {"discount_amount": 150})
		dn = Doc("Delivery Note")
		self.copy(so, dn)
		self.assertEqual(dn.values, {})

	def test_an_order_without_taxes_adds_no_rows(self):
		so = Doc("Sales Order")
		si = Doc("Sales Invoice")
		self.copy(so, si)
		self.assertEqual(si.appended, [])


if __name__ == "__main__":
	unittest.main()
