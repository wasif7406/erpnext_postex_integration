"""Install / migrate hooks.

`before_install` runs before the app's doctypes are synced, which is where
legacy UI-created doctypes with a name we also want to own have to be adopted;
after that point Frappe would see two definitions of the same DocType.
"""

import frappe

MODULE = "PostEx Integration"
APP = "postex_integration"

# Doctypes this app owns that a site may already have created through the UI
# (module "Custom"). They are converted to app-owned doctypes, keeping all rows.
ADOPTABLE_DOCTYPES = ("PostEx Webhook Log",)


def before_install():
	_ensure_module_def()
	adopt_legacy_doctypes()
	frappe.db.commit()


def after_install():
	from .fields import ensure_custom_fields

	ensure_custom_fields()
	frappe.db.commit()
	print(
		"\nPostEx Integration installed.\n"
		"Next steps:\n"
		"  1. Create a PostEx Account per company (PostEx Integration > PostEx Account).\n"
		"  2. Use 'Create default accounts' to provision the missing ledger accounts,\n"
		"     or select the existing ones in the Account Mapping table.\n"
		"  3. Set the API token and the webhook secret.\n"
		"  4. If you are migrating from the Server Scripts, run:\n"
		"     bench --site <site> execute postex_integration.migrate.import_legacy_settings\n"
		"     then disable the old Server Scripts and Scheduled Job Type.\n"
	)


def after_migrate():
	from .fields import ensure_custom_fields

	ensure_custom_fields()


# ---------------------------------------------------------------------------
def _ensure_module_def():
	"""Module Def may not exist yet when before_install runs."""
	if frappe.db.exists("Module Def", MODULE):
		return
	try:
		frappe.get_doc(
			{
				"doctype": "Module Def",
				"module_name": MODULE,
				"app_name": APP,
				"custom": 0,
			}
		).insert(ignore_permissions=True)
	except Exception:
		# If Module Def cannot be created here, Frappe will create it during
		# the doctype sync; adoption below simply becomes a no-op.
		frappe.log_error("PostEx: could not create Module Def", frappe.get_traceback())


def adopt_legacy_doctypes():
	"""Turn UI-created ('Custom') doctypes into app-owned ones, keeping rows."""
	adopted = []
	for doctype in ADOPTABLE_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		meta = frappe.db.get_value("DocType", doctype, ["module", "custom"], as_dict=True)
		if not meta or meta.module == MODULE:
			continue
		try:
			frappe.db.set_value(
				"DocType",
				doctype,
				{"module": MODULE, "custom": 0},
				update_modified=False,
			)
			adopted.append(doctype)
		except Exception:
			frappe.log_error(
				title="PostEx: could not adopt legacy doctype",
				message=f"{doctype}\n{frappe.get_traceback()}",
			)
	frappe.clear_cache()
	return adopted
