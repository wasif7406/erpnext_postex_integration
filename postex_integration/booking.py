"""Booking a Sales Order at PostEx (manual button, auto-booking hook, sweep).

A booking is the only call that creates something irreversible *outside*
ERPNext, so it is guarded three ways:

* one `filelock` per Sales Order, so a double click, the sweep and the
  auto-booking job can never post for the same order at the same time;
* `add-order` is never retried (see `client._send`): a timeout or a 5xx is
  raised as *ambiguous*, the order reference is looked up in the PostEx feed and
  the tracking is adopted if PostEx did create the order after all;
* when that lookup finds nothing the order is parked in
  `custom_postex_booking_doubt` and **no** automatic path tries again — a human
  confirms PostEx has no such order and clears the flag.

The order reference (`shopify_order_number` → `po_no` → name) is the only
idempotency key PostEx gives us: it is what the lookup matches on.
"""

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, now_datetime, nowdate

from .client import PostExClient
from .config import account_for_document, config_for_document
from .constants import POSTEX_COURIER
from .exceptions import PostExAPIError, PostExConfigError
from .utils import dump_snippet, existing_invoice, log_error, map_status, norm

try:
	from frappe.exceptions import LockTimeoutError
except ImportError:  # very old Frappe
	from frappe.utils.synchronization import LockTimeoutError  # type: ignore

BOOKING_LOG = "PostEx Booking Log"

# Feed statuses that must never be adopted as "our" booking: a cancelled or
# expired parcel is a previous attempt, not the one we are looking for.
DEAD_STATUSES = ("Cancelled", "Expired")

# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def book_order(sales_order, account=None, source="manual") -> dict:
	"""Book one Sales Order and write the result back on the order.

	Runs under a per-order lock: two attempts for the same order can never be in
	flight together (double click, sweep vs. button, auto-booking vs. manual).
	"""
	from frappe.utils.synchronization import filelock

	so = frappe.get_doc("Sales Order", sales_order)
	result = {"ok": 0, "so": so.name, "account": None, "source": source}

	try:
		with filelock(f"postex-book-{so.name}", timeout=0):
			return _book_order(so, account=account, source=source, result=result)
	except LockTimeoutError:
		return _fail(
			so,
			result,
			"a booking for this order is already in progress",
			source,
			status="Skipped",
		)


def _book_order(so, account, source, result) -> dict:
	try:
		account_doc = account_for_document(so, account=account)
	except PostExConfigError as exc:
		return _fail(so, result, str(exc), source)

	result["account"] = account_doc.name

	if so.docstatus != 1:
		return _fail(so, result, "sales order is not submitted", source, status="Skipped")
	if (so.get("custom_postex_tracking_number") or "").strip():
		return _fail(so, result, "order already has a courier tracking number", source, status="Skipped")
	if (so.get("custom_courier") or "") not in ("", POSTEX_COURIER):
		return _fail(
			so, result, f"order is assigned to courier '{so.custom_courier}'", source, status="Skipped"
		)
	if cint(so.get("custom_postex_booking_cancelled")):
		return _fail(
			so,
			result,
			"this booking was cancelled - press 'Reopen PostEx Booking' before booking it again",
			source,
			status="Skipped",
		)
	if cint(so.get("custom_postex_booking_doubt")):
		return _fail(
			so,
			result,
			"the last booking attempt was unconfirmed: check PostEx for this reference, then "
			"press 'Clear booking doubt'. Booking again now could create a second parcel.",
			source,
			status="Skipped",
		)

	existing = _successful_booking(so.name, account_doc.name)
	if existing:
		return _fail(so, result, f"already booked ({existing})", source, status="Skipped")

	if not account_doc.enabled:
		return _fail(so, result, f"PostEx Account {account_doc.name} is disabled", source, status="Skipped")
	if account_doc.get("dry_run"):
		return _fail(so, result, "dry run: booking not sent to PostEx", source, status="Skipped")

	try:
		config = config_for_document(so, account=account_doc.name)
		payload, reference = build_payload(so, config)
	except PostExConfigError as exc:
		return _fail(so, result, str(exc), source)
	except Exception as exc:
		# A stale address link or similar used to escape as a bare 500, with
		# nothing in the Booking Log: always answer with a readable error.
		log_error(
			"PostEx booking payload failed",
			f"{so.name}\n{frappe.get_traceback()}",
			"Sales Order",
			so.name,
		)
		return _fail(so, result, f"cannot build the booking payload: {exc}", source)

	result["reference"] = reference

	client = PostExClient(account_doc)
	try:
		dist = client.add_order(payload)
	except PostExAPIError as exc:
		return _handle_post_failure(so, account_doc, client, payload, reference, exc, source, result)

	tracking = (dist.get("trackingNumber") or dist.get("tracking") or "").strip()
	if not tracking:
		# PostEx accepted the order but gave no tracking number: just as
		# unconfirmed as a timeout, so reconcile instead of sending it again.
		error = PostExAPIError("PostEx did not return a tracking number")
		error.ambiguous = True
		return _handle_post_failure(so, account_doc, client, payload, reference, error, source, result)

	clash = _tracking_owner(account_doc, tracking, so.name)
	if clash:
		_log(so, account_doc.name, reference, "Failed", payload, dist, f"tracking already used by {clash}", source)
		return _fail(so, result, f"tracking {tracking} is already used by {clash}", source)

	status = map_status(dist.get("transactionStatus")) or "Booked"
	_write_booking(so, account_doc, tracking, status)
	_log(so, account_doc.name, reference, "Success", payload, dist, None, source)
	so.add_comment(
		text=f"PostEx booking created ({source}): {tracking} on account {account_doc.name}",
		comment_email=None,
	)

	result.update({"ok": 1, "tracking": tracking, "status": status, "reference": reference})
	return result


