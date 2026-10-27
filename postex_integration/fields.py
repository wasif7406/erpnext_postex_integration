"""Custom fields the integration needs on Sales Order / Sales Invoice.

They are created from code (not shipped as fixtures) so that installing the
app on an existing site never rewrites labels, positions or list settings the
merchant already customised. `ensure_custom_fields` is idempotent and only
creates what is missing.
"""

import frappe

from .constants import STATUS_OPTIONS

COURIER_OPTIONS = "\nPostEx\nQuick Delivery\nMissing Data\nFast And Quick"
PAYMENT_OPTIONS = "\nNot Paid\nPaid\nPaid (Approx)\nNo Payment Due"
STATUS_OPTIONS_TEXT = "\n" + "\n".join(STATUS_OPTIONS)

# Field order is driven purely by insert_after, so the list is ordered.
SALES_ORDER_FIELDS = [
	{
		"fieldname": "custom_postex_section",
		"label": "Courier Detail",
		"fieldtype": "Section Break",
		"insert_after": "column_break1",
		"print_hide": 1,
	},
	{
		"fieldname": "custom_courier",
		"label": "Courier Issued",
		"fieldtype": "Select",
		"options": COURIER_OPTIONS,
		"insert_after": "custom_postex_section",
		"in_list_view": 1,
		"in_standard_filter": 1,
	},
	{
		"fieldname": "custom_postex_account",
		"label": "PostEx Account",
		"fieldtype": "Link",
		"options": "PostEx Account",
		"insert_after": "custom_courier",
		"in_standard_filter": 1,
		"description": "Which PostEx merchant account books and settles this order (link filtered by company).",
	},
	{
		"fieldname": "custom_postex_status",
		"label": "Courier Status",
		"fieldtype": "Select",
		"options": STATUS_OPTIONS_TEXT,
		"insert_after": "custom_postex_account",
		"in_list_view": 1,
		"in_standard_filter": 1,
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_last_synced_on",
		"label": "Courier Last Synced On",
		"fieldtype": "Datetime",
		"insert_after": "custom_postex_status",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_delivery_fee",
		"label": "PostEx Delivery Fee",
		"fieldtype": "Currency",
		"insert_after": "custom_postex_last_synced_on",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_delivery_tax",
		"label": "PostEx Fee Tax",
		"fieldtype": "Currency",
		"insert_after": "custom_postex_delivery_fee",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_reversal_fee",
		"label": "PostEx Return Fee",
		"fieldtype": "Currency",
		"insert_after": "custom_postex_delivery_tax",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_reversal_tax",
		"label": "PostEx Return Fee Tax",
		"fieldtype": "Currency",
		"insert_after": "custom_postex_reversal_fee",
		"read_only": 1,
	},
	{
		"fieldname": "custom_create_postex_booking",
		"label": "Create PostEx Booking",
		"fieldtype": "Button",
		"insert_after": "custom_postex_reversal_tax",
	},
	{
		"fieldname": "custom_postex_col_break",
		"label": None,
		"fieldtype": "Column Break",
		"insert_after": "custom_create_postex_booking",
	},
	{
		"fieldname": "custom_postex_tracking_number",
		"label": "Courier Tracking ID",
		"fieldtype": "Data",
		"insert_after": "custom_postex_col_break",
		"in_standard_filter": 1,
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_pickup_entry",
		"label": "Courier Pickup Entry",
		"fieldtype": "Link",
		"options": "Stock Entry",
		"insert_after": "custom_postex_tracking_number",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_delivery_note",
		"label": "Courier Delivery Note",
		"fieldtype": "Link",
		"options": "Delivery Note",
		"insert_after": "custom_postex_pickup_entry",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_return_entry",
		"label": "Courier Return Entry",
		"fieldtype": "Link",
		"options": "Stock Entry",
		"insert_after": "custom_postex_delivery_note",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_return_charges_entry",
		"label": "Courier Return Charges Entry",
		"fieldtype": "Link",
		"options": "Journal Entry",
		"insert_after": "custom_postex_return_entry",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_sales_invoice",
		"label": "Courier Sales Invoice",
		"fieldtype": "Link",
		"options": "Sales Invoice",
		"insert_after": "custom_postex_return_charges_entry",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_payout_entry",
		"label": "Courier Payout Entry",
		"fieldtype": "Link",
		"options": "Payment Entry",
		"insert_after": "custom_postex_sales_invoice",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_settled_on",
		"label": "Courier Settled On",
		"fieldtype": "Date",
		"insert_after": "custom_postex_payout_entry",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_charges_entry",
		"label": "Courier Charges Entry",
		"fieldtype": "Link",
		"options": "Journal Entry",
		"insert_after": "custom_postex_settled_on",
		"read_only": 1,
	},
]

