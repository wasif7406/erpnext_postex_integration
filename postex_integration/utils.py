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
