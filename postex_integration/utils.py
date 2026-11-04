"""Small shared helpers: status mapping, field introspection, safe doc helpers."""

import json
import re

import frappe
from frappe.utils import flt, getdate, now_datetime, nowdate

from .constants import HISTORY_CODES, STATUS_MAP


# ---------------------------------------------------------------------------
# Text / status
# ---------------------------------------------------------------------------
# Separators that different systems sprinkle into the same reference:
# `#3144`, `VM-0012`, `VM 0012`, `VM_0012`. They are noise for matching.
REF_SEPARATORS = re.compile(r"[\s#\-_.]+")
# Leading zeros of a numeric group: `VM0012` and `VM12` are the same order.
# The look-behind keeps a number that genuinely starts mid-string (`1002`).
REF_ZERO_PADDING = re.compile(r"(?<!\d)0+(\d+)")


def norm(value) -> str:
	"""Canonical key used to match a PostEx order reference.

	The same order is written differently by different systems — the Shopify
	name, the old OMS counter, PostEx's `orderRefNumber` — so the key drops the
	separators and the zero padding and upper-cases everything:

	    ``#3144`` == ``3144`` · ``VM#0012`` == ``VM0012`` == ``VM-12``

	It is only ever used as a **matching key**; the stored values are never
	rewritten, so the merchant's numbering stays exactly as entered.
	"""
	text = (value or "").strip().upper()
	if not text:
		return ""
	text = REF_SEPARATORS.sub("", text)
	return REF_ZERO_PADDING.sub(r"\1", text)


# ---------------------------------------------------------------------------
# Dates from a PostEx payload
# ---------------------------------------------------------------------------
# The status-history entry that means "delivered" (guide 3.8.3).
DELIVERED_HISTORY_CODE = "0005"

# Date-ish keys the order object itself may carry. The guide documents
# `orderDeliveryDate`; the rest are accepted because PostEx accounts differ.
DELIVERY_DATE_KEYS = (
	"orderDeliveryDate",
	"deliveryDate",
	"deliveredDate",
	"delivery_date",
)

# Date-ish keys a history entry may carry. PostEx does not document one, so the
# lookup is tolerant instead of guessing a single name.
HISTORY_DATE_KEYS = (
	"transactionStatusMessageDate",
	"transactionStatusDate",
	"statusDateTime",
	"transactionDate",
	"statusDate",
	"date",
	"createdAt",
	"created",
)


def postex_date(value):
	"""Parse a PostEx date/time string; None when empty or unparseable."""
	if not value:
		return None
	try:
		return getdate(value)
	except Exception:
		return None


def delivery_date(payload):
	"""The date PostEx delivered the parcel, or None.

	`orderDeliveryDate` is the documented field (guide 3.16.3) but it is
	optional, so the delivered entry of the status history is the fallback.
	Nothing is guessed: an unparseable payload returns None and the caller keeps
	its own default.
	"""
	if not isinstance(payload, dict):
		return None

	candidates = [payload.get(key) for key in DELIVERY_DATE_KEYS]
	for entry in payload.get("transactionStatusHistory") or []:
		if not isinstance(entry, dict):
			continue
		if (entry.get("transactionStatusMessageCode") or "").strip() != DELIVERED_HISTORY_CODE:
			continue
		candidates.extend(entry.get(key) for key in HISTORY_DATE_KEYS)

	for value in candidates:
		date = postex_date(value)
		if date:
			return date
	return None


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


def exclude_historical_sql(doctype: str = "Sales Order") -> str:
	"""WHERE-clause twin of `exclude_historical()`.

	The stock helpers query with `frappe.db.sql` so that every predicate is
	applied **before** `LIMIT`; this keeps the historical-import rule identical
	to the `get_all` version instead of a second, drifting copy.
	"""
	if field_exists(doctype, "custom_shopify_state"):
		return "ifnull(custom_shopify_state, '') = ''"
	return ""


def exclude_quarantined_sql() -> str:
	"""WHERE-clause twin of the quarantine guard: NULL (pre-field rows) counts as
	"not quarantined", so the condition is `ifnull(...) = 0` and never `= 0`.

	It **must** be a SQL predicate. The stock helpers apply `LIMIT` before they
	load a document, so filtering in Python lets a quarantined order keep its
	slot on every run (its `modified` is never bumped) until the newest 100 rows
	are all stuck and no document can be created again.
	"""
	return "ifnull(custom_postex_needs_attention, 0) = 0"


def claim_field(doctype: str, name: str, fieldname: str, value: str, allow=None) -> bool:
	"""Atomically store `value` in a link field, but only if nobody else owns it.

	`frappe.db.set_value` writes unconditionally, which made the old
	"write, then read it back" check a no-op under concurrency: run A wrote its
	document and read it back, run B wrote its own and read that back, so **both**
	kept their document and one was orphaned with its stock/GL effect intact (the
	25-10 duplicate delivery note / invoice chain).

	A conditional UPDATE is the actual guard: only the first writer to see an
	empty field can store its value, and the read-back then tells the loser the
	truth. `allow` accepts one specific stale value — a link left behind by an
	interrupted run to a document that was never submitted — so an order can
	never be stuck on a dead claim.

	Returns True when `value` is the stored value at the end.
	"""
	if not fieldname.isidentifier():
		raise ValueError(f"unsafe field name: {fieldname!r}")

	condition = f"ifnull(`{fieldname}`, '') = ''"
	params = [value, now_datetime(), name]
	if allow:
		condition = f"({condition} or `{fieldname}` = %s)"
		params.append(allow)

	frappe.db.sql(
		f"""
		update `tab{doctype}`
		set `{fieldname}` = %s, `modified` = %s
		where `name` = %s
		  and {condition}
		""",
		tuple(params),
	)
	frappe.db.commit()
	return frappe.db.get_value(doctype, name, fieldname) == value


def set_values(doctype: str, name: str, values: dict) -> None:
	if not values:
		return
	frappe.db.set_value(doctype, name, values, update_modified=False)


def existing_invoice(sales_order: str):
	"""Any submitted-or-draft sales invoice (not a credit note) billing this order.

	Checked through `Sales Invoice Item.sales_order`, so an invoice created outside
	the app (or by an older integration) also counts. A cancelled invoice
	(docstatus 2) and a credit note (`Sales Invoice.is_return`) do not.
	"""
	rows = frappe.db.sql(
		"""
		select sii.parent
		from `tabSales Invoice Item` sii
		join `tabSales Invoice` si on si.name = sii.parent
		where sii.sales_order = %s
		  and sii.docstatus != 2
		  and ifnull(si.is_return, 0) = 0
		order by si.posting_date desc, si.name desc
		limit 1
		""",
		(sales_order,),
	)
	return rows[0][0] if rows else None


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