# ---------------------------------------------------------------------------
# A POST that may or may not have created the order
# ---------------------------------------------------------------------------
def _handle_post_failure(so, account_doc, client, payload, reference, exc, source, result) -> dict:
	"""Reconcile before ever retrying (or not retrying) a booking POST."""
	if not getattr(exc, "ambiguous", False):
		# A definitive rejection (4xx): nothing was created, a later retry is safe.
		_log(so, account_doc.name, reference, "Failed", payload, None, str(exc), source)
		return _fail(so, result, str(exc), source)

	try:
		found = _find_existing_booking(client, account_doc, reference)
	except PostExAPIError as lookup_error:
		_log(
			so,
			account_doc.name,
			reference,
			"Unconfirmed",
			payload,
			None,
			f"{exc}\nlookup failed: {lookup_error}",
			source,
		)
		return _park_in_doubt(so, account_doc, reference, source, result)

	if found:
		tracking = (found.get("trackingNumber") or "").strip()
		clash = _tracking_owner(account_doc, tracking, so.name)
		if clash:
			# The reference already belongs to another order: never take it over.
			_log(
				so,
				account_doc.name,
				reference,
				"Failed",
				payload,
				found,
				f"tracking already used by {clash}",
				source,
			)
			return _fail(so, result, f"tracking {tracking} is already used by {clash}", source)

		status = map_status(found.get("transactionStatus")) or "Booked"
		_write_booking(so, account_doc, tracking, status)
		_log(
			so,
			account_doc.name,
			reference,
			"Success",
			payload,
			found,
			f"recovered after: {str(exc)[:200]}",
			source,
		)
		so.add_comment(
			text=f"PostEx booking recovered after an API error ({source}): {tracking}",
			comment_email=None,
		)
		result.update(
			{
				"ok": 1,
				"tracking": tracking,
				"status": status,
				"reference": reference,
				"recovered": True,
			}
		)
		return result

	_log(so, account_doc.name, reference, "Unconfirmed", payload, None, str(exc), source)
	return _park_in_doubt(so, account_doc, reference, source, result)


def _park_in_doubt(so, account_doc, reference, source, result) -> dict:
	"""Park the order: PostEx may hold a booking, so nothing retries by itself.

	The flag is cleared by `clear_booking_doubt()` (a human checked PostEx) or by
	the next sync when the reference shows up in the feed with a tracking number.
	"""
	frappe.db.set_value(
		"Sales Order",
		so.name,
		{
			"custom_postex_booking_doubt": 1,
			"custom_postex_account": account_doc.name,
			"custom_courier": POSTEX_COURIER,
		},
	)
	frappe.db.commit()
	so.add_comment(
		text=(
			f"PostEx booking unconfirmed ({source}): not retried automatically. "
			f"Check reference {reference} at PostEx."
		),
		comment_email=None,
	)
	message = _(
		"The booking could not be confirmed (reference {0}). PostEx may have created the "
		"order, so it was NOT retried. Check that reference at PostEx: if it exists, fetch "
		"the status or clear the doubt once you have the tracking; if it does not, press "
		"'Clear booking doubt' and book again."
	).format(reference)
	result.update({"error": message, "doubt": 1, "reference": reference})
	return result