# No section break on Sales Invoice on purpose: sites that already use the
# single-company integration have an established layout, and adding a break in
# the middle of it only creates an empty-looking section.
SALES_INVOICE_FIELDS = [
	{
		"fieldname": "custom_postex_tracking_number",
		"label": "Courier Tracking ID",
		"fieldtype": "Data",
		"insert_after": "remarks",
		"in_standard_filter": 1,
		"read_only": 1,
	},
	{
		"fieldname": "custom_courier",
		"label": "Courier Issued",
		"fieldtype": "Select",
		"options": COURIER_OPTIONS,
		"insert_after": "custom_postex_tracking_number",
		"in_list_view": 1,
		"in_standard_filter": 1,
	},
	{
		"fieldname": "custom_postex_account",
		"label": "PostEx Account",
		"fieldtype": "Link",
		"options": "PostEx Account",
		"insert_after": "custom_courier",
		"in_standard_filter": 1,
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_status",
		"label": "Courier Status",
		"fieldtype": "Select",
		"options": STATUS_OPTIONS_TEXT,
		"insert_after": "custom_postex_account",
		"in_list_view": 1,
		"in_standard_filter": 1,
		"read_only": 1,
	},
	{
		"fieldname": "custom_courier_payment_status",
		"label": "Courier Payment",
		"fieldtype": "Select",
		"options": PAYMENT_OPTIONS,
		"insert_after": "custom_postex_status",
		"in_list_view": 1,
		"in_standard_filter": 1,
	},
	{
		"fieldname": "custom_courier_paid_on",
		"label": "Courier Paid On",
		"fieldtype": "Date",
		"insert_after": "custom_courier_payment_status",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_delivery_fee",
		"label": "PostEx Delivery Fee",
		"fieldtype": "Currency",
		"insert_after": "custom_courier_paid_on",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_delivery_tax",
		"label": "PostEx Fee Tax",
		"fieldtype": "Currency",
		"insert_after": "custom_postex_delivery_fee",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_settled_on",
		"label": "Courier Settled On",
		"fieldtype": "Date",
		"insert_after": "custom_postex_delivery_tax",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_payout_entry",
		"label": "Courier Payout Entry",
		"fieldtype": "Link",
		"options": "Payment Entry",
		"insert_after": "custom_postex_settled_on",
		"read_only": 1,
	},
	{
		"fieldname": "custom_postex_charges_entry",
		"label": "Courier Charges Entry",
		"fieldtype": "Link",
		"options": "Journal Entry",
		"insert_after": "custom_postex_payout_entry",
		"read_only": 1,
	},
]


def ensure_custom_fields():
	"""Create only the fields that are missing. Never updates existing ones."""
	created = []
	for doctype, specs in (
		("Sales Order", SALES_ORDER_FIELDS),
		("Sales Invoice", SALES_INVOICE_FIELDS),
	):
		for spec in specs:
			spec = {k: v for k, v in spec.items() if v is not None}
			name = f"{doctype}-{spec['fieldname']}"
			if frappe.db.exists("Custom Field", name):
				continue
			if frappe.get_meta(doctype).has_field(spec["fieldname"]):
				# Field exists as a standard field of a future ERPNext version.
				continue
			from frappe.custom.doctype.custom_field.custom_field import create_custom_field

			df = dict(spec)
			df["dt"] = doctype
			df["is_system_generated"] = 0
			try:
				create_custom_field(doctype, df, ignore_validate=True)
				created.append(name)
			except Exception:
				frappe.log_error(
					title="PostEx: could not create custom field",
					message=f"{name}\n{frappe.get_traceback()}",
				)
	frappe.clear_cache()
	return created
