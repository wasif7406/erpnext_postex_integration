"""Whitelisted endpoints used by buttons, desk calls and bench execute."""

import re

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, nowdate

from . import booking as booking_module
from . import settlement, sync
from .config import account_for_document, default_account, get_account
from .constants import POSTEX_COURIER
from .tasks import run_locked
from .utils import map_status, norm

WRITE_ROLES = ("System Manager", "Accounts Manager")

# How many candidates the manual "link a booking" search returns.
BOOKING_SEARCH_LIMIT = 20


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
def cancel_booking(sales_order, reason=None, account=None):
	"""Cancel a PostEx booking (manual button).

	Only for an order that has not moved: no submitted pickup transfer, no
	delivery note, no invoice. The booking is cancelled at PostEx first; a 404
	there (already cancelled) still allows the local cancellation.
	"""
	if not sales_order:
		frappe.throw(_("A Sales Order is required."))
	if not frappe.has_permission("Sales Order", "write", doc=sales_order):
		frappe.throw(_("Not permitted to change this Sales Order."), frappe.PermissionError)
	return booking_module.cancel_booking(
		sales_order, account=account, reason=reason, source="manual"
	)


@frappe.whitelist()
def reopen_booking(sales_order):
	"""Undo a cancellation so the order re-enters the automatic sync."""
	if not sales_order:
		frappe.throw(_("A Sales Order is required."))
	if not frappe.has_permission("Sales Order", "write", doc=sales_order):
		frappe.throw(_("Not permitted to change this Sales Order."), frappe.PermissionError)
	return booking_module.reopen_booking(sales_order, source="manual")


@frappe.whitelist()
def clear_booking_doubt(sales_order):
	"""A human checked PostEx and the reference is not booked: allow a retry."""
	if not sales_order:
		frappe.throw(_("A Sales Order is required."))
	if not frappe.has_permission("Sales Order", "write", doc=sales_order):
		frappe.throw(_("Not permitted to change this Sales Order."), frappe.PermissionError)
	return booking_module.clear_booking_doubt(sales_order, source="manual")


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


# ---------------------------------------------------------------------------
# Manual matching: link an existing PostEx booking
# ---------------------------------------------------------------------------
@frappe.whitelist()
def find_bookings(sales_order, search=None):
	"""Search this account's PostEx feed for a booking to link by hand.

	The reference alone does not always identify an order (the old OMS writes
	`VM####`, Shopify writes its own number, the oldest orders use `aug27/8`),
	so the search accepts a reference, a phone number or a customer name.
	"""
	if not sales_order:
		frappe.throw(_("A Sales Order is required."))
	so = frappe.get_doc("Sales Order", sales_order)
	if not frappe.has_permission("Sales Order", "read", doc=so):
		frappe.throw(_("Not permitted to read this Sales Order."), frappe.PermissionError)

	from .client import PostExClient
	from .exceptions import PostExConfigError

	try:
		account_doc = account_for_document(so)
	except PostExConfigError as exc:
		frappe.throw(str(exc))

	window = int(account_doc.get("sync_window_days") or 60)
	items = PostExClient(account_doc).list_orders(add_days(nowdate(), -window), nowdate())

	text = (search or "").strip() or (
		so.get("shopify_order_number") or so.get("po_no") or so.name
	)
	key = norm(text)
	phone_query = re.sub(r"\D", "", text)
	phone_query = phone_query[-10:] if len(phone_query) >= 7 else ""
	name_query = text.lower()

	rows = []
	seen = set()
	for item in items:
		tracking = (item.get("trackingNumber") or "").strip()
		if not tracking or tracking in seen:
			continue
		reference = item.get("orderRefNumber") or ""
		phone = re.sub(r"\D", "", str(item.get("customerPhone") or ""))[-10:]
		customer = str(item.get("customerName") or "")

		if not (
			(key and key in norm(reference))
			or (phone_query and phone and phone_query == phone)
			or (name_query and name_query in customer.lower())
		):
			continue

		status = map_status(item.get("transactionStatus"))
		if status in ("Cancelled", "Expired"):
			continue  # a dead parcel is never worth linking

		seen.add(tracking)
		rows.append(
			{
				"reference": reference,
				"tracking_number": tracking,
				"status": status or (item.get("transactionStatus") or ""),
				"date": str(item.get("transactionDate") or "")[:10],
				"customer": customer,
				"phone": item.get("customerPhone") or "",
				"amount": flt(item.get("invoicePayment")),
				"linked_to": _tracking_owner(tracking, so.name),
			}
		)

	# newest first, then exact reference matches on top (stable sorts)
	rows.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
	rows.sort(key=lambda row: norm(row["reference"]) != key)
	return rows[:BOOKING_SEARCH_LIMIT]


