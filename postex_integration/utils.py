"""Small shared helpers: status mapping, field introspection, safe doc helpers."""

import json

import frappe
from frappe.utils import flt, now_datetime, nowdate

from .constants import HISTORY_CODES, STATUS_MAP


# ---------------------------------------------------------------------------
# Text / status
# ---------------------------------------------------------------------------
def norm(value) -> str:
	"""Upper-case, trimmed key used to match PostEx order references."""
	return (value or "").strip().upper()


def match_status(raw) -> str:
	"""Map a free-text PostEx status to one of our stored options.

	Order of the needle table matters (see constants.STATUS_MAP).
	"""
	text = (raw or "").lower().replace("-", " ").replace("_", " ")
	text = " ".join(text.split())
	if not text:
		return ""
	for needle, mapped in STATUS_MAP:
		if needle in text:
			return mapped
	return ""


def map_status(status_text, history=None) -> str:
	"""Resolve the ERPNext courier status for a PostEx payload.

	`transactionStatus` wins whenever present. History codes are only a
	fallback when PostEx gave us no status text at all (unbooked orders carry
	history code 0001 / "At ... Warehouse" and must not become "Booked").
	"""
	mapped = match_status(status_text)
	if mapped:
		return mapped
	if (status_text or "").strip():
		return "Unknown"

	last_code = ""
	last_message = ""
	for entry in history or []:
		if not isinstance(entry, dict):
			continue
		code = (entry.get("transactionStatusMessageCode") or "").strip()
		if code in HISTORY_CODES:
			last_code = code
		message = entry.get("transactionStatusMessage") or ""
		if message.strip():
			last_message = message
	if last_code:
		return HISTORY_CODES[last_code]
	return match_status(last_message) or "Unknown"


# ---------------------------------------------------------------------------
# DocType introspection
# ---------------------------------------------------------------------------
def field_exists(doctype: str, fieldname: str) -> bool:
	return frappe.get_meta(doctype).has_field(fieldname)


def has_any_field(doctype: str, *fieldnames) -> bool:
	meta = frappe.get_meta(doctype)
	return all(meta.has_field(f) for f in fieldnames)


def exclude_historical(doctype: str = "Sales Order") -> dict:
	"""Skip orders imported from history.

	`custom_shopify_state` marks historical imported orders whose stock and
	revenue were already booked by the import; live helpers must never touch
	them. The field belongs to the import project, so tolerate its absence.
	"""
	if field_exists(doctype, "custom_shopify_state"):
		# `("is", "not set")` expands to `ifnull(field,'') = ''`, which matches
		# both NULL and ''. `IN ('', NULL)` would never match NULL.
		return {"custom_shopify_state": ("is", "not set")}
	return {}


def set_values(doctype: str, name: str, values: dict) -> None:
	if not values:
		return
	frappe.db.set_value(doctype, name, values, update_modified=False)


# ---------------------------------------------------------------------------
# Per-order failure tracking (quarantine)
# ---------------------------------------------------------------------------
def bump_failures(sales_order: str, step: str | None = None, error: str | None = None) -> int:
	"""Count one more consecutive failure for an order.

	After `MAX_SYNC_FAILURES` in a row the order is quarantined
	(`custom_postex_needs_attention`), which every automatic sweep skips until a
	human re-queues it — that is what stops a permanently broken order from
	consuming a scan slot forever.
	"""
	from .constants import MAX_SYNC_FAILURES

	if not field_exists("Sales Order", "custom_postex_sync_failures"):
		return 0

	current = int(frappe.db.get_value("Sales Order", sales_order, "custom_postex_sync_failures") or 0) + 1
	values = {"custom_postex_sync_failures": current}
	if current >= MAX_SYNC_FAILURES:
		values["custom_postex_needs_attention"] = 1
	frappe.db.set_value("Sales Order", sales_order, values, update_modified=False)
	if step:
		log_error(
			f"PostEx {step} failed",
			f"{sales_order}\n{(error or '')[:500]}",
			"Sales Order",
			sales_order,
		)
	return current


def clear_failures(sales_order: str) -> None:
	"""Reset the consecutive-failure counter after a successful step."""
	if not field_exists("Sales Order", "custom_postex_sync_failures"):
		return
	frappe.db.set_value(
		"Sales Order",
		sales_order,
		{"custom_postex_sync_failures": 0, "custom_postex_needs_attention": 0},
		update_modified=False,
	)



# ---------------------------------------------------------------------------
# Document helpers
# ---------------------------------------------------------------------------
def submit(doc, ignore_permissions: bool = True):
	doc.flags.ignore_permissions = ignore_permissions
	doc.insert(ignore_permissions=ignore_permissions)
	doc.submit()
	return doc


def log_error(title: str, message: str, reference_doctype=None, reference_name=None):
	frappe.log_error(
		title=title[:140],
		message=message,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
	)


def dump_snippet(value, limit: int = 900) -> str:
	try:
		return json.dumps(value, default=str)[:limit]
	except Exception:
		return str(value)[:limit]


def today_or(value, fallback=None):
	value = (value or "")[:10]
	return value or fallback or nowdate()


def now():
	return now_datetime()


def money(value) -> float:
	return flt(value, 2)


def net_rate(amount, qty) -> float:
	"""Per-unit rate that reproduces an exact line amount.

	ERPNext recomputes `amount = qty x rate`, so a line with a discount or a zero
	rate must be passed as its **net** rate; passing `rate` alone lets ERPNext
	re-price a zero rate from the price list.
	"""
	qty = flt(qty)
	if qty <= 0:
		return 0.0
	return money(flt(amount) / qty)
