"""Status / tracking / fee sync for one PostEx account at a time.

Two design rules drive this module:

* **The cursor, not the page, is the correctness mechanism.** Orders are
  selected oldest-attempt-first (`custom_postex_last_attempt_on`, NULL first)
  and that timestamp is stamped on *every* attempt, success or failure. A run
  that dies mid-page resumes where it stopped, and no order can be starved by a
  flood of newer ones.
* **Scope is decided by order state, never by a row count** (`scope.py`). Still
  moving -> every run; Delivered/Returned -> once a day for 45 days; final ->
  never; other couriers and historical imports -> never.

Every HTTP call is paced and budgeted (`client.py`); exhausting the budget
stops the run cleanly and leaves the remaining orders at the head of the cursor.
"""

import frappe
from frappe.utils import cint, flt, now_datetime, nowdate, add_days

from .client import PostExClient
from .config import AccountConfig, get_account
from .constants import (
	ACTIVE_STATUSES,
	CLOSABLE_STATUSES,
	DEFAULT_SYNC_PAGE_SIZE,
	MAX_SCAN_CEILING,
	MAX_SYNC_FAILURES,
	POSTEX_COURIER,
	SCAN_ACTIVE,
	SCAN_LATE_RETURNS,
)
from .exceptions import PostExAPIError, PostExBudgetExceeded
from .scope import is_our_courier, window_start
from .utils import delivery_date, field_exists, log_error, map_status, norm

SYNC_RUN = "PostEx Sync Run"

# A run record must stay readable: 200 rows is more than enough to answer
# "which order, which step, what error" without turning the doctype into a log.
MAX_RUN_ROWS = 200

# Commit the cursor every N orders so a killed worker loses at most N stamps.
CURSOR_COMMIT_EVERY = 25

REQUIRED_FIELDS = (
	"custom_postex_last_attempt_on",
	"custom_postex_sync_failures",
	"custom_postex_needs_attention",
	"custom_postex_status_changed_on",
	"custom_postex_booking_cancelled",
	"custom_postex_booking_doubt",
	"custom_postex_closed_on",
	"custom_postex_delivered_on",
)

ORDER_FIELDS = """
	name, company, customer, status, shopify_order_number, po_no,
	custom_postex_account, custom_postex_tracking_number, custom_postex_status,
	custom_postex_status_changed_on, custom_courier,
	custom_postex_delivery_fee, custom_postex_delivery_tax,
	custom_postex_reversal_fee, custom_postex_reversal_tax,
	ifnull(custom_postex_sync_failures, 0) as custom_postex_sync_failures,
	ifnull(custom_postex_booking_doubt, 0) as custom_postex_booking_doubt,
	custom_postex_closed_on, custom_postex_delivered_on,
	ifnull(custom_postex_booking_cancelled, 0) as custom_postex_booking_cancelled
"""


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
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


def sync_account_locked(account: str) -> dict:
	"""Cron entry point: one account, under that account's sync lock.

	The lock matters because a manual "Sync now" can land on top of a cron run;
	without it two workers would read the same page and race the same documents.
	"""
	from .tasks import run_locked

	return run_locked("sync", account, sync_account)


def sync_late_returns_locked(account: str) -> dict:
	from .tasks import run_locked

	return run_locked("late-returns", account, sync_late_returns)