@frappe.whitelist()
def link_booking(sales_order, tracking, account=None):
	"""Attach an existing PostEx booking to a Sales Order (manual match).

	The escape hatch for an order the sync could not match: PostEx is asked for
	the tracking (one call) so the status, fee and delivery date are written
	exactly like a normal sync would.
	"""
	if not sales_order or not tracking:
		frappe.throw(_("A Sales Order and a tracking number are required."))
	so = frappe.get_doc("Sales Order", sales_order)
	if not frappe.has_permission("Sales Order", "write", doc=so):
		frappe.throw(_("Not permitted to change this Sales Order."), frappe.PermissionError)
	if so.docstatus != 1:
		frappe.throw(_("Submit the Sales Order before linking a booking."))
	if (so.get("custom_postex_tracking_number") or "").strip():
		frappe.throw(_("This order already has a tracking number."))
	if cint(so.get("custom_postex_booking_cancelled")):
		frappe.throw(_("This booking was cancelled; use 'Reopen PostEx Booking' first."))

	tracking = str(tracking).strip().replace(" ", "")
	owner = _tracking_owner(tracking, so.name)
	if owner:
		frappe.throw(_("Tracking {0} already belongs to {1}.").format(tracking, owner))

	from .client import PostExClient
	from .exceptions import PostExAPIError, PostExConfigError
	from .sync import _write_payload
	from .utils import clear_failures

	try:
		account_doc = account_for_document(so, account=account)
	except PostExConfigError as exc:
		frappe.throw(str(exc))

	try:
		payload = PostExClient(account_doc).track_order(tracking)
	except PostExAPIError as exc:
		frappe.throw(str(exc)[:300])
	if not payload:
		frappe.throw(_("PostEx has no booking with tracking {0}.").format(tracking))
	payload_status = map_status(payload.get("transactionStatus"))
	if payload_status in ("Cancelled", "Expired"):
		frappe.throw(
			_("The booking with tracking {0} is {1} at PostEx; there is nothing to link.").format(
				tracking, payload_status
			)
		)

	result = {"tracking_filled": 0, "status_set": 0, "courier_filled": 0, "unchanged": 0}
	written = _write_payload(account_doc, frappe._dict(so.as_dict()), payload, "", result)
	if written is None:
		frappe.throw(_("That tracking number already belongs to another Sales Order."))

	if not so.get("custom_postex_account"):
		frappe.db.set_value("Sales Order", so.name, "custom_postex_account", account_doc.name)
	# a human just resolved what the sync could not: un-quarantine the order
	clear_failures(so.name)
	frappe.db.commit()

	so.add_comment(
		text=f"PostEx booking linked by hand: {written} on account {account_doc.name}"
	)
	# A Success row makes this a known booking: auto-booking can never duplicate it.
	booking_module._log(
		so,
		account_doc.name,
		so.get("shopify_order_number") or so.get("po_no") or so.name,
		"Success",
		{},
		payload,
		None,
		"manual",
		remarks="linked to an existing PostEx booking",
	)

	values = (
		frappe.db.get_value(
			"Sales Order",
			so.name,
			[
				"custom_postex_status",
				"custom_postex_delivered_on",
				"custom_postex_sales_invoice",
				"custom_postex_return_entry",
			],
			as_dict=True,
		)
		or {}
	)
	status = (values.get("custom_postex_status") or "").strip()
	return {
		"ok": 1,
		"tracking": written,
		"status": status,
		"delivered_on": str(values.get("custom_postex_delivered_on") or ""),
		"next_step": _next_step(status, values),
	}