def _find_existing_booking(client, account_doc, reference):
	"""Look the reference up in the feed — the only idempotency key PostEx has."""
	window = int(account_doc.get("sync_window_days") or 60)
	today = nowdate()
	return _choose_booking(client.list_orders(add_days(today, -window), today), reference)


def _choose_booking(items, reference):
	"""Newest usable booking in the feed for `reference`, or None.

	Pure on purpose: the rules are pinned by `tests/test_booking_recovery.py`.
	"""
	key = norm(reference)
	if not key:
		return None
	best = None
	for item in items or []:
		if norm(item.get("orderRefNumber")) != key:
			continue
		if not (item.get("trackingNumber") or "").strip():
			continue
		if map_status(item.get("transactionStatus")) in DEAD_STATUSES:
			continue
		if best is None or str(item.get("transactionDate") or "") >= str(
			best.get("transactionDate") or ""
		):
			best = item
	return best


def _tracking_owner(account_doc, tracking, sales_order):
	"""The other Sales Order already carrying this tracking number, if any."""
	return frappe.db.get_value(
		"Sales Order",
		{
			"custom_postex_account": account_doc.name,
			"custom_postex_tracking_number": tracking,
			"name": ("!=", sales_order),
		},
		"name",
	)


def _write_booking(so, account_doc, tracking, status):
	frappe.db.set_value(
		"Sales Order",
		so.name,
		{
			"custom_courier": POSTEX_COURIER,
			"custom_postex_account": account_doc.name,
			"custom_postex_tracking_number": tracking,
			"custom_postex_status": status,
			"custom_postex_last_synced_on": now_datetime(),
			# a tracking number settles any doubt
			"custom_postex_booking_doubt": 0,
		},
	)


def clear_booking_doubt(sales_order, source="manual") -> dict:
	"""A human confirmed PostEx has no such booking: allow a new attempt."""
	so = frappe.get_doc("Sales Order", sales_order)
	if (so.get("custom_postex_tracking_number") or "").strip():
		return {"ok": 0, "error": "the order already has a tracking number"}
	if not cint(so.get("custom_postex_booking_doubt")):
		return {"ok": 0, "error": "this order is not waiting for a booking decision"}

	frappe.db.set_value(
		"Sales Order",
		so.name,
		{
			"custom_postex_booking_doubt": 0,
			# back to the head of the cursor, so a sync re-reads the reference
			"custom_postex_last_attempt_on": None,
		},
	)
	frappe.db.commit()
	so.add_comment(
		text=f"PostEx booking doubt cleared ({source}); a new booking may be created.",
		comment_email=None,
	)
	return {"ok": 1, "so": so.name}


# ---------------------------------------------------------------------------
# Cancelling / reopening a booking
# ---------------------------------------------------------------------------
def cancel_booking(sales_order, account=None, reason=None, source="manual") -> dict:
	"""Cancel the booking at PostEx for an order that never physically left.

	Runs under the same per-order lock as `book_order`, so a booking and a
	cancellation for one order can never be in flight together.
	"""
	from frappe.utils.synchronization import filelock

	so = frappe.get_doc("Sales Order", sales_order)
	try:
		with filelock(f"postex-book-{so.name}", timeout=0):
			return _cancel_booking(so, account=account, reason=reason, source=source)
	except LockTimeoutError:
		return {
			"ok": 0,
			"so": so.name,
			"error": _("another booking operation for this order is in progress"),
		}


