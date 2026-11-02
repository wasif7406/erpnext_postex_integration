"""Delivered (and invoiced) but not yet paid by the courier, bucketed by age.

This is the cash the courier still holds. PostEx pays weekly, so a normal book
sits in 0-7 / 8-14; anything in 30+ is either a stuck settlement, an invoice
created for a parcel PostEx never delivered, or a payout that was never
allocated — exactly what needs a human.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from postex_integration.scope import AGE_BUCKETS, age_bucket


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.get("company"):
		frappe.throw(_("Company is required."))

	today = getdate(nowdate())
	rows = _rows(filters, today)

	data = []
	for row in rows:
		days = max(0, (today - getdate(row.delivered_on)).days if row.delivered_on else 0)
		bucket = age_bucket(days)
		data.append(
			{
				"bucket": bucket,
				"sales_order": row.sales_order,
				"sales_invoice": row.sales_invoice,
				"customer_name": row.customer_name,
				"custom_postex_tracking_number": row.custom_postex_tracking_number,
				"delivered_on": row.delivered_on,
				"age_days": days,
				"outstanding_amount": flt(row.outstanding_amount),
				"grand_total": flt(row.grand_total),
			}
		)

	if filters.get("bucket"):
		data = [row for row in data if row["bucket"] == filters.bucket]

	data.sort(key=lambda r: (-r["age_days"], r["sales_order"]))
	report_summary = [
		{
			"label": _("{0} days").format(bucket),
			"value": len([r for r in data if r["bucket"] == bucket]),
			"datatype": "Int",
			"indicator": "Red" if bucket == "30+" else "Orange" if bucket == "15-30" else "Blue",
		}
		for bucket in AGE_BUCKETS
	]
	report_summary.append(
		{
			"label": _("Outstanding"),
			"value": sum(r["outstanding_amount"] for r in data),
			"datatype": "Currency",
			"indicator": "Red",
		}
	)
	return get_columns(), data, None, None, report_summary


def _rows(filters, today):
	conditions = [
		"so.docstatus = 1",
		"so.company = %(company)s",
		"so.custom_postex_status = 'Delivered'",
		"ifnull(so.custom_postex_settled_on, '') = ''",
		"ifnull(so.custom_postex_sales_invoice, '') != ''",
	]
	params = {"company": filters.company}
	if filters.get("postex_account"):
		conditions.append("so.custom_postex_account = %(account)s")
		params["account"] = filters.postex_account

	sql = f"""
		select so.name as sales_order,
		       so.custom_postex_sales_invoice as sales_invoice,
		       so.customer_name,
		       so.custom_postex_tracking_number,
		       date(ifnull(so.custom_postex_status_changed_on, si.posting_date)) as delivered_on,
		       si.outstanding_amount,
		       si.grand_total
		from `tabSales Order` so
		join `tabSales Invoice` si on si.name = so.custom_postex_sales_invoice
		where {" and ".join(conditions)}
		  and si.docstatus = 1
		order by delivered_on asc
		limit 5000
	"""
	return [frappe._dict(row) for row in frappe.db.sql(sql, params, as_dict=True)]


def get_columns():
	return [
		{"label": _("Bucket"), "fieldname": "bucket", "fieldtype": "Data", "width": 80},
		{"label": _("Sales Order"), "fieldname": "sales_order", "fieldtype": "Link", "options": "Sales Order", "width": 150},
		{"label": _("Sales Invoice"), "fieldname": "sales_invoice", "fieldtype": "Link", "options": "Sales Invoice", "width": 160},
		{"label": _("Customer"), "fieldname": "customer_name", "fieldtype": "Data", "width": 170},
		{"label": _("Tracking"), "fieldname": "custom_postex_tracking_number", "fieldtype": "Data", "width": 150},
		{"label": _("Delivered On"), "fieldname": "delivered_on", "fieldtype": "Date", "width": 110},
		{"label": _("Age (days)"), "fieldname": "age_days", "fieldtype": "Int", "width": 90},
		{"label": _("Outstanding"), "fieldname": "outstanding_amount", "fieldtype": "Currency", "width": 120},
		{"label": _("Invoice Total"), "fieldname": "grand_total", "fieldtype": "Currency", "width": 120},
	]
