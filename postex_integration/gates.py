"""Gates that decide whether an order may be booked **automatically**.

Both gates are opt-in per PostEx Account and both only ever affect automatic
booking — the "Create PostEx Booking" button always works, because a human
pressing it has already made the decision.

* **Return risk** — a high-risk parcel is often refused at the door, and a
  refusal costs the delivery attempt plus the return leg. Risk comes from the
  `ai_return_prediction` score (`custom_return_risk_pct` / `_band`).
* **Products** — the merchant may want ERPNext to book only *some* products,
  while another catalogue keeps being booked elsewhere. An order qualifies only
  when **every** line matches the list: booking a mixed parcel would put the
  other product's unit on a parcel ERPNext did not intend to create.

An empty/zero setting means "no restriction": a ticked box with nothing behind
it must never stop every booking.
"""

from frappe.utils import cint, flt

RISK_PCT_FIELD = "custom_return_risk_pct"
RISK_BAND_FIELD = "custom_return_risk_band"

# Set on the Sales Order when a gate holds it, cleared when it is booked.
REVIEW_FIELD = "custom_postex_review_reason"


# ---------------------------------------------------------------------------
# Return risk
# ---------------------------------------------------------------------------
def risk_gate_blocker(account, sales_order) -> str | None:
	"""Why this order must not be auto-booked because of its return risk."""
	if not account or not cint(account.get("enable_risk_gate")):
		return None

	limit = flt(account.get("auto_book_max_return_risk"))
	if limit <= 0:
		return None

	pct = flt(sales_order.get(RISK_PCT_FIELD))
	band = (sales_order.get(RISK_BAND_FIELD) or "").strip()

	# The scoring app leaves pct 0 and an empty band on an order it could not
	# score (no delivery address). That is "unknown", not "safe".
	if not band and pct <= 0:
		return "no return-risk score on this order"

	if pct >= limit:
		return f"return risk {pct:g}% is not below the {limit:g}% auto-booking limit"

	return None


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
def _row_matches(item_code, item_group, rows) -> bool:
	for row in rows:
		if row.get("item_code") and row.get("item_code") == item_code:
			return True
		if row.get("item_group") and row.get("item_group") == item_group:
			return True
	return False


def product_gate_blocker(account, sales_order) -> str | None:
	"""Why this order must not be auto-booked because of its products.

	Every line has to be listed (by item or by item group). One unlisted product
	makes the whole order manual — the parcel would otherwise be created for a
	catalogue ERPNext is not supposed to book.
	"""
	if not account or not cint(account.get("enable_item_gate")):
		return None

	rows = account.get("auto_booking_items") or []
	if not rows:
		return None

	items = sales_order.get("items") or []
	if not items:
		return "the order has no items"

	for item in items:
		code = item.get("item_code")
		if not _row_matches(code, item.get("item_group"), rows):
			return f"product {code} is not in the auto-booking list"

	return None


# ---------------------------------------------------------------------------
# Combined
# ---------------------------------------------------------------------------
def gates_enabled(account) -> bool:
	"""True when at least one gate is on — lets callers skip loading order items."""
	if not account:
		return False
	return bool(cint(account.get("enable_risk_gate")) or cint(account.get("enable_item_gate")))


def auto_booking_blocker(account, sales_order) -> str | None:
	"""The first reason this order must wait for a human, or None.

	Risk first: it needs no items, so an unloaded order can still be judged.
	"""
	return risk_gate_blocker(account, sales_order) or product_gate_blocker(account, sales_order)
