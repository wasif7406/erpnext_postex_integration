"""One-shot migration helpers (run manually, never automatically).

* `import_legacy_settings` copies the legacy Single `PostEx Settings` (one
  token for the whole site) into a `PostEx Account` per company and fills the
  mapping from the existing `- <ABBR>` accounts.
* `provision_default_accounts` creates the ledger accounts / transit warehouse
  a company is missing and maps them. It never overwrites a mapping the user
  already selected.
"""

import frappe
from frappe.utils import flt

from .constants import (
	REQUIRED_ROLES,
	ROLE_COST_CENTER,
	ROLE_DEBTORS,
	ROLE_DELIVERY_CHARGES,
	ROLE_FINANCE,
	ROLE_IN_TRANSIT,
	ROLE_STORES,
	ROLE_TAX_WITHHELD,
	ROLE_UNACCOUNTED,
	ROLE_UNSETTLED_COD,
)

DEFAULT_BASE_URL = "https://api.postex.pk"

# role -> (base name, root_type, account_type, parent group hints)
PROVISION_ACCOUNTS = {
	ROLE_FINANCE: ("Postex Finance", "Asset", "Bank", ("Bank Accounts", "Current Assets")),
	ROLE_DELIVERY_CHARGES: ("Delivery Charges", "Expense", "", ("Direct Expenses", "Expenses")),
	ROLE_TAX_WITHHELD: (
		"PostEx Tax Withheld (Advance Tax)",
		"Asset",
		"",
		("Current Assets",),
	),
	ROLE_UNSETTLED_COD: ("PostEx Unsettled COD", "Asset", "", ("Current Assets",)),
	ROLE_UNACCOUNTED: (
		"PostEx Unaccounted Amount",
		"Liability",
		"",
		("Current Liabilities",),
	),
}


# ---------------------------------------------------------------------------
# Legacy settings import
# ---------------------------------------------------------------------------
def import_legacy_settings(company=None):
	"""Create a PostEx Account per company from the legacy Single doctype."""
	report = {"companies": {}, "created": [], "mapped": [], "notes": []}

	legacy = None
	if frappe.db.exists("DocType", "PostEx Settings"):
		try:
			legacy = frappe.get_single("PostEx Settings")
		except Exception:
			report["notes"].append("PostEx Settings exists but could not be read")

	companies = [company] if company else frappe.get_all("Company", pluck="name")
	for company_name in companies:
		abbr = frappe.get_cached_value("Company", company_name, "abbr")
		existing = frappe.get_all(
			"PostEx Account", filters={"company": company_name}, pluck="name"
		)

		if existing:
			account = frappe.get_doc("PostEx Account", existing[0])
			report["companies"][company_name] = {"account": account.name, "created": False}
		else:
			account = frappe.get_doc(
				{
					"doctype": "PostEx Account",
					"account_name": f"PostEx - {abbr}",
					"company": company_name,
					"is_default": 1,
					"enabled": 1,
					"enable_sync": 1,
					"base_url": (legacy.get("base_url") if legacy else None) or DEFAULT_BASE_URL,
					"default_weight": flt(legacy.get("default_weight") if legacy else 0) or 0.5,
					"enable_auto_booking": int(legacy.get("enable_auto_booking") or 0) if legacy else 0,
				}
			)
			token = None
			if legacy:
				token = legacy.get_password("api_token", raise_exception=False)
			if token:
				account.api_token = token
			account.flags.ignore_mandatory = True
			account.insert(ignore_permissions=True)
			report["created"].append(account.name)
			report["companies"][company_name] = {"account": account.name, "created": True}

		filled = _fill_mapping_from_existing(account, abbr)
		if filled:
			report["mapped"].append(f"{account.name}: {', '.join(filled)}")
		frappe.db.commit()

	if legacy and not report["created"]:
		report["notes"].append(
			"Legacy PostEx Settings left untouched; disable the old scheduler script "
			"and Scheduled Job Type once the app has run a shadow period."
		)
	return report


