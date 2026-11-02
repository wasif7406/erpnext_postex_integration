"""Scheduled entry points and the per-account lock helper.

Each cron tick only enqueues one short job per account; the heavy lifting and
the lock live in the enqueued function, so a slow PostEx API for company A can
never delay company B — and a manual run can never overlap a cron run of the
same account.
"""

import html

import frappe
from frappe.utils import add_days, add_to_date, flt, get_datetime, now_datetime


def sync_all_accounts():
	_enqueue_for_all("postex_integration.sync.sync_account_locked", "postex-sync")


def settle_all_accounts():
	_enqueue_for_all("postex_integration.settlement.reconcile_account_locked", "postex-settle")


def late_return_sweep():
	"""Daily: re-check Delivered/Returned orders inside the 45-day window.

	Late returns are real (a parcel can be refused days after "Delivered"), but
	everything older than the window is final, so this is the only job that ever
	touches a terminal order — and only once a day.
	"""
	_enqueue_for_all("postex_integration.sync.sync_late_returns_locked", "postex-late")


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


def webhook_watchdog():
	"""Alert when an account has received no webhook in 24 hours.

	A silent webhook is invisible in normal operation: every status change still
	arrives eventually through the poll, just minutes-to-hours late. This is the
	only signal that the dashboard URL is wrong or was removed.
	"""
	cutoff = add_to_date(now_datetime(), hours=-24)
	for row in _enabled_accounts():
		created = frappe.db.get_value("PostEx Account", row.name, "creation")
		if created and get_datetime(created) > get_datetime(cutoff):
			continue  # too new to judge
		try:
			received = frappe.db.count(
				"PostEx Webhook Log",
				{"postex_account": row.name, "creation": (">=", cutoff)},
			)
			if received:
				continue
			message = (
				f"PostEx Account {row.name} ({row.company}) received no webhook call in the "
				"last 24 hours while status sync is enabled.\n\n"
				"Check that the URL registered in the PostEx merchant dashboard matches "
				"'Show webhook URL' on the account (account name + secret), then send a test "
				"update.\n\nUntil it is fixed every status change waits for the 15-minute poll."
			)
			frappe.log_error(title="PostEx webhook silent for 24h", message=message)
			_notify(f"PostEx webhook silent — {row.name}", message)
		except Exception:
			frappe.log_error(
				title="PostEx webhook watchdog failed",
				message=f"{row.name}\n{frappe.get_traceback()}",
			)


def daily_digest():
	"""One message per account: is the sync covering the book, and is the cash right?"""
	for row in _enabled_accounts():
		try:
			_digest_account(row.name)
		except Exception:
			frappe.log_error(
				title="PostEx daily digest failed",
				message=f"{row.name}\n{frappe.get_traceback()}",
			)