def _cancel_booking(so, account=None, reason=None, source="manual") -> dict:
	"""Cancel the booking for an order that never physically left the store.

	Safety rules (a cancellation must never desync stock or revenue):

	* nothing has been delivered or invoiced — a delivered parcel is a return,
	  not a cancelled booking;
	* no **submitted** pickup transfer — once stock sits in the transit warehouse
	  the parcel is with the courier, and cancelling would leave the stock in
	  transit forever (let it come back as a Returned order instead);
	* PostEx is cancelled **first**. A 404 (PostEx has no such tracking number,
	  usually because it was already cancelled there) still allows the local
	  cancellation; any other error aborts it, so the books never claim a
	  cancelled booking that the courier still holds.

	A cancelled Sales Order (docstatus 2) is deliberately accepted: "customer
	cancelled the sale" is the main reason to cancel a booking, and the merchant
	may well cancel the sale in ERPNext before they get here.

	The Sales Order itself is not touched: cancelling the sale is the merchant's
	own ERPNext action.
	"""
	result = {"ok": 0, "so": so.name}

	if so.docstatus == 0:
		return _fail(
			so, result, "the Sales Order is still a draft; submit it first", source, status="Skipped"
		)
	if cint(so.get("custom_postex_booking_cancelled")):
		return {"ok": 1, "so": so.name, "skipped": "already cancelled"}
	if (so.get("custom_courier") or "") not in ("", POSTEX_COURIER):
		return _fail(
			so, result, f"order is booked with courier '{so.custom_courier}'", source, status="Skipped"
		)

	tracking = (so.get("custom_postex_tracking_number") or "").strip()
	if not tracking:
		return _fail(so, result, "order has no PostEx tracking number", source, status="Skipped")

	status = (so.get("custom_postex_status") or "").strip()
	if status in ("Delivered", "Returned"):
		return _fail(
			so,
			result,
			f"courier status is already {status}: a delivered or returned parcel cannot be "
			"cancelled, use a return instead",
			source,
			status="Skipped",
		)

	invoice = existing_invoice(so.name)
	if so.get("custom_postex_sales_invoice") or so.get("custom_postex_delivery_note") or invoice:
		return _fail(
			so,
			result,
			"order is already delivered/invoiced"
			+ (f" ({invoice})" if invoice else "")
			+ ": cancel a booking only before pickup; use a return or credit note instead",
			source,
			status="Skipped",
		)
	if flt(so.get("per_delivered")) >= 100:
		return _fail(
			so,
			result,
			"stock has already been delivered against this order; a cancellation would "
			"desync inventory - use a return instead",
			source,
			status="Skipped",
		)

	pickup = so.get("custom_postex_pickup_entry")
	if pickup and frappe.db.get_value("Stock Entry", pickup, "docstatus") == 1:
		return _fail(
			so,
			result,
			f"stock already moved to the transit warehouse ({pickup}): the parcel is with "
			"the courier, so let it come back as a return instead of cancelling",
			source,
			status="Skipped",
		)

	try:
		account_doc = account_for_document(so, account=account)
	except PostExConfigError as exc:
		return _fail(so, result, str(exc), source)
	if cint(account_doc.get("dry_run")):
		return _fail(so, result, "dry run: nothing sent to PostEx", source, status="Skipped")

	reference = so.get("shopify_order_number") or so.get("po_no") or so.name
	note = ""
	try:
		PostExClient(account_doc).cancel_order(tracking.replace(" ", ""))
	except PostExAPIError as exc:
		if getattr(exc, "status_code", None) == 404:
			note = "PostEx has no record of this tracking number (already cancelled there)."
		else:
			return _fail(so, result, str(exc)[:250], source)

	stamp = now_datetime()
	frappe.db.set_value(
		"Sales Order",
		so.name,
		{
			"custom_postex_booking_cancelled": 1,
			"custom_postex_cancelled_on": stamp,
			"custom_postex_cancel_reason": (reason or "")[:140] or None,
			"custom_postex_status": "Cancelled",
			"custom_postex_status_changed_on": stamp,
			"custom_postex_last_synced_on": stamp,
			"custom_postex_sync_failures": 0,
			"custom_postex_needs_attention": 0,
			# The parcel no longer exists at PostEx: a dead tracking number must
			# not linger on the order (a re-booking needs a clean slate, and a
			# different courier must not inherit it). It is kept in the Booking
			# Log row and the timeline comment below.
			"custom_postex_tracking_number": None,
			"custom_postex_delivered_on": None,
		},
	)
	frappe.db.commit()

	_log(
		so,
		account_doc.name,
		reference,
		"Cancelled",
		{"trackingNumber": tracking},
		None,
		None,
		source,
		remarks=reason,
		tracking=tracking,
	)
	so.add_comment(
		text=f"PostEx booking cancelled ({source}): {tracking}"
		+ (f" — {reason}" if reason else "")
	)
	result.update(
		{
			"ok": 1,
			"account": account_doc.name,
			"tracking": tracking,
			"status": "Cancelled",
			"note": note,
			"next_step": (
				"Cancelled at PostEx and removed from the automatic sync. "
				"The Sales Order itself is unchanged — cancel it in ERPNext if the sale is dead."
			),
		}
	)
	return result


