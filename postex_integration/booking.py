"""Booking a Sales Order at PostEx (manual button, auto-booking hook, sweep)."""

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from .client import PostExClient
from .config import account_for_document, config_for_document
from .constants import POSTEX_COURIER
from .exceptions import PostExAPIError, PostExConfigError
from .utils import dump_snippet, log_error, map_status

BOOKING_LOG = "PostEx Booking Log"

# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def book_order(sales_order, account=None, source="manual") -> dict:
	"""Book one Sales Order and write the result back on the order."""
	so = frappe.get_doc("Sales Order", sales_order)
	result = {
		"ok": 0,
		"so": so.name,
		"account": None,
		"source": source,
	}

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

	existing = _successful_booking(so.name, account_doc.name)
	if existing:
		return _fail(so, result, f"already booked ({existing})", source, status="Skipped")

	if not account_doc.enabled:
		return _fail(so, result, f"PostEx Account {account_doc.name} is disabled", source, status="Skipped")
	if account_doc.get("dry_run"):
		return _fail(so, result, "dry run: booking not sent to PostEx", source, status="Skipped")

	config = config_for_document(so, account=account_doc.name)
	payload, reference = build_payload(so, config)
	result["reference"] = reference

	try:
		client = PostExClient(account_doc)
		dist = client.add_order(payload)
	except PostExAPIError as exc:
		_log(so, account_doc.name, reference, "Failed", payload, None, str(exc), source)
		return _fail(so, result, str(exc), source)

	tracking = (dist.get("trackingNumber") or dist.get("tracking") or "").strip()
	if not tracking:
		message = "PostEx did not return a tracking number"
		_log(so, account_doc.name, reference, "Failed", payload, dist, message, source)
		result.update({"error": message, "response": dump_snippet(dist, 400)})
		return result

	# A tracking number may only ever belong to one Sales Order of this account.
	clash = frappe.db.get_value(
		"Sales Order",
		{
			"custom_postex_account": account_doc.name,
			"custom_postex_tracking_number": tracking,
			"name": ("!=", so.name),
		},
		"name",
	)
	if clash:
		_log(so, account_doc.name, reference, "Failed", payload, dist, f"tracking already used by {clash}", source)
		return _fail(so, result, f"tracking {tracking} is already used by {clash}", source)

	status = map_status(dist.get("transactionStatus")) or "Booked"
	frappe.db.set_value(
		"Sales Order",
		so.name,
		{
			"custom_courier": POSTEX_COURIER,
			"custom_postex_account": account_doc.name,
			"custom_postex_tracking_number": tracking,
			"custom_postex_status": status,
			"custom_postex_last_synced_on": now_datetime(),
		},
	)
	_log(so, account_doc.name, reference, "Success", payload, dist, None, source)
	so.add_comment(
		text=f"PostEx booking created ({source}): {tracking} on account {account_doc.name}",
		comment_email=None,
	)

	result.update({"ok": 1, "tracking": tracking, "status": status, "reference": reference})
	return result


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

	The courier and the account are validated per document inside `book_order`,
	so the query stays simple (an empty Select is NULL and would not match a
	SQL `IN` list).
	"""
	filters = {
		"docstatus": 1,
		"custom_postex_tracking_number": ("is", "not set"),
	}
	if frappe.get_meta("Sales Order").has_field("custom_shopify_state"):
		filters["custom_shopify_state"] = ("in", ["", None])

	orders = frappe.get_all(
		"Sales Order",
		filters=filters,
		pluck="name",
		limit=limit,
		order_by="modified desc",
	)
	result = {"checked": 0, "booked": [], "failed": []}
	for name in orders:
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


def _log(so, account, reference, status, payload, response, error, source):
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
				"tracking_number": (response or {}).get("trackingNumber") if isinstance(response, dict) else None,
				"error_message": (error or "")[:500] or None,
			}
		).insert(ignore_permissions=True)
	except Exception:
		log_error("PostEx booking log failed", frappe.get_traceback(), "Sales Order", so.name)


def _fail(so, result, message, source, status="Failed"):
	result["error"] = message
	if status == "Failed":
		_log(so, result.get("account"), None, status, {}, None, message, source)
	return result