def _tracking_owner(tracking, sales_order):
	"""The other Sales Order already carrying this tracking number, if any."""
	return frappe.db.get_value(
		"Sales Order",
		{"custom_postex_tracking_number": tracking, "name": ("!=", sales_order)},
		"name",
	)


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
			# `sync_account_locked`, never `sync_account`: the lock is what keeps a
			# backgrounded manual run from racing the cron into the same page (and
			# from creating the same delivery note / invoice twice). The job name is
			# distinct from the cron's so the two never collide as an RQ job id.
			frappe.enqueue(
				"postex_integration.sync.sync_account_locked",
				queue="long",
				timeout=1800,
				job_name=f"postex-sync-manual-{row}",
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
def resync_order(sales_order):
	"""Fetch the current PostEx status of one Sales Order (manual button).

	One `track-order` call for this order's own tracking number: no feed pull and
	no document created. Refused for orders that are not PostEx orders (Quick
	Delivery / Fast And Quick) and for orders without a tracking number.
	"""
	if not sales_order:
		frappe.throw(_("A Sales Order is required."))
	if not frappe.has_permission("Sales Order", "write", doc=sales_order):
		frappe.throw(_("Not permitted to change this Sales Order."), frappe.PermissionError)

	so = frappe.get_doc("Sales Order", sales_order)
	if so.docstatus == 2:
		frappe.throw(_("This Sales Order is cancelled; its courier status is not synced."))
	if cint(so.get("custom_postex_booking_cancelled")):
		frappe.throw(
			_(
				"This booking was cancelled. Use 'Reopen PostEx Booking' first if it "
				"should be synced again."
			)
		)

	courier = (so.get("custom_courier") or "").strip()
	if courier and courier != POSTEX_COURIER:
		frappe.throw(
			_("This order is booked with courier {0}, not PostEx.").format(courier)
		)

	tracking = (so.get("custom_postex_tracking_number") or "").strip()
	if not tracking:
		frappe.throw(
			_(
				"This order has no tracking number yet. Book it at PostEx first, "
				"then fetch its status."
			)
		)

	from frappe.utils import now_datetime

	from .client import PostExClient
	from .config import account_for_document
	from .exceptions import PostExAPIError
	from .sync import _write_payload
	from .utils import clear_failures

	account_doc = account_for_document(so)
	try:
		payload = PostExClient(account_doc).track_order(tracking.replace(" ", ""))
	except PostExAPIError as exc:
		return {"ok": 0, "error": str(exc)[:300]}
	if not payload:
		return {
			"ok": 0,
			"error": _("PostEx returned no data for tracking {0}.").format(tracking),
		}

	previous = (so.get("custom_postex_status") or "").strip()
	if not so.get("custom_postex_account"):
		frappe.db.set_value("Sales Order", so.name, "custom_postex_account", account_doc.name)

	result = {
		"tracking_filled": 0,
		"status_set": 0,
		"courier_filled": 0,
		"unchanged": 0,
	}
	new_tracking = _write_payload(account_doc, frappe._dict(so.as_dict()), payload, tracking, result)
	if new_tracking is None:
		return {
			"ok": 0,
			"error": _(
				"PostEx tracking {0} already belongs to another Sales Order."
			).format(tracking),
		}
	frappe.db.set_value(
		"Sales Order",
		so.name,
		{"custom_postex_last_attempt_on": now_datetime()},
		update_modified=False,
	)
	# A successful manual fetch proves the order works: un-quarantine it.
	clear_failures(so.name)
	frappe.db.commit()

	values = frappe.db.get_value(
		"Sales Order",
		so.name,
		[
			"custom_postex_status",
			"custom_postex_last_synced_on",
			"custom_postex_delivery_fee",
			"custom_postex_delivery_tax",
			"custom_postex_sales_invoice",
			"custom_postex_return_entry",
		],
		as_dict=True,
	) or {}
	status = (values.get("custom_postex_status") or "").strip()
	return {
		"ok": 1,
		"account": account_doc.name,
		"tracking": new_tracking,
		"status": status,
		"previous_status": previous,
		"postex_status": (payload.get("transactionStatus") or "").strip(),
		"changed": bool(result["status_set"]),
		"delivery_fee": flt(values.get("custom_postex_delivery_fee"))
		+ flt(values.get("custom_postex_delivery_tax")),
		"last_synced_on": str(values.get("custom_postex_last_synced_on") or ""),
		"next_step": _next_step(status, values),
	}


def _next_step(status, values) -> str:
	"""What happens next, so a manual fetch is not a dead end."""
	if status == "Delivered" and not values.get("custom_postex_sales_invoice"):
		return _(
			"Delivered: the delivery note and invoice are created by the next sync "
			"run (within 15 minutes)."
		)
	if status == "Returned" and not values.get("custom_postex_return_entry"):
		return _(
			"Returned: the stock return and courier cost are booked by the next sync "
			"run (within 15 minutes)."
		)
	if status in ("Delivered", "Returned"):
		return _("Final status, nothing left to book.")
	return ""


@frappe.whitelist()
def requeue_order(sales_order):
	"""Clear the quarantine flag so a fixed order re-enters the automatic sweep."""
	if not sales_order:
		frappe.throw(_("A Sales Order is required."))
	if not frappe.has_permission("Sales Order", "write", doc=sales_order):
		frappe.throw(_("Not permitted to change this Sales Order."), frappe.PermissionError)
	if not frappe.db.exists("Sales Order", sales_order):
		frappe.throw(_("Sales Order {0} does not exist.").format(sales_order))

	frappe.db.set_value(
		"Sales Order",
		sales_order,
		{
			"custom_postex_sync_failures": 0,
			"custom_postex_needs_attention": 0,
			# NULL puts it at the head of the cursor, so the next run retries it.
			"custom_postex_last_attempt_on": None,
		},
		update_modified=False,
	)
	frappe.db.commit()
	return {"ok": 1, "sales_order": sales_order}


@frappe.whitelist()
def late_return_sweep(account=None, company=None):
	"""Run the daily Delivered/Returned sweep now (manual catch-up)."""
	frappe.only_for(WRITE_ROLES)
	accounts = _resolve_accounts(account, company)
	if not accounts:
		frappe.throw(_("No enabled PostEx Account found for the given company/account."))
	results = {}
	for row in accounts:
		results[row] = run_locked("late-returns", row, sync.sync_late_returns)
	return results


@frappe.whitelist()
def backfill_accounts(account=None, company=None, dry_run=1):
	"""Assign `custom_postex_account` to PostEx orders that have none (P4)."""
	frappe.only_for(("System Manager",))
	from .backfill import assign_accounts

	return assign_accounts(account=account, company=company, dry_run=bool(frappe.utils.cint(dry_run)))


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
			"persistent_sync_cursor": True,
			"state_based_scope": True,
			"late_return_sweep": True,
			"paced_requests": True,
			"failure_rows_and_quarantine": True,
			"duplicate_document_guards": True,
			"webhook_watchdog": True,
			"manual_status_fetch": True,
			"manual_booking_cancel": True,
			"quarantine_excluded_in_sql": True,
			"helper_backlog_metric": True,
			"cas_link_claims": True,
			"background_sync_locked": True,
			"booking_post_not_retried": True,
			"booking_doubt_state": True,
			"terminal_outcomes": True,
			"tax_and_discount_rows_copied": True,
			"delivery_date_posting": True,
			"tolerant_reference_matching": True,
			"ambiguous_reference_guard": True,
			"manual_booking_link": True,
			"auto_cancel_on_order_cancel": True,
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
