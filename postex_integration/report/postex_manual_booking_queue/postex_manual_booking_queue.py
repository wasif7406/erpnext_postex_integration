"""Sales Orders that still need a PostEx booking — the manual-review worklist.

Why it exists: when an account turns on **Only Auto-Book Low Return Risk**, an
order at or above the limit (or with no score at all) is deliberately *not*
booked. Nothing is created, so the order would otherwise sit invisible until
someone noticed. This is the list to work: call the customer, then press
"Create PostEx Booking" on the order.

Read-only. A row means "no courier parcel exists for this order yet", with the
risk score and the reason it was held, so the caller knows why it is in front of
them.

Columns: Sales Order, Date, Customer, Phone, City, Grand Total, Return Risk %,
Risk Band, Held Reason.
"""

import frappe
from frappe import _
from frappe.utils import flt

# The order is still open and has no parcel: that is what "awaiting booking" means.
_NOT_CLOSED = ("Closed", "Completed", "Cancelled")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.get("company"):
		frappe.throw(_("Company is required."))

	columns = _columns()
	rows = _rows(filters)
	return columns, rows


def _columns():
	return [
		{"fieldname": "sales_order", "label": _("Sales Order"), "fieldtype": "Link",
		 "options": "Sales Order", "width": 150},
		{"fieldname": "transaction_date", "label": _("Date"), "fieldtype": "Date", "width": 95},
		{"fieldname": "customer", "label": _("Customer"), "fieldtype": "Link",
		 "options": "Customer", "width": 160},
		{"fieldname": "customer_name", "label": _("Customer Name"), "fieldtype": "Data", "width": 170},
		{"fieldname": "phone", "label": _("Phone"), "fieldtype": "Data", "width": 120},
		{"fieldname": "city", "label": _("City"), "fieldtype": "Data", "width": 110},
		{"fieldname": "grand_total", "label": _("COD"), "fieldtype": "Currency", "width": 100},
		{"fieldname": "risk_pct", "label": _("Return Risk %"), "fieldtype": "Percent", "width": 110},
		{"fieldname": "risk_band", "label": _("Risk Band"), "fieldtype": "Data", "width": 110},
		{"fieldname": "held_reason", "label": _("Held Reason"), "fieldtype": "Data", "width": 320},
	]


def _has(fieldname):
	return frappe.get_meta("Sales Order").has_field(fieldname)


def _rows(filters):
	conditions = [
		"so.docstatus = 1",
		"so.company = %(company)s",
		"ifnull(so.custom_postex_tracking_number, '') = ''",
		"ifnull(so.custom_courier, '') in ('', 'PostEx')",
		"so.status not in %(not_closed)s",
	]
	params = {
		"company": filters.company,
		"not_closed": tuple(_NOT_CLOSED),
		"min_risk": flt(filters.get("min_risk")),
	}

	if _has("custom_shopify_state"):
		conditions.append("ifnull(so.custom_shopify_state, '') = ''")
	if _has("custom_postex_booking_cancelled"):
		conditions.append("ifnull(so.custom_postex_booking_cancelled, 0) = 0")

	risk_pct = "ifnull(so.custom_return_risk_pct, 0)" if _has("custom_return_risk_pct") else "0"
	risk_band = "ifnull(so.custom_return_risk_band, '')" if _has("custom_return_risk_band") else "''"
	held = "ifnull(so.custom_postex_review_reason, '')" if _has("custom_postex_review_reason") else "''"

	# Both are optional on a site that has neither the AI app nor a contact field.
	phone = "coalesce(nullif(so.contact_phone, ''), addr.phone)" if _has("contact_phone") else "addr.phone"
	if _has("custom_ship_city"):
		city = "ifnull(nullif(so.custom_ship_city, ''), addr.city)"
	else:
		city = "addr.city"

	if filters.get("only_held"):
		conditions.append(f"{held} != ''")
	if params["min_risk"] > 0:
		conditions.append(f"{risk_pct} >= %(min_risk)s")

	sql = f"""
		select
			so.name as sales_order,
			so.transaction_date,
			so.customer,
			so.customer_name,
			{phone} as phone,
			{city} as city,
			so.grand_total,
			{risk_pct} as risk_pct,
			{risk_band} as risk_band,
			{held} as held_reason
		from `tabSales Order` so
		left join `tabAddress` addr on addr.name = so.shipping_address_name
		where {' and '.join(conditions)}
		order by {risk_pct} desc, so.transaction_date asc
		limit 2000
	"""
	return frappe.db.sql(sql, params, as_dict=True)
