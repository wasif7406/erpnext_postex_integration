import frappe


def execute():
	"""Create the custom fields the integration needs, if missing.

	Non-fatal on purpose: a missing field breaks one feature, while an exception
	here would abort `bench migrate` and pin the site to the previous release.
	"""
	try:
		from postex_integration.fields import ensure_custom_fields

		ensure_custom_fields()
	except Exception:
		frappe.log_error(
			title="PostEx: custom field patch failed",
			message=frappe.get_traceback(),
		)