def sync_account(
	account: str,
	limit: int | None = None,
	run_helpers: bool = True,
	mode: str = SCAN_ACTIVE,
) -> dict:
	"""Pull PostEx orders for one account and update its Sales Orders.

	Only orders of the account's company are touched, and within those only
	orders already assigned to this account or still unassigned.

	`limit` is a one-off override of the account's `sync_page_size` (used by the
	rollout: page size 20 for an hour); the hard ceiling still applies.
	"""
	account_doc = get_account(account)
	if not account_doc.enabled or not account_doc.enable_sync:
		return {"skipped": "disabled"}
	token = account_doc.get_password("api_token", raise_exception=False)
	if not token:
		return {"skipped": "no token"}

	missing = [field for field in REQUIRED_FIELDS if not field_exists("Sales Order", field)]
	if missing:
		return {"skipped": f"custom fields missing ({', '.join(missing)}): run bench migrate"}

	page_size = min(cint(account_doc.get("sync_page_size")) or DEFAULT_SYNC_PAGE_SIZE, MAX_SCAN_CEILING)
	limit = min(cint(limit) or page_size, MAX_SCAN_CEILING)

	run = frappe.get_doc(
		{
			"doctype": SYNC_RUN,
			"postex_account": account_doc.name,
			"company": account_doc.company,
			"mode": SCAN_LATE_RETURNS if mode == SCAN_LATE_RETURNS else SCAN_ACTIVE,
			"started_on": now_datetime(),
			"status": "Running",
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	result = _blank_result(account_doc, limit, mode)
	client = None
	try:
		client = PostExClient(account_doc)
		ref_map = _pull_ref_map(account_doc, client, result)
		_sync_orders(account_doc, client, ref_map, result, limit, mode)
		if mode == SCAN_ACTIVE and run_helpers:
			result["run_helpers"] = _run_helpers(account_doc)
		result["status"] = _run_status(result)
	except PostExBudgetExceeded as exc:
		result["budget_exhausted"] = True
		result["errors"].append(str(exc)[:200])
		result["status"] = "Partial"
	except Exception as exc:
		result["errors"].append(str(exc)[:250])
		result["status"] = "Failed"
		log_error(
			"PostEx sync account failed",
			f"{account}\n{frappe.get_traceback()}",
		)
	finally:
		if client is not None:
			result["requests"] = client.requests_made
		_finish_run(run, result)

	return result


def sync_late_returns(account: str, limit: int | None = None) -> dict:
	"""Daily slow sweep: Delivered/Returned orders inside the 45-day window.

	Late returns are real: a parcel marked Delivered can still be refused and
	come back weeks later. Everything older than the window is final and is
	never polled again. The ref-map covers the 60-day feed window, so this sweep
	is almost free (two list calls, no per-order track calls).
	"""
	return sync_account(
		account,
		limit=limit or MAX_SCAN_CEILING,
		run_helpers=False,
		mode=SCAN_LATE_RETURNS,
	)


# ---------------------------------------------------------------------------
# Selection (the cursor)
# ---------------------------------------------------------------------------
def _blank_result(account_doc, limit, mode) -> dict:
	return {
		"account": account_doc.name,
		"company": account_doc.company,
		"mode": mode,
		"limit": limit,
		"orders_seen": 0,
		"refs_found": 0,
		"tracking_filled": 0,
		"status_set": 0,
		"courier_filled": 0,
		"unchanged": 0,
		"no_data": 0,
		"unassigned": 0,
		"booked": 0,
		"booking_recovered": 0,
		"reopened": 0,
		"delivered_date_filled": 0,
		"requests": 0,
		"budget_exhausted": False,
		"quarantined": [],
		"errors": [],
		"failures": [],
		"unmatched": [],
		"ambiguous": [],
	}


def _run_status(result) -> str:
	helpers = result.get("run_helpers") or {}
	helper_failures = sum(
		len(block.get("failed") or []) for block in helpers.values() if isinstance(block, dict)
	)
	failure_rows = len(result.get("failures") or [])
	if result.get("errors") and not (result.get("status_set") or result.get("orders_seen")):
		return "Failed"
	if result.get("errors") or result.get("budget_exhausted") or helper_failures or failure_rows:
		return "Partial"
	return "Success"


def _eligibility(account, mode: str) -> tuple[str, dict]:
	"""The WHERE clause of the scan scope, plus its parameters."""
	params = {
		"company": account.company,
		"account": account.name,
		"active": tuple(ACTIVE_STATUSES),
		"cutoff": window_start(),
	}
	clauses = [
		"docstatus = 1",
		"company = %(company)s",
		# an unassigned order may still be claimed by this account
		"ifnull(custom_postex_account, '') in ('', %(account)s)",
		# other couriers are never PostEx's business
		"ifnull(custom_courier, '') in ('', 'PostEx')",
		# quarantined orders wait for a human
		"ifnull(custom_postex_needs_attention, 0) = 0",
		# a booking cancelled by the merchant is never resurrected by the poll
		"ifnull(custom_postex_booking_cancelled, 0) = 0",
	]
	if field_exists("Sales Order", "custom_shopify_state"):
		# historical imports are out of scope entirely
		clauses.append("ifnull(custom_shopify_state, '') = ''")

	if mode == SCAN_LATE_RETURNS:
		clauses.append("custom_postex_status in ('Delivered', 'Returned')")
		# `modified` is the fallback for rows stamped before the field existed
		clauses.append("ifnull(custom_postex_status_changed_on, modified) >= %(cutoff)s")
	else:
		clauses.append(
			"""(
				ifnull(custom_postex_status, '') = ''
				or custom_postex_status = 'Unknown'
				or custom_postex_status in %(active)s
				or (
					custom_postex_status in ('Cancelled', 'Expired')
					and ifnull(`status`, '') not in ('Closed', 'Completed', 'Cancelled')
				)
			)"""
		)
	return " and ".join(clauses), params


def _select_orders(account, limit: int, mode: str = SCAN_ACTIVE) -> list:
	"""Oldest attempt first; NULL (never attempted) first of all.

	MariaDB sorts NULL before any value on ASC, but `ifnull` makes the intent
	explicit and keeps the cursor stable across database versions.
	"""
	where, params = _eligibility(account, mode)
	params["limit"] = int(limit)
	sql = f"""
		select {ORDER_FIELDS}
		from `tabSales Order`
		where {where}
		order by ifnull(custom_postex_last_attempt_on, '1900-01-01 00:00:00') asc, name asc
		limit %(limit)s
	"""
	return [frappe._dict(row) for row in frappe.db.sql(sql, params, as_dict=True)]


def count_eligible(account, mode: str = SCAN_ACTIVE) -> int:
	"""How many orders are in scope right now (used by the daily digest)."""
	where, params = _eligibility(account, mode)
	rows = frappe.db.sql(f"select count(*) from `tabSales Order` where {where}", params)
	return int(rows[0][0]) if rows else 0


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------
def _pull_ref_map(account, client, result) -> dict:
	"""Reference -> list of PostEx orders with that reference.

	A reference is not unique at PostEx (re-booking, `-COPY` orders, the same
	order appearing in both feeds), so the map keeps every candidate and the
	per-order step decides — never "last one wins".
	"""
	window = int(account.get("sync_window_days") or 60)
	today = nowdate()
	start = add_days(today, -window)

	ref_map = {}
	for item in client.list_orders(start, today):
		ref = norm(item.get("orderRefNumber"))
		if not ref:
			continue
		ref_map.setdefault(ref, []).append(item)

	# Unbooked list: fill gaps only, never overwrite richer data. A broken
	# v2 endpoint must not stop the main feed from being applied.
	try:
		unbooked = client.list_unbooked_orders(start, today)
	except PostExBudgetExceeded:
		raise
	except PostExAPIError as exc:
		result["errors"].append(f"get-unbooked-orders: {str(exc)[:120]}")
		unbooked = []

	for item in unbooked:
		ref = norm(item.get("orderRefNumber"))
		bucket = ref_map.setdefault(ref, [])
		if not bucket:
			bucket.append(item)
			continue
		current = bucket[0]
		for key in ("trackingNumber", "transactionStatus"):
			if not (current.get(key) or "").strip() and (item.get(key) or "").strip():
				current[key] = item[key]

	result["refs_found"] = len(ref_map)
	result["duplicate_refs"] = sorted(ref for ref, items in ref_map.items() if len(items) > 1)
	return ref_map


def _pick_payload(payloads, tracking):
	"""The PostEx order that belongs to this Sales Order, or None.

	One candidate needs no thought. With several, only the order's own tracking
	number can tell them apart — without it the reference is ambiguous and
	guessing is how a wrong status/parcel gets attached.
	"""
	if not payloads:
		return None
	if len(payloads) == 1:
		return payloads[0]
	key = (tracking or "").replace(" ", "")
	if not key:
		return None
	for item in payloads:
		if (item.get("trackingNumber") or "").strip().replace(" ", "") == key:
			return item
	return None


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
def _sync_orders(account, client, ref_map, result, limit, mode=SCAN_ACTIVE):
	orders = _select_orders(account, limit, mode)
	result["orders_seen"] = len(orders)

	looked_up = {}
	unmatched = []
	processed = 0

	for order in orders:
		if client.budget_exhausted:
			result["budget_exhausted"] = True
			break
		try:
			outcome = _sync_one(account, client, ref_map, result, order, looked_up, unmatched)
		except PostExBudgetExceeded:
			# The order was never really tried: advance the cursor (a dead
			# request must not pin the queue) but do not count a failure.
			result["budget_exhausted"] = True
			_touch(account, order, result, failure=False)
			break
		except Exception as exc:
			_record_failure(
				result,
				order.get("name"),
				"sync",
				str(exc)[:250],
				order.get("custom_postex_tracking_number"),
			)
			_touch(account, order, result, failure=True)
		else:
			_touch(account, order, result, failure=(outcome == "failed"), reset=(outcome == "ok"))

		processed += 1
		if processed % CURSOR_COMMIT_EVERY == 0:
			frappe.db.commit()

	result["unmatched"] = unmatched


def _sync_one(account, client, ref_map, result, order, looked_up, unmatched) -> str:
	"""Sync one order. Returns 'ok' | 'no_data' | 'failed'."""
	reference = norm(order.get("shopify_order_number")) or norm(order.get("po_no"))
	tracking = (order.get("custom_postex_tracking_number") or "").strip()
	payloads = ref_map.get(reference) or []
	payload = _pick_payload(payloads, tracking)
	track_failed = False

	if payloads and payload is None:
		# Several PostEx orders share this reference and our order has no
		# tracking number to tell them apart: never guess (that is how one
		# parcel's status ended up on another order), and never auto-book,
		# which would create yet another parcel.
		result.setdefault("ambiguous", []).append(order.get("name"))
		_record_failure(
			result,
			order.get("name"),
			"sync",
			f"reference {reference} matches {len(payloads)} PostEx orders; "
			"link the right one with 'Link PostEx Booking'",
			reference,
		)
		return "failed"

	if payload is None and tracking and is_our_courier(order.get("custom_courier")):
		key = tracking.replace(" ", "")
		if key not in looked_up:
			try:
				looked_up[key] = client.track_order(key) or None
			except PostExBudgetExceeded:
				raise
			except PostExAPIError as exc:
				looked_up[key] = None
				track_failed = True
				_record_failure(
					result,
					order.get("name"),
					"sync",
					f"track-order {key}: {str(exc)[:180]}",
					key,
				)
		payload = looked_up[key]

	if payload is None:
		result["no_data"] += 1
		if not order.get("custom_postex_account"):
			result["unassigned"] += 1
		unmatched.append(order.get("name"))
		_maybe_auto_book(account, order, tracking, result)
		return "failed" if track_failed else "no_data"

	# -- ownership -----------------------------------------------------
	if not order.get("custom_postex_account") and not _claim(order.get("name"), account.name):
		# Another account of the same company got there first.
		return "no_data"

	written = _write_payload(account, order, payload, tracking, result)
	return "failed" if (track_failed or written is None) else "ok"


def _write_payload(account, order, payload, tracking, result):
	"""Apply one PostEx payload to one Sales Order.

	Returns the stored tracking number, or **None** when the payload belongs to
	another Sales Order (same tracking already linked elsewhere). In that case
	nothing is written at all: copying the status would make this order look
	delivered while it is really a duplicate reference.
	"""
	values = {}

	# A booking cancelled from the desk while this run was in flight must never be
	# revived by the payload the run had already fetched.
	if cint(order.get("custom_postex_booking_cancelled")):
		return tracking

	new_tracking = (payload.get("trackingNumber") or "").strip()
	if new_tracking and new_tracking != tracking:
		clash = frappe.db.get_value(
			"Sales Order",
			{
				"custom_postex_account": account.name,
				"custom_postex_tracking_number": new_tracking,
				"name": ("!=", order.get("name")),
			},
			"name",
		)
		if clash:
			_record_failure(
				result,
				order.get("name"),
				"sync",
				f"tracking {new_tracking} already belongs to {clash}",
				new_tracking,
			)
			return None
		values["custom_postex_tracking_number"] = new_tracking
		tracking = new_tracking
		result["tracking_filled"] += 1
		if cint(order.get("custom_postex_booking_doubt")):
			# The booking existed after all: the poll just resolved the doubt.
			values["custom_postex_booking_doubt"] = 0
			result["booking_recovered"] = result.get("booking_recovered", 0) + 1

	if (order.get("custom_courier") or "") != POSTEX_COURIER:
		values["custom_courier"] = POSTEX_COURIER
		result["courier_filled"] += 1

	mapped = (
		map_status(payload.get("transactionStatus"), payload.get("transactionStatusHistory"))
		or "Unknown"
	)

	# The delivery date is what the invoice is posted on, so it must be captured
	# the moment the parcel is reported delivered (once — a later poll or a
	# return must not move it).
	if mapped == "Delivered" and not order.get("custom_postex_delivered_on"):
		delivered_on = delivery_date(payload)
		if delivered_on:
			values["custom_postex_delivered_on"] = delivered_on
			result["delivered_date_filled"] = result.get("delivered_date_filled", 0) + 1

	if mapped != (order.get("custom_postex_status") or ""):
		values["custom_postex_status"] = mapped
		# Drives the 45-day late-return sweep; never rely on `modified`.
		values["custom_postex_status_changed_on"] = now_datetime()
		result["status_set"] += 1
		# The terminal handler closes the order; if the parcel comes back to life
		# (a cancelled booking is re-booked, a lost parcel turns up) the order must
		# be usable again or the next document would be refused on a Closed order.
		if mapped and mapped not in CLOSABLE_STATUSES and order.get("custom_postex_closed_on"):
			values["custom_postex_closed_on"] = None
			values["status"] = "To Deliver and Bill"
			result["reopened"] = result.get("reopened", 0) + 1
	else:
		result["unchanged"] += 1

	# Courier cost reported by PostEx (varies by destination).
	fee = flt(payload.get("transactionFee"))
	fee_tax = flt(payload.get("transactionTax"))
	reversal_fee = flt(payload.get("reversalFee"))
	reversal_tax = flt(payload.get("reversalTax"))
	if (
		fee != flt(order.get("custom_postex_delivery_fee"))
		or fee_tax != flt(order.get("custom_postex_delivery_tax"))
		or reversal_fee != flt(order.get("custom_postex_reversal_fee"))
		or reversal_tax != flt(order.get("custom_postex_reversal_tax"))
	):
		values["custom_postex_delivery_fee"] = fee
		values["custom_postex_delivery_tax"] = fee_tax
		values["custom_postex_reversal_fee"] = reversal_fee
		values["custom_postex_reversal_tax"] = reversal_tax

	values["custom_postex_last_synced_on"] = now_datetime()
	values["custom_postex_sync_failures"] = 0
	frappe.db.set_value("Sales Order", order.get("name"), values, update_modified=False)
	return tracking


def _touch(account, order, result, failure=False, reset=False):
	"""Stamp the cursor. Called for every attempt, whatever the outcome.

	`failure` increments the consecutive-failure counter and quarantines the
	order after MAX_SYNC_FAILURES; `reset` clears it on a successful match;
	neither still advances the cursor, which is what guarantees progress.
	"""
	values = {"custom_postex_last_attempt_on": now_datetime()}
	failures = cint(order.get("custom_postex_sync_failures"))

	if failure:
		failures += 1
		values["custom_postex_sync_failures"] = failures
	elif reset and failures:
		failures = 0
		values["custom_postex_sync_failures"] = 0

	if failures >= MAX_SYNC_FAILURES:
		values["custom_postex_needs_attention"] = 1
		result.setdefault("quarantined", []).append(order.get("name"))

	frappe.db.set_value("Sales Order", order.get("name"), values, update_modified=False)


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
	"""Book an order that PostEx does not know yet (only when switched on).

	Historical imports and other couriers are already excluded by the scan
	scope, so they can never reach this point. An unconfirmed previous attempt
	is **never** retried here: PostEx may already hold the parcel, and only the
	manual "Clear booking doubt" (after a human checked) resets it.
	"""
	if not cint(account.get("enable_auto_booking")) or tracking:
		return
	if cint(order.get("custom_postex_booking_doubt")):
		return
	if not is_our_courier(order.get("custom_courier")):
		return
	if not (account.get("pickup_address") or "").strip():
		return
	try:
		from .booking import book_order

		booked = book_order(order.get("name"), account=account.name, source="sync")
		if booked.get("ok"):
			result["booked"] += 1
			result["tracking_filled"] += 1
		elif booked.get("error"):
			result["errors"].append(f"book {order.get('name')}: {booked['error'][:100]}")
	except Exception as exc:
		result["errors"].append(f"book {order.get('name')}: {str(exc)[:100]}")


# ---------------------------------------------------------------------------
# Downstream flows for this account
# ---------------------------------------------------------------------------
def _run_helpers(account) -> dict:
	"""Pickup / delivery+return, scoped to this account."""
	from .stock import delivery_and_return, helper_backlog, pickup_transfer

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

	# What is left after this run, split into actionable and quarantine-blocked.
	# A green run with a growing blocked count is the silent stall this is for.
	try:
		out["backlog"] = helper_backlog(config)
	except Exception:
		log_error("PostEx helper backlog failed", frappe.get_traceback())
		out["backlog_error"] = True

	return out


# ---------------------------------------------------------------------------
# Run record
# ---------------------------------------------------------------------------
def _backlog_note(backlog) -> str:
	"""One line for the run summary: work left, and what quarantine is holding.

	Formatted as `step ready/blocked`. The alert is deliberately loud: a green
	sync that creates no documents is otherwise indistinguishable from a quiet
	day, which is how a stalled helper stayed invisible before.
	"""
	if not isinstance(backlog, dict) or not backlog:
		return ""
	parts = []
	blocked_total = 0
	for step in ("pickup", "delivery", "invoice", "return", "terminal"):
		counts = backlog.get(step)
		if not isinstance(counts, dict):
			continue
		ready = int(counts.get("ready") or 0)
		blocked = int(counts.get("blocked") or 0)
		blocked_total += blocked
		if ready or blocked:
			parts.append(f"{step} {ready}/{blocked}")

	note = ""
	if parts:
		note += " | backlog ready/blocked: " + ", ".join(parts)
	if blocked_total:
		note += (
			f" | ALERT: {blocked_total} quarantined order(s) are holding back "
			"document creation - re-queue them"
		)
	return note


def _record_failure(result, sales_order, step, error, reference=None):
	"""Add one row to the run's failure table (bounded).

	Per-order problems live only in the Failures table; `result["errors"]` stays
	for run-level problems (API down, booking failed, budget spent) so the
	summary and the `errors` counter do not double-report the same event.
	"""
	rows = result.setdefault("failures", [])
	if len(rows) >= MAX_RUN_ROWS:
		return
	rows.append(
		{
			"sales_order": sales_order,
			"step": step,
			"reference": (reference or "")[:140] or None,
			"error": (error or "")[:500] or None,
		}
	)


def _collect_failures(result) -> list:
	"""Order failures plus every helper failure, bounded by MAX_RUN_ROWS."""
	rows = list(result.get("failures") or [])
	helpers = result.get("run_helpers") or {}
	for key, step in (("pickup", "pickup"), ("delivery", "delivery")):
		block = helpers.get(key)
		if not isinstance(block, dict):
			continue
		for item in block.get("failed") or []:
			if isinstance(item, dict):
				rows.append(
					{
						"sales_order": item.get("sales_order"),
						"step": item.get("step") or step,
						"reference": (item.get("reference") or "")[:140] or None,
						"error": (item.get("error") or "")[:500] or None,
					}
				)
			else:
				rows.append({"sales_order": None, "step": step, "error": str(item)[:500]})
	return rows[:MAX_RUN_ROWS]


def _finish_run(run, result):
	"""Write the run record. Never raises: it runs in a `finally`."""
	try:
		_write_run(run, result)
	except Exception:
		log_error(
			"PostEx: could not write the sync run record",
			f"{getattr(run, 'name', '?')}\n{frappe.get_traceback()}",
		)


def _write_run(run, result):
	notes = []
	for key in (
		"tracking_filled",
		"status_set",
		"reopened",
		"unchanged",
		"no_data",
		"unassigned",
		"booked",
		"booking_recovered",
		"delivered_date_filled",
	):
		notes.append(f"{key.replace('_', ' ')} {result.get(key, 0)}")
	if result.get("courier_filled"):
		notes.append(f"courier filled {result['courier_filled']}")
	summary = (
		f"[{result.get('mode', SCAN_ACTIVE)}] "
		f"{result.get('orders_seen', 0)} orders / {result.get('refs_found', 0)} postex refs; "
		+ ", ".join(notes)
	)
	if result.get("requests"):
		summary += f" | requests {result['requests']}"
	if result.get("budget_exhausted"):
		summary += " | budget exhausted (continues next run)"
	if result.get("quarantined"):
		summary += f" | quarantined {len(result['quarantined'])}"

	helpers = result.get("run_helpers") or {}
	for key, label in (("pickup", "pickup"), ("delivery", "delivery")):
		block = helpers.get(key)
		if isinstance(block, dict):
			counts = ", ".join(
				f"{k} {len(v)}" for k, v in block.items() if isinstance(v, list) and v
			)
			summary += f" | {label}" + (f": {counts}" if counts else "")
	summary += _backlog_note(helpers.get("backlog"))
	if result.get("failures"):
		summary += f" | failures {len(result['failures'])}"
	if result.get("ambiguous"):
		summary += f" | ALERT: {len(result['ambiguous'])} order(s) have an ambiguous reference"
	if result.get("duplicate_refs"):
		summary += (
			f" | {len(result['duplicate_refs'])} reference(s) used by several PostEx orders: "
			+ ", ".join(result["duplicate_refs"][:5])
		)
	if result.get("errors"):
		summary += " | errors: " + "; ".join(str(e) for e in result["errors"][:5])

	# Keep the summary on the returned dict even if the record write fails.
	result["summary"] = summary[:900]

	frappe.db.set_value(
		"PostEx Account",
		run.postex_account,
		{"last_sync_on": now_datetime(), "last_run_result": summary[:900]},
		update_modified=False,
	)

	failures = _collect_failures(result)
	# `result["errors"]` holds run-level problems; `failures` holds per-order ones.
	# Counting both makes the number match what the user can actually see.
	error_count = len(result.get("errors") or []) + len(failures)
	unmatched = result.get("unmatched") or []
	doc = frappe.get_doc(SYNC_RUN, run.name)
	doc.update(
		{
			"finished_on": now_datetime(),
			"status": result.get("status") or "Failed",
			"orders_seen": result.get("orders_seen", 0),
			"refs_found": result.get("refs_found", 0),
			"tracking_filled": result.get("tracking_filled", 0),
			"status_changed": result.get("status_set", 0),
			"booked": result.get("booked", 0),
			"errors": error_count,
			"requests": result.get("requests", 0),
			"budget_exhausted": 1 if result.get("budget_exhausted") else 0,
			"summary": summary[:900],
			"unmatched": ", ".join(unmatched[:MAX_RUN_ROWS])[:900] or None,
		}
	)
	for row in failures:
		doc.append("failures", row)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	result["summary"] = summary[:900]
