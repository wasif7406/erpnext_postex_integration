"""Whitelisted endpoints used by buttons, desk calls and bench execute."""

import frappe
from frappe import _

from . import booking as booking_module
from . import settlement, sync
from .config import default_account, get_account
from .tasks import run_locked

WRITE_ROLES = ("System Manager", "Accounts Manager")


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------
@frappe.whitelist()
def book_order(sales_order, account=None):
	"""Manual booking from the Sales Order button.

	`account` is optional: with one enabled account for the company it can be
	omitted, with several the desk asks the user to pick one.
	"""
	if not sales_order:
		frappe.throw(_("A Sales Order is required."))
	if not frappe.has_permission("Sales Order", "write", doc=sales_order):
		frappe.throw(_("Not permitted to change this Sales Order."), frappe.PermissionError)
	return booking_module.book_order(sales_order, account=account, source="manual")


@frappe.whitelist()
def get_booking_accounts(sales_order):
	"""Enabled PostEx accounts of the order's company, for the booking dialog."""
	so = frappe.get_doc("Sales Order", sales_order)
	if not frappe.has_permission("Sales Order", "read", doc=so):
		frappe.throw(_("Not permitted to read this Sales Order."), frappe.PermissionError)
	accounts = frappe.get_all(
		"PostEx Account",
		filters={"company": so.company, "enabled": 1},
		fields=["name", "account_name", "is_default"],
		order_by="is_default desc, account_name asc",
	)
	return {
		"company": so.company,
		"current": so.get("custom_postex_account") or None,
		"accounts": accounts,
	}


@frappe.whitelist()
def assign_account(sales_order, account):
	"""Attach a Sales Order to a PostEx Account (multi-account companies)."""
	frappe.only_for(WRITE_ROLES)
	so = frappe.get_doc("Sales Order", sales_order)
	acc = get_account(account)
	if acc.company != so.company:
		frappe.throw(_("Account {0} belongs to {1}, not {2}.").format(acc.name, acc.company, so.company))
	if so.get("custom_postex_tracking_number"):
		clash = frappe.db.get_value(
			"Sales Order",
			{
				"custom_postex_account": acc.name,
				"custom_postex_tracking_number": so.custom_postex_tracking_number,
				"name": ("!=", so.name),
			},
			"name",
		)
		if clash:
			frappe.throw(
				_("Tracking {0} already belongs to {1} on this account.").format(
					so.custom_postex_tracking_number, clash
				)
			)
	frappe.db.set_value("Sales Order", so.name, "custom_postex_account", acc.name)
	frappe.db.commit()
	return {"ok": 1, "sales_order": so.name, "account": acc.name}


# ---------------------------------------------------------------------------
# Sync / settlement
# ---------------------------------------------------------------------------
@frappe.whitelist()
def sync_now(account=None, company=None, background=0):
	"""Run the sync for one account (or every account of a company)."""
	frappe.only_for(WRITE_ROLES)
	accounts = _resolve_accounts(account, company)
	if not accounts:
		frappe.throw(_("No enabled PostEx Account found for the given company/account."))

	if frappe.utils.cint(background):
		for row in accounts:
			frappe.enqueue(
				"postex_integration.sync.sync_account",
				queue="long",
				timeout=1800,
				job_name=f"postex-sync-{row}",
				account=row,
			)
		return {"queued": accounts}

	results = {}
	for row in accounts:
		results[row] = run_locked("sync", row, sync.sync_account)
	return results


@frappe.whitelist()
def reconcile_payouts(account=None, company=None):
	frappe.only_for(WRITE_ROLES)
	accounts = _resolve_accounts(account, company)
	if not accounts:
		frappe.throw(_("No enabled PostEx Account found for the given company/account."))
	results = {}
	for row in accounts:
		results[row] = run_locked("settle", row, settlement.reconcile_account)
	return results


@frappe.whitelist()
def run_all(company=None):
	"""Sync + settle one company in one call (manual catch-up)."""
	frappe.only_for(WRITE_ROLES)
	out = {"sync": sync_now(company=company), "settle": reconcile_payouts(company=company)}
	return out


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------
@frappe.whitelist()
def webhook_url(account):
	"""Return the guest endpoint for this account (includes its secret)."""
	frappe.only_for(("System Manager",))
	acc = get_account(account)
	secret = acc.get_password("webhook_secret", raise_exception=False)
	if not secret:
		frappe.throw(_("No webhook secret set on {0}.").format(acc.name))
	base = frappe.utils.get_url().rstrip("/")
	return {
		"url": f"{base}/api/method/postex_integration.webhook.receive/{acc.name}/{secret}",
		"account": acc.name,
		"company": acc.company,
	}


@frappe.whitelist()
def create_default_accounts(account):
	"""Provision the default accounts/warehouse for this account's company."""
	frappe.only_for(("System Manager",))
	from .migrate import provision_default_accounts

	return provision_default_accounts(account)


@frappe.whitelist()
def migrate_legacy_settings(company=None):
	"""One-shot import of the legacy Single `PostEx Settings` into PostEx Account."""
	frappe.only_for(("System Manager",))
	from .migrate import import_legacy_settings

	return import_legacy_settings(company=company)


@frappe.whitelist()
def health():
	"""Which code is this site actually running? (deploy verification)"""
	import postex_integration as package

	from .constants import BUILD

	return {
		"app": "postex_integration",
		"version": package.__version__,
		"build": BUILD,
		"features": {
			"delivery_income_account_fix": True,
			"settlement_outstanding_cap": True,
			"track_order_skips_other_couriers": True,
		},
	}


# ---------------------------------------------------------------------------
def _resolve_accounts(account, company):
	if account:
		acc = get_account(account)
		return [acc.name]
	if company:
		rows = frappe.get_all(
			"PostEx Account",
			filters={"company": company, "enabled": 1},
			pluck="name",
			order_by="is_default desc",
		)
		if rows:
			return rows
		return [default_account(company).name]
	rows = frappe.get_all("PostEx Account", filters={"enabled": 1}, pluck="name")
	return rows
