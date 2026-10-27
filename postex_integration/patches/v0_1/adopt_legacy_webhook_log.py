import frappe


def execute():
	"""Adopt a UI-created 'PostEx Webhook Log' so the app can own it.

	Deliberately non-fatal: a migrated site must never get stuck because this
	bookkeeping step failed (the doctype already works either way).
	"""
	try:
		from postex_integration.install import adopt_legacy_doctypes

		adopted = adopt_legacy_doctypes()
		if adopted:
			frappe.clear_cache()
	except Exception:
		frappe.log_error(
			title="PostEx: webhook log adoption patch failed",
			message=frappe.get_traceback(),
		)