def reopen_booking(sales_order, source="manual") -> dict:
	"""Undo a cancellation so the order is polled again (cancel by mistake)."""
	from frappe.utils.synchronization import filelock

	so = frappe.get_doc("Sales Order", sales_order)
	try:
		with filelock(f"postex-book-{so.name}", timeout=0):
			return _reopen_booking(so, source=source)
	except LockTimeoutError:
		return {
			"ok": 0,
			"so": so.name,
			"error": _("another booking operation for this order is in progress"),
		}


def _reopen_booking(so, source="manual") -> dict:
	"""Undo a cancellation so the order is polled again (cancel by mistake)."""
	if not cint(so.get("custom_postex_booking_cancelled")):
		return {"ok": 0, "error": "this booking is not cancelled"}

	values = {
		"custom_postex_booking_cancelled": 0,
		"custom_postex_cancelled_on": None,
		"custom_postex_cancel_reason": None,
		# The parcel was cancelled at PostEx, so its tracking number is dead: clear
		# it (and any link left on it) so the order can be booked again. Safe by
		# construction — `cancel_booking` only succeeds when nothing has moved.
		"custom_postex_tracking_number": None,
		# Blank puts it back in the active scope; the next sync re-reads the
		# real status from PostEx.
		"custom_postex_status": "",
		"custom_postex_status_changed_on": None,
		"custom_postex_last_attempt_on": None,
		"custom_postex_sync_failures": 0,
		"custom_postex_needs_attention": 0,
	}
	# A closed order cannot receive a delivery note or invoice: if the terminal
	# handler closed it, opening it is part of undoing the cancellation.
	if cint(so.get("custom_postex_closed_on")):
		values["custom_postex_closed_on"] = None
		values["status"] = "To Deliver and Bill"

	frappe.db.set_value("Sales Order", so.name, values)
	frappe.db.commit()
	so.add_comment(text=f"PostEx booking reopened ({source}); status will be re-read from PostEx.")
	return {"ok": 1, "so": so.name}


def on_sales_order_submit(doc, method=None):
	"""Auto-book a freshly submitted order when the account has it enabled."""
	try:
		if (doc.get("custom_postex_tracking_number") or "").strip():
			return
		if (doc.get("custom_courier") or "") not in ("", POSTEX_COURIER):
			return
		if (doc.get("custom_shopify_state") or ""):
			return  # historical import, never book those
		from .config import accounts_for_company

		if not accounts_for_company(doc.company):
			return  # company does not use PostEx at all
		try:
			account = account_for_document(doc)
		except PostExConfigError:
			return
		if not cint(account.enabled) or not cint(account.enable_auto_booking):
			return
		if not (account.pickup_address or "").strip():
			return
		frappe.enqueue(
			"postex_integration.booking.book_order",
			queue="short",
			enqueue_after_commit=True,
			job_name=f"postex-book-{doc.name}",
			sales_order=doc.name,
			account=account.name,
			source="auto",
		)
	except Exception:
		# Never block a Sales Order submit because of the courier integration.
		log_error(
			"PostEx auto-booking enqueue failed",
			f"{doc.name}\n{frappe.get_traceback()}",
			"Sales Order",
			doc.name,
		)


