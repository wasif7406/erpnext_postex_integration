"""Seed the new sync/scope fields on existing Sales Orders.

`custom_postex_status_changed_on` drives the 45-day late-return sweep, so old
Delivered/Returned orders need a value: `modified` is the best available
approximation (it is the timestamp the status was last written by the scripts).

`custom_postex_sync_failures` / `custom_postex_needs_attention` are Check/Int
columns added after the rows existed, so they are NULL; the queries treat NULL
as "not quarantined", but seeding 0 keeps the desk and the reports honest.

Non-fatal on purpose: a failure here must never abort `bench migrate`.
"""

import frappe


def execute():
	try:
		from postex_integration.fields import ensure_custom_fields

		ensure_custom_fields()
		frappe.db.commit()
	except Exception:
		frappe.log_error(
			title="PostEx: could not create fields before seeding",
			message=frappe.get_traceback(),
		)
		return

	meta = frappe.get_meta("Sales Order")
	if not meta.has_field("custom_postex_status_changed_on"):
		return

	try:
		frappe.db.sql(
			"""
			update `tabSales Order`
			set custom_postex_status_changed_on = modified
			where custom_postex_status_changed_on is null
			  and ifnull(custom_postex_status, '') != ''
			"""
		)
		frappe.db.sql(
			"""
			update `tabSales Order`
			set custom_postex_sync_failures = 0
			where custom_postex_sync_failures is null
			"""
		)
		frappe.db.sql(
			"""
			update `tabSales Order`
			set custom_postex_needs_attention = 0
			where custom_postex_needs_attention is null
			"""
		)
		frappe.db.commit()
	except Exception:
		frappe.log_error(
			title="PostEx: could not seed sync fields",
			message=frappe.get_traceback(),
		)
