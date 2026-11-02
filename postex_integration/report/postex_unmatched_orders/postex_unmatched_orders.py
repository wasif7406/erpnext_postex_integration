"""Every eligible PostEx order that PostEx has no record for — by name, with a reason.

Read-only worklist: nothing here writes. The sync's phone+amount heuristic was
deliberately removed (it produced the duplicate invoices cleaned up on 02-11),
so ambiguity is surfaced for a human to resolve instead of being guessed.

Reason vocabulary
-----------------
* **No reference**   — no `po_no` and no `shopify_order_number`: PostEx can never
                       match this order, whatever the courier says.
* **Not yet booked** — never polled by the sync (usually just submitted).
* **Ref unknown**    — has a tracking number, but PostEx returned no status for it
                       (booked elsewhere, cancelled, or a different courier).
* **No tracking**    — polled at PostEx and no tracking number came back.
"""

import frappe
from frappe import _
from frappe.utils import add_days, flt, nowdate

REASONS = ("No reference", "Ref unknown", "No tracking", "Not yet booked")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.get("company"):
		frappe.throw(_("Company is required."))

	rows = _rows(filters)
	data = []
	for row in rows:
		reason = _reason(row)
		if filters.get("reason") and reason != filters.get("reason"):
			continue
		data.append(
			{
				"sales_order": row.name,
				"customer_name": row.customer_name,
				"transaction_date": row.transaction_date,
				"status": row.status,
				"custom_courier": row.custom_courier,
				"custom_postex_tracking_number": row.custom_postex_tracking_number,
				"reference": row.po_no or row.shopify_order_number,
				"reason": reason,
				"custom_postex_last_attempt_on": row.custom_postex_last_attempt_on,
				"custom_postex_sync_failures": row.custom_postex_sync_failures,
				"grand_total": flt(row.grand_total),
			}
		)

	data.sort(key=lambda r: (REASONS.index(r["reason"]), r["transaction_date"] or ""))
	return get_columns(), data


def _rows(filters):
	cutoff = add_days(nowdate(), -int(filters.get("days") or 0))
	conditions = [
		"so.docstatus = 1",
		"so.company = %(company)s",
		"ifnull(so.custom_courier, '') in ('', 'PostEx')",
		"ifnull(so.custom_postex_status, '') in ('', 'Unknown')",
		"ifnull(so.custom_postex_needs_attention, 0) = 0",
	]
	params = {"company": filters.company, "cutoff": cutoff}
	if frappe.get_meta("Sales Order").has_field("custom_shopify_state"):
		conditions.append("ifnull(so.custom_shopify_state, '') = ''")
	if filters.get("postex_account"):
		conditions.append("so.custom_postex_account = %(account)s")
		params["account"] = filters.postex_account
	if int(filters.get("days") or 0):
		conditions.append("so.transaction_date <= %(cutoff)s")

	sql = f"""
		select so.name, so.customer_name, so.transaction_date, so.status,
		       so.custom_courier, so.custom_postex_tracking_number,
		       so.po_no, so.shopify_order_number,
		       so.custom_postex_last_attempt_on,
		       ifnull(so.custom_postex_sync_failures, 0) as custom_postex_sync_failures,
		       so.grand_total
		from `tabSales Order` so
		where {" and ".join(conditions)}
		order by so.transaction_date asc
		limit 5000
	"""
	return [frappe._dict(row) for row in frappe.db.sql(sql, params, as_dict=True)]


def _reason(row) -> str:
	if not (row.po_no or row.shopify_order_number):
		return "No reference"
	if not row.custom_postex_last_attempt_on:
		return "Not yet booked"
	if row.custom_postex_tracking_number:
		return "Ref unknown"
	return "No tracking"


def get_columns():
	return [
		{"label": _("Sales Order"), "fieldname": "sales_order", "fieldtype": "Link", "options": "Sales Order", "width": 150},
		{"label": _("Customer"), "fieldname": "customer_name", "fieldtype": "Data", "width": 170},
		{"label": _("Date"), "fieldname": "transaction_date", "fieldtype": "Date", "width": 95},
		{"label": _("Order Status"), "fieldname": "status", "fieldtype": "Data", "width": 140},
		{"label": _("Courier"), "fieldname": "custom_courier", "fieldtype": "Data", "width": 110},
		{"label": _("Tracking"), "fieldname": "custom_postex_tracking_number", "fieldtype": "Data", "width": 150},
		{"label": _("Reference"), "fieldname": "reference", "fieldtype": "Data", "width": 130},
		{"label": _("Reason"), "fieldname": "reason", "fieldtype": "Data", "width": 130},
		{"label": _("Last Attempt"), "fieldname": "custom_postex_last_attempt_on", "fieldtype": "Datetime", "width": 150},
		{"label": _("Failures"), "fieldname": "custom_postex_sync_failures", "fieldtype": "Int", "width": 70},
		{"label": _("Total"), "fieldname": "grand_total", "fieldtype": "Currency", "width": 110},
	]