def _fill_mapping_from_existing(account, abbr):
	"""Map only roles that are still empty, by the classic 'Name - ABBR' pattern."""
	candidates = {
		ROLE_FINANCE: f"Postex Finance - {abbr}",
		ROLE_DEBTORS: f"Debtors - {abbr}",
		ROLE_DELIVERY_CHARGES: f"Delivery Charges - {abbr}",
		ROLE_TAX_WITHHELD: f"PostEx Tax Withheld (Advance Tax) - {abbr}",
		ROLE_UNSETTLED_COD: f"PostEx Unsettled COD - {abbr}",
		ROLE_UNACCOUNTED: f"PostEx Unaccounted Amount - {abbr}",
	}
	warehouses = {
		ROLE_IN_TRANSIT: f"PostEx In Transit - {abbr}",
		ROLE_STORES: f"Stores - {abbr}",
	}
	cost_center = frappe.get_cached_value("Company", account.company, "cost_center")

	present = {row.role for row in account.mapping or []}
	filled = []

	def add(role, **values):
		if role in present:
			return
		account.append("mapping", {"role": role, **values})
		filled.append(role)

	for role, name in candidates.items():
		if frappe.db.exists("Account", name):
			company = frappe.db.get_value("Account", name, "company")
			if company == account.company:
				add(role, account=name)
	for role, name in warehouses.items():
		if frappe.db.exists("Warehouse", name):
			company = frappe.db.get_value("Warehouse", name, "company")
			if company == account.company:
				add(role, warehouse=name)
	if cost_center:
		add(ROLE_COST_CENTER, cost_center=cost_center)

	if filled:
		try:
			account.flags.ignore_mandatory = True
			account.save(ignore_permissions=True)
		except Exception as exc:
			frappe.log_error(
				title="PostEx: mapping fill failed",
				message=f"{account.name}: {exc}\n{frappe.get_traceback()}",
			)
			return []
	return filled


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------
def provision_default_accounts(account):
	"""Create the missing accounts/warehouse for an account and map them."""
	doc = frappe.get_doc("PostEx Account", account)
	company = doc.company
	abbr = frappe.get_cached_value("Company", company, "abbr")
	report = {"created": [], "mapped": [], "missing": [], "account": doc.name}

	present = {row.role: row for row in doc.mapping or []}

	# -- ledger accounts ---------------------------------------------------
	for role, (base_name, root_type, account_type, parents) in PROVISION_ACCOUNTS.items():
		row = present.get(role)
		if row and (row.account or "").strip():
			continue
		name = f"{base_name} - {abbr}"
		if not frappe.db.exists("Account", name):
			parent = _find_group_account(company, abbr, root_type, parents)
			if not parent:
				report["missing"].append(
					f"{role}: no group account of root type {root_type} for {company}"
				)
				continue
			try:
				new_account = frappe.get_doc(
					{
						"doctype": "Account",
						"account_name": base_name,
						"parent_account": parent,
						"company": company,
						"account_type": account_type,
						"is_group": 0,
					}
				).insert(ignore_permissions=True)
				report["created"].append(new_account.name)
			except Exception as exc:
				report["missing"].append(f"{role}: {str(exc)[:140]}")
				continue
		_map_role(doc, present, role, account=name, report=report)

	# -- debtors -----------------------------------------------------------
	row = present.get(ROLE_DEBTORS)
	if not row or not (row.account or "").strip():
		debtors = frappe.get_cached_value("Company", company, "default_receivable_account")
		if not debtors:
			debtors = frappe.db.get_value(
				"Account",
				{"company": company, "account_type": "Receivable", "is_group": 0},
				"name",
			)
		if debtors:
			_map_role(doc, present, ROLE_DEBTORS, account=debtors, report=report)
		else:
			report["missing"].append("Debtors: no Receivable account found")

	# -- warehouses --------------------------------------------------------
	row = present.get(ROLE_IN_TRANSIT)
	if not row or not (row.warehouse or "").strip():
		name = f"PostEx In Transit - {abbr}"
		if not frappe.db.exists("Warehouse", name):
			parent = frappe.db.get_value(
				"Warehouse", {"company": company, "is_group": 1, "parent_warehouse": ""}, "name"
			) or frappe.db.get_value("Warehouse", {"company": company, "is_group": 1}, "name")
			if parent:
				try:
					new_warehouse = frappe.get_doc(
						{
							"doctype": "Warehouse",
							"warehouse_name": "PostEx In Transit",
							"company": company,
							"parent_warehouse": parent,
							"warehouse_type": "Transit",
							"is_group": 0,
						}
					).insert(ignore_permissions=True)
					name = new_warehouse.name
					report["created"].append(name)
				except Exception as exc:
					report["missing"].append(f"In-Transit Warehouse: {str(exc)[:140]}")
			else:
				report["missing"].append("In-Transit Warehouse: no parent group warehouse")
		if frappe.db.exists("Warehouse", name):
			_map_role(doc, present, ROLE_IN_TRANSIT, warehouse=name, report=report)

	row = present.get(ROLE_STORES)
	if not row or not (row.warehouse or "").strip():
		stores = f"Stores - {abbr}"
		if not frappe.db.exists("Warehouse", stores):
			stores = (
				frappe.db.get_value(
					"Warehouse",
					{"company": company, "is_group": 0, "warehouse_type": ("!=", "Transit")},
					"name",
				)
				or None
			)
		if stores:
			_map_role(doc, present, ROLE_STORES, warehouse=stores, report=report)
		else:
			report["missing"].append("Stores Warehouse: no leaf warehouse found")

	# -- cost center -------------------------------------------------------
	row = present.get(ROLE_COST_CENTER)
	if not row or not (row.cost_center or "").strip():
		cost_center = frappe.get_cached_value("Company", company, "cost_center")
		if cost_center:
			_map_role(doc, present, ROLE_COST_CENTER, cost_center=cost_center, report=report)

	if report["mapped"]:
		try:
			doc.flags.ignore_mandatory = True
			doc.save(ignore_permissions=True)
			frappe.db.commit()
		except Exception as exc:
			report["missing"].append(f"save mapping: {str(exc)[:200]}")

	missing_roles = [role for role in REQUIRED_ROLES if role not in {r.role for r in doc.mapping}]
	report["unmapped_roles"] = missing_roles
	return report


def _map_role(doc, present, role, report, **values):
	if role in present:
		row = present[role]
		for key, value in values.items():
			setattr(row, key, value)
		if role not in report["mapped"]:
			report["mapped"].append(role)
		return
	doc.append("mapping", {"role": role, **values})
	report["mapped"].append(role)


def _find_group_account(company, abbr, root_type, parents):
	for hint in parents:
		exact = f"{hint} - {abbr}"
		if frappe.db.exists("Account", exact):
			if frappe.db.get_value("Account", exact, "is_group"):
				return exact
	name = frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 1, "root_type": root_type},
		"name",
		order_by="lft asc",
	)
	return name
