"""Status / tracking / fee sync for one PostEx account at a time."""

import frappe
from frappe.utils import add_days, flt, now_datetime, nowdate

from .client import PostExClient
from .config import AccountConfig, get_account
from .constants import (
	MAX_ORDERS_PER_SYNC,
	MAX_TRACK_ORDER_CALLS,
	POSTEX_COURIER,
)
from .exceptions import PostExAPIError
from .utils import field_exists, log_error, map_status, norm

SYNC_RUN = "PostEx Sync Run"


def sync_all(company=None) -> dict:
	"""Sync every enabled account, sequentially (used by the 15-minute cron)."""
	accounts = frappe.get_all(
		"PostEx Account",
		filters={"enabled": 1, "enable_sync": 1, **({"company": company} if company else {})},
		fields=["name", "company"],
		order_by="company asc, is_default desc",
	)
	summary = {"accounts": len(accounts), "results": {}}
	for row in accounts:
		try:
			summary["results"][row.name] = sync_account(row.name)
		except Exception:
			log_error(
				"PostEx sync failed",
				f"{row.name}\n{frappe.get_traceback()}",
			)
			summary["results"][row.name] = {"error": "exception"}
	return summary


def sync_account(account: str, limit: int = MAX_ORDERS_PER_SYNC, run_helpers: bool = True) -> dict:
	"""Pull PostEx orders for one account and update its Sales Orders.

	Only orders of the account's company are touched, and within those only
	orders already assigned to this account or still unassigned.
	"""
	account_doc = get_account(account)
	if not account_doc.enabled or not account_doc.enable_sync:
		return {"skipped": "disabled"}
	token = account_doc.get_password("api_token", raise_exception=False)
	if not token:
		return {"skipped": "no token"}

	run = frappe.get_doc(
		{
			"doctype": SYNC_RUN,
			"postex_account": account_doc.name,
			"company": account_doc.company,
			"started_on": now_datetime(),
			"status": "Running",
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	result = {
		"account": account_doc.name,
		"company": account_doc.company,
		"orders_seen": 0,
		"refs_found": 0,
		"tracking_filled": 0,
		"status_set": 0,
		"unchanged": 0,
		"no_data": 0,
		"unassigned": 0,
		"booked": 0,
		"errors": [],
	}
	try:
		client = PostExClient(account_doc)
		ref_map = _pull_ref_map(account_doc, client, result)
		_sync_orders(account_doc, client, ref_map, result, limit)
		result["run_helpers"] = _run_helpers(account_doc) if run_helpers else {}
		result["status"] = "Failed" if result["errors"] and not result["status_set"] else (
			"Partial" if result["errors"] else "Success"
		)
	except Exception as exc:
		result["errors"].append(str(exc)[:250])
		result["status"] = "Failed"
		log_error(
			"PostEx sync account failed",
			f"{account}\n{frappe.get_traceback()}",
		)
	finally:
		_finish_run(run, result)

	return result


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------
def _pull_ref_map(account, client, result) -> dict:
	window = int(account.get("sync_window_days") or 60)
	today = nowdate()
	start = add_days(today, -window)

	ref_map = {}
	for item in client.list_orders(start, today):
		ref_map[norm(item.get("orderRefNumber"))] = item

	# Unbooked list: fill gaps only, never overwrite richer data.
	for item in client.list_unbooked_orders(start, today):
		ref = norm(item.get("orderRefNumber"))
		current = ref_map.get(ref)
		if not current:
			ref_map[ref] = item
			continue
		for key in ("trackingNumber", "transactionStatus"):
			if not (current.get(key) or "").strip() and (item.get(key) or "").strip():
				current[key] = item[key]

	result["refs_found"] = len(ref_map)
	return ref_map


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
def _candidate_filters(account) -> dict:
	"""Orders this account is allowed to touch (company scope only)."""
	return {"docstatus": 1, "company": account.company}


def _candidate_or_filters(account) -> list:
	"""Explicitly-assigned orders OR unassigned ones.

	`("is", "not set")` expands to `ifnull(field,'') = ''`, which is the only
	reliable way to catch both NULL and '' — a Link field with no value is
	stored as NULL, and `IN ('', NULL)` never matches NULL in MariaDB.
	"""
	return [
		{"custom_postex_account": account.name},
		{"custom_postex_account": ("is", "not set")},
	]


def _sync_orders(account, client, ref_map, result, limit):
	fields = [
		"name",
		"company",
		"shopify_order_number",
		"po_no",
		"customer",
		"custom_postex_account",
		"custom_postex_tracking_number",
		"custom_postex_status",
		"custom_courier",
		"custom_postex_delivery_fee",
		"custom_postex_delivery_tax",
		"custom_postex_reversal_fee",
		"custom_postex_reversal_tax",
	]
	if field_exists("Sales Order", "custom_shopify_state"):
		fields.append("custom_shopify_state")

	orders = frappe.get_all(
		"Sales Order",
		filters=_candidate_filters(account),
		or_filters=_candidate_or_filters(account),
		fields=fields,
		limit=limit,
		order_by="modified desc",
	)
	result["orders_seen"] = len(orders)

	looked_up = {}
	extra_calls = 0
	extra_fail = 0

	for order in orders:
		reference = norm(order.shopify_order_number) or norm(order.get("po_no"))
		tracking = (order.custom_postex_tracking_number or "").strip()
		payload = ref_map.get(reference) if reference else None

		if payload is None and tracking and (order.get("custom_courier") or "") in (
			"",
			POSTEX_COURIER,
		):
			key = tracking.replace(" ", "")
			if key in looked_up:
				payload = looked_up[key]
			elif extra_calls < MAX_TRACK_ORDER_CALLS:
				extra_calls += 1
				try:
					payload = client.track_order(key) or None
				except PostExAPIError as exc:
					extra_fail += 1
					result["errors"].append(f"track-order {key}: {str(exc)[:80]}")
					payload = None
				looked_up[key] = payload

		if payload is None:
			result["no_data"] += 1
			if not order.custom_postex_account:
				result["unassigned"] += 1
			_maybe_auto_book(account, order, tracking, result)
			continue

		# -- ownership -----------------------------------------------------
		if not order.custom_postex_account and not _claim(order.name, account.name):
			# Another account of the same company got there first.
			continue

		tracking = _write_payload(account, order, payload, tracking, result)
		frappe.db.set_value(
			"Sales Order",
			order.name,
			"custom_postex_last_synced_on",
			now_datetime(),
			update_modified=False,
		)

	result["track_order_calls"] = extra_calls
	result["track_order_failures"] = extra_fail


def _write_payload(account, order, payload, tracking, result):
	new_tracking = (payload.get("trackingNumber") or "").strip()
	if new_tracking and new_tracking != tracking:
		clash = frappe.db.get_value(
			"Sales Order",
			{
				"custom_postex_account": account.name,
				"custom_postex_tracking_number": new_tracking,
				"name": ("!=", order.name),
			},
			"name",
		)
		if not clash:
			frappe.db.set_value(
				"Sales Order", order.name, "custom_postex_tracking_number", new_tracking
			)
			tracking = new_tracking
			result["tracking_filled"] += 1

	if (order.get("custom_courier") or "") != POSTEX_COURIER:
		frappe.db.set_value("Sales Order", order.name, "custom_courier", POSTEX_COURIER)

	mapped = map_status(payload.get("transactionStatus"), payload.get("transactionStatusHistory")) or "Unknown"
	if mapped != (order.custom_postex_status or ""):
		frappe.db.set_value("Sales Order", order.name, "custom_postex_status", mapped)
		result["status_set"] += 1
	else:
		result["unchanged"] += 1

	# Courier cost reported by PostEx (varies by destination).
	fee = flt(payload.get("transactionFee"))
	fee_tax = flt(payload.get("transactionTax"))
	reversal_fee = flt(payload.get("reversalFee"))
	reversal_tax = flt(payload.get("reversalTax"))
	if (
		fee != flt(order.custom_postex_delivery_fee)
		or fee_tax != flt(order.custom_postex_delivery_tax)
		or reversal_fee != flt(order.custom_postex_reversal_fee)
		or reversal_tax != flt(order.custom_postex_reversal_tax)
	):
		frappe.db.set_value(
			"Sales Order",
			order.name,
			{
				"custom_postex_delivery_fee": fee,
				"custom_postex_delivery_tax": fee_tax,
				"custom_postex_reversal_fee": reversal_fee,
				"custom_postex_reversal_tax": reversal_tax,
			},
		)
	return tracking


def _claim(sales_order, account) -> bool:
	"""Atomically assign an unassigned order to this account."""
	frappe.db.sql(
		"""
		update `tabSales Order`
		set custom_postex_account = %s
		where name = %s
		  and (custom_postex_account is null or custom_postex_account = '')
		""",
		(account, sales_order),
	)
	frappe.db.commit()
	return (frappe.db.get_value("Sales Order", sales_order, "custom_postex_account") or "") == account


def _maybe_auto_book(account, order, tracking, result):
	from frappe.utils import cint

	if not cint(account.get("enable_auto_booking")) or tracking:
		return
	if (order.get("custom_shopify_state") or ""):
		return
	if (order.get("custom_courier") or "") not in ("", POSTEX_COURIER):
		return
	if not (account.get("pickup_address") or "").strip():
		return
	try:
		from .booking import book_order

		booked = book_order(order.name, account=account.name, source="sync")
		if booked.get("ok"):
			result["booked"] += 1
			result["tracking_filled"] += 1
		elif booked.get("error"):
			result["errors"].append(f"book {order.name}: {booked['error'][:100]}")
	except Exception as exc:
		result["errors"].append(f"book {order.name}: {str(exc)[:100]}")


# ---------------------------------------------------------------------------
# Downstream flows for this account
# ---------------------------------------------------------------------------
def _run_helpers(account) -> dict:
	"""Pickup / delivery+return, scoped to this account."""
	from .stock import delivery_and_return, pickup_transfer

	out = {}
	try:
		config = AccountConfig(account)
	except Exception as exc:
		return {"error": f"mapping invalid: {exc}"}
	if config.dry_run:
		return {"skipped": "dry run: no documents created"}

	try:
		out["pickup"] = pickup_transfer(config)
	except Exception:
		log_error("PostEx pickup helper failed", frappe.get_traceback())
		out["pickup_error"] = True

	try:
		out["delivery"] = delivery_and_return(config)
	except Exception:
		log_error("PostEx delivery helper failed", frappe.get_traceback())
		out["delivery_error"] = True

	return out


# ---------------------------------------------------------------------------
# Run record
# ---------------------------------------------------------------------------
def _finish_run(run, result):
	notes = []
	for key in ("tracking_filled", "status_set", "unchanged", "no_data", "unassigned", "booked"):
		notes.append(f"{key.replace('_', ' ')} {result.get(key, 0)}")
	summary = (
		f"{result.get('orders_seen', 0)} orders / {result.get('refs_found', 0)} postex refs; "
		+ ", ".join(notes)
	)
	helpers = result.get("run_helpers") or {}
	for key, label in (("pickup", "pickup"), ("delivery", "delivery")):
		block = helpers.get(key)
		if isinstance(block, dict):
			counts = ", ".join(
				f"{k} {len(v)}" for k, v in block.items() if isinstance(v, list) and v
			)
			summary += f" | {label}" + (f": {counts}" if counts else "")
	if result.get("errors"):
		summary += " | errors: " + "; ".join(result["errors"][:5])

	frappe.db.set_value(
		"PostEx Account",
		run.postex_account,
		{"last_sync_on": now_datetime(), "last_run_result": summary[:900]},
		update_modified=False,
	)
	frappe.db.set_value(
		SYNC_RUN,
		run.name,
		{
			"finished_on": now_datetime(),
			"status": result.get("status") or "Failed",
			"orders_seen": result.get("orders_seen", 0),
			"refs_found": result.get("refs_found", 0),
			"tracking_filled": result.get("tracking_filled", 0),
			"status_changed": result.get("status_set", 0),
			"booked": result.get("booked", 0),
			"errors": len(result.get("errors") or []),
			"summary": summary[:900],
		},
		update_modified=False,
	)
	frappe.db.commit()
	result["summary"] = summary[:900]
