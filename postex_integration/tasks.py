"""Scheduled entry points and the per-account lock helper.

Each cron tick only enqueues one short job per account; the heavy lifting and
the lock live in the enqueued function, so a slow PostEx API for company A can
never delay company B.
"""

import frappe


def sync_all_accounts():
	_enqueue_for_all("postex_integration.sync.sync_account", "postex-sync")


def settle_all_accounts():
	_enqueue_for_all("postex_integration.settlement.reconcile_account", "postex-settle")


def booking_sweep():
	from .booking import booking_sweep as sweep

	for row in _enabled_accounts():
		try:
			run_locked("sweep", row.name, sweep, account=row.name)
		except Exception:
			frappe.log_error(
				title="PostEx booking sweep failed",
				message=f"{row.name}\n{frappe.get_traceback()}",
			)


def prune_logs():
	"""Keep the log doctypes small: 90 days of history is plenty."""
	from frappe.utils import add_days, nowdate

	cutoff = add_days(nowdate(), -90)
	for doctype in ("PostEx Sync Run", "PostEx Booking Log", "PostEx Webhook Log"):
		if not frappe.db.exists("DocType", doctype):
			continue
		try:
			names = frappe.get_all(
				doctype,
				filters={"creation": ("<", cutoff)},
				pluck="name",
				limit=5000,
			)
			for name in names:
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
			if names:
				frappe.db.commit()
		except Exception:
			frappe.log_error(
				title="PostEx log pruning failed",
				message=f"{doctype}\n{frappe.get_traceback()}",
			)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _enabled_accounts():
	return frappe.get_all(
		"PostEx Account",
		filters={"enabled": 1, "enable_sync": 1},
		fields=["name", "company", "enable_auto_booking"],
	)


def _enqueue_for_all(method, job_prefix):
	for row in _enabled_accounts():
		try:
			frappe.enqueue(
				method,
				queue="long",
				timeout=1800,
				is_async=True,
				job_name=f"{job_prefix}-{row.name}",
				account=row.name,
			)
		except Exception:
			frappe.log_error(
				title="PostEx enqueue failed",
				message=f"{method} / {row.name}\n{frappe.get_traceback()}",
			)


def run_locked(kind, account_name, fn, **kwargs):
	"""Run `fn(account_name, **kwargs)` under a per-account, per-kind file lock."""
	from frappe.utils.synchronization import filelock

	try:
		from frappe.exceptions import LockTimeoutError
	except ImportError:  # very old Frappe
		from frappe.utils.synchronization import LockTimeoutError  # type: ignore

	try:
		with filelock(f"postex-{kind}-{account_name}", timeout=0):
			return fn(account_name, **kwargs)
	except LockTimeoutError:
		return {"skipped": f"{kind} already running for {account_name}"}