def on_sales_order_cancel(doc, method=None):
	"""Auto-cancel the PostEx booking when its Sales Order is cancelled.

	Opt-in per account (`enable_auto_cancel`, **off by default**) and only for a
	PostEx booking that still exists. The cancellation itself runs in a
	background job under the per-order lock and applies exactly the same safety
	rules as the button (`cancel_booking`): nothing delivered/invoiced, no
	submitted pickup transfer — otherwise it refuses and says why.
	"""
	try:
		if (doc.get("custom_courier") or "") not in ("", POSTEX_COURIER):
			return
		if not (doc.get("custom_postex_tracking_number") or "").strip():
			return
		if cint(doc.get("custom_postex_booking_cancelled")):
			return
		from .config import accounts_for_company

		if not accounts_for_company(doc.company):
			return  # company does not use PostEx at all
		try:
			account = account_for_document(doc)
		except PostExConfigError:
			return
		if not cint(account.enabled) or not cint(account.get("enable_auto_cancel")):
			return
		frappe.enqueue(
			"postex_integration.booking.cancel_booking",
			queue="short",
			enqueue_after_commit=True,
			job_name=f"postex-cancel-{doc.name}",
			sales_order=doc.name,
			account=account.name,
			reason="Sales Order cancelled",
			source="auto",
		)
	except Exception:
		# Never block a Sales Order cancel because of the courier integration.
		log_error(
			"PostEx auto-cancel enqueue failed",
			f"{doc.name}\n{frappe.get_traceback()}",
			"Sales Order",
			doc.name,
		)


def ensure_po_no(doc, method=None):
	"""Mirror the Shopify order name into `po_no`.

	The Shopify connector writes `shopify_order_number` only; every importer,
	report and matching script on this site looks at `po_no`. When the two drift
	apart the same sale gets a second Sales Order (31-10 / 01-11 duplication of
	VM5347, VM5350, VM5353, VM5354, VM5356), because a `po_no` lookup finds
	nothing and a new document is created.

	Filling `po_no` from `shopify_order_number` at insert/save time keeps both
	keys in step, so a match on either one finds the same order. Never
	overwrites an existing `po_no`.
	"""
	if (doc.get("po_no") or "").strip():
		return
	shopify_name = (doc.get("shopify_order_number") or "").strip()
	if shopify_name:
		doc.po_no = shopify_name


def validate_account_change(doc, method=None):
	"""Keep `custom_postex_account` consistent with the document.

	The field is editable so a company with several PostEx accounts can pick one,
	but it must always belong to the order's company, and an order that already
	carries a tracking number has to stay on the account that booked it —
	otherwise its settlement would be attributed to another wallet.
	"""
	account = doc.get("custom_postex_account")
	if not account:
		return

	account_company = frappe.db.get_value("PostEx Account", account, "company")
	if not account_company:
		frappe.throw(_("PostEx Account {0} does not exist.").format(account))
	if account_company != doc.company:
		frappe.throw(
			_("PostEx Account {0} belongs to {1}, not {2}.").format(
				account, account_company, doc.company
			)
		)

	if doc.get("custom_postex_tracking_number") and not doc.is_new():
		previous = frappe.db.get_value("Sales Order", doc.name, "custom_postex_account")
		if previous and previous != account:
			frappe.throw(
				_(
					"This order was booked on PostEx Account {0} (tracking {1}). "
					"Changing the account would misattribute its settlement."
				).format(previous, doc.get("custom_postex_tracking_number")),
				title=_("PostEx account cannot be changed"),
			)