def prune_logs():
	"""Keep the log doctypes small: 90 days of history is plenty."""
	cutoff = add_days(now_datetime().date(), -90)
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
# Digest
# ---------------------------------------------------------------------------
def _digest_account(account_name):
	from .config import AccountConfig, get_account
	from .constants import SCAN_ACTIVE, SCAN_LATE_RETURNS
	from .sync import count_eligible

	account = get_account(account_name)
	cutoff = add_to_date(now_datetime(), hours=-24)

	active = count_eligible(account, SCAN_ACTIVE)
	late = count_eligible(account, SCAN_LATE_RETURNS)

	runs = frappe.get_all(
		"PostEx Sync Run",
		filters={"postex_account": account_name, "creation": (">=", cutoff)},
		fields=["mode", "orders_seen", "status", "unmatched", "budget_exhausted"],
		limit=500,
	)
	scanned = sum(int(row.orders_seen or 0) for row in runs if (row.mode or SCAN_ACTIVE) != SCAN_LATE_RETURNS)
	failed_runs = len([row for row in runs if row.status == "Failed"])
	budget_hits = len([row for row in runs if row.budget_exhausted])

	unmatched = set()
	for row in runs:
		for name in (row.unmatched or "").split(","):
			name = name.strip()
			if name:
				unmatched.add(name)

	quarantined = frappe.db.count(
		"Sales Order",
		{
			"company": account.company,
			"custom_postex_needs_attention": 1,
			"modified": (">=", cutoff),
		},
	)

	# A full pass over the in-flight set at the observed scan rate.
	coverage = f"{active * 24.0 / scanned:.1f} h" if scanned else "no successful scans"

	documents = _documents_created(account_name, cutoff)

	try:
		config = AccountConfig(account)
		received = _cash_received(config, cutoff)
		expected = _cash_expected(account_name)
	except Exception:
		received, expected = 0.0, 0.0

	lines = [
		f"Account: {account.account_name} ({account.name}) — {account.company}",
		f"Eligible now: {active} active, {late} late-return candidates",
		f"Scanned in the last 24 h: {scanned} orders over {len(runs)} runs "
		f"(failed runs: {failed_runs}, budget hits: {budget_hits})",
		f"Time for one full pass at that rate: {coverage}",
		f"No PostEx record: {len(unmatched)} order(s)"
		+ (f" — {', '.join(sorted(unmatched)[:15])}" if unmatched else ""),
		f"Newly quarantined: {quarantined}",
		f"Documents created: {documents}",
		f"Cash expected (unsettled COD): {expected:,.2f}",
		f"Cash received into the wallet in 24 h: {received:,.2f}",
	]
	body = "\n".join(lines)
	_notify(f"PostEx daily digest — {account.account_name}", body)

	recipients = [
		email.strip() for email in (account.get("digest_recipients") or "").split(",") if email.strip()
	]
	if recipients:
		try:
			frappe.sendmail(
				recipients=recipients,
				subject=f"PostEx daily digest — {account.account_name}",
				message="<pre>" + html.escape(body) + "</pre>",
				now=False,
			)
		except Exception:
			frappe.log_error(
				title="PostEx digest email failed",
				message=f"{account_name}\n{frappe.get_traceback()}",
			)


def _documents_created(account_name, cutoff) -> str:
	label = f"[{account_name}]"
	counts = {
		"invoices": frappe.db.count(
			"Sales Invoice",
			{"custom_postex_account": account_name, "docstatus": 1, "creation": (">=", cutoff)},
		),
		"delivery notes": frappe.db.count(
			"Delivery Note",
			{"docstatus": 1, "creation": (">=", cutoff), "instructions": ("like", f"%{label}%")},
		),
		"stock entries": frappe.db.count(
			"Stock Entry",
			{"docstatus": 1, "creation": (">=", cutoff), "remark": ("like", f"%{label}%")},
		),
	}
	return ", ".join(f"{value} {name}" for name, value in counts.items() if value) or "none"


def _cash_expected(account_name) -> float:
	rows = frappe.db.sql(
		"""
		select ifnull(sum(outstanding_amount), 0)
		from `tabSales Invoice`
		where docstatus = 1
		  and custom_postex_account = %s
		  and ifnull(custom_courier_payment_status, '') = 'Not Paid'
		""",
		(account_name,),
	)
	return flt(rows[0][0]) if rows else 0.0


def _cash_received(config, cutoff) -> float:
	rows = frappe.db.sql(
		"""
		select ifnull(sum(paid_amount), 0)
		from `tabPayment Entry`
		where docstatus = 1 and paid_to = %s and creation >= %s
		""",
		(config.finance, cutoff),
	)
	return flt(rows[0][0]) if rows else 0.0


def _notify(subject, message):
	"""Desk notification (bell) for the System Manager."""
	try:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"subject": subject[:140],
				"email_content": message,
				"for_user": "Administrator",
				"type": "Alert",
			}
		).insert(ignore_permissions=True)
	except Exception:
		# A missing Notification Log must never break the job.
		pass


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
