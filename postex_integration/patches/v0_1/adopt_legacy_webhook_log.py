import frappe

from postex_integration.install import adopt_legacy_doctypes


def execute():
	"""Adopt a UI-created 'PostEx Webhook Log' so the app can own it."""
	adopted = adopt_legacy_doctypes()
	if adopted:
		frappe.clear_cache()