def booking_sweep(account=None, limit=50) -> dict:
	"""Book submitted orders that fell through (switch was off when created).

	The sweep is the only automatic retry path, so it must skip exactly the
	orders a retry would be wrong for: an unconfirmed previous attempt and a
	cancelled booking. Both filters are SQL: `LIMIT` runs in the database, and a
	Python skip would let those rows occupy the sweep window on every run.
	"""
	conditions = [
		"docstatus = 1",
		"ifnull(custom_postex_tracking_number, '') = ''",
		"ifnull(custom_postex_booking_doubt, 0) = 0",
		"ifnull(custom_postex_booking_cancelled, 0) = 0",
	]
	if frappe.get_meta("Sales Order").has_field("custom_shopify_state"):
		conditions.append("ifnull(custom_shopify_state, '') = ''")

	orders = frappe.db.sql(
		f"""
		select name from `tabSales Order`
		where {" and ".join(conditions)}
		order by modified desc
		limit %s
		""",
		(int(limit),),
		as_dict=True,
	)

	result = {"checked": 0, "booked": [], "failed": []}
	for row in orders:
		name = row.get("name")
		so = frappe.get_doc("Sales Order", name)
		try:
			acc = account_for_document(so, account=account)
		except PostExConfigError:
			continue
		if not (cint(acc.enabled) and cint(acc.enable_auto_booking)):
			continue
		result["checked"] += 1
		booked = book_order(so.name, account=acc.name, source="sweep")
		if booked.get("ok"):
			result["booked"].append(f"{so.name}={booked.get('tracking')}")
		elif booked.get("error"):
			result["failed"].append(f"{so.name}: {booked['error'][:100]}")
	return result


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------
def build_payload(so, config) -> tuple[dict, str]:
	"""Build the add-order payload. Returns (payload, reference)."""
	pickup_address = (config.account.get("pickup_address") or "").strip()
	pickup_city = (config.account.get("pickup_city") or "").strip()
	if not pickup_address or not pickup_city:
		raise PostExConfigError(
			f"PostEx Account {config.name} has no pickup address/city; "
			"fill them in before booking."
		)

	phone = _customer_phone(so)
	if not phone:
		raise PostExConfigError(f"Sales Order {so.name} has no customer phone number.")

	address_doc = None
	for fieldname in ("shipping_address_name", "customer_address"):
		if so.get(fieldname):
			address_doc = frappe.get_doc("Address", so.get(fieldname))
			break

	parts = []
	if address_doc:
		for fieldname in ("address_line1", "address_line2", "city"):
			value = (address_doc.get(fieldname) or "").strip()
			if value:
				parts.append(value)
	delivery_address = ", ".join(parts) or (so.get("customer_name") or so.customer)
	city = (address_doc.get("city") if address_doc else "") or ""

	qty = sum(flt(item.qty) for item in so.items)
	detail = [item.item_name or item.item_code for item in so.items]
	reference = so.get("shopify_order_number") or so.get("po_no") or so.name

	payload = {
		"orderRefNumber": reference,
		"customerName": so.get("customer_name") or so.customer,
		"customerPhone": phone,
		"deliveryAddress": delivery_address,
		"cityName": city,
		"invoicePayment": round(flt(so.grand_total), 2),
		"items": int(qty) if qty else 1,
		"orderDetail": ", ".join(detail) or config.account.get("default_order_detail") or "Order",
		"orderType": "Normal",
		"pickupAddress": pickup_address,
		"pickupCity": pickup_city,
		"weight": flt(config.account.get("default_weight")) or 0.5,
	}
	return payload, str(reference)


def _customer_phone(so) -> str:
	phone = (so.get("contact_mobile") or so.get("contact_phone") or "").strip()
	if phone:
		return phone
	for fieldname in ("shipping_address_name", "customer_address"):
		if so.get(fieldname):
			value = frappe.db.get_value("Address", so.get(fieldname), "phone")
			if value:
				return value.strip()
	return (frappe.db.get_value("Customer", so.customer, "mobile_no") or "").strip()


# ---------------------------------------------------------------------------
# Booking log
# ---------------------------------------------------------------------------
def _successful_booking(sales_order, account):
	return frappe.db.get_value(
		BOOKING_LOG,
		{"sales_order": sales_order, "postex_account": account, "status": "Success"},
		"name",
	)


def _log(so, account, reference, status, payload, response, error, source, remarks=None, tracking=None):
	try:
		frappe.get_doc(
			{
				"doctype": BOOKING_LOG,
				"sales_order": so.name,
				"company": so.company,
				"postex_account": account,
				"reference_number": reference,
				"status": status,
				"source": source,
				"request_hash": hashlib.sha1(
					json.dumps(payload, sort_keys=True, default=str).encode()
				).hexdigest(),
				"request_payload": json.dumps(payload, indent=1, default=str)[:60000],
				"response_snippet": dump_snippet(response, 600),
				"tracking_number": tracking
				or ((response or {}).get("trackingNumber") if isinstance(response, dict) else None),
				"error_message": (error or "")[:500] or None,
				"remarks": (remarks or "")[:500] or None,
			}
		).insert(ignore_permissions=True)
	except Exception:
		log_error("PostEx booking log failed", frappe.get_traceback(), "Sales Order", so.name)


def _fail(so, result, message, source, status="Failed"):
	result["error"] = message
	if status == "Failed":
		_log(so, result.get("account"), None, status, {}, None, message, source)
	return result
