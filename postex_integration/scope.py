"""Which orders a sync run is allowed to look at — pure, testable predicates.

The scope is decided by **order state**, never by a row count:

| order state                                        | polled                  |
|----------------------------------------------------|-------------------------|
| still moving (see ACTIVE_STATUSES)                 | every run               |
| Delivered / Returned inside the late-return window | once a day (slow sweep) |
| Delivered / Returned older than the window         | never                   |
| Cancelled / Expired with a closed Sales Order      | never                   |
| Lost / Damage / Stolen / Un-Assigned By Me         | never                   |
| other courier (Quick Delivery, Fast And Quick, …)  | never                   |
| historical import (`custom_shopify_state` set)     | never                   |
| quarantined (`custom_postex_needs_attention`)      | never (until re-queued) |

Nothing here touches the database, so the rules can be unit-tested without a
site (`tests/test_scope.py`).
"""

from frappe.utils import add_days, getdate, nowdate

from .constants import (
	ACTIVE_STATUSES,
	CLOSED_SALES_ORDER_STATUSES,
	FINAL_STATUSES,
	LATE_RETURN_STATUSES,
	LATE_RETURN_WINDOW_DAYS,
	POSTEX_COURIER,
	SCAN_ACTIVE,
	SCAN_LATE_RETURNS,
)


def is_our_courier(courier) -> bool:
	"""Blank means 'not decided yet', which the PostEx sync may still claim."""
	return (courier or "") in ("", POSTEX_COURIER)


def is_active(status) -> bool:
	"""Still moving, or not known yet: must be polled on every run."""
	status = (status or "").strip()
	if not status or status == "Unknown":
		# Never matched / unmapped text: the only way to learn the state is to
		# keep polling. `no data` orders stay here on purpose (see P3).
		return True
	if status in ACTIVE_STATUSES:
		return True
	if status in ("Cancelled", "Expired"):
		# Final only once the Sales Order is closed too.
		return True
	return False


def is_late_return_candidate(status, sales_order_status=None) -> bool:
	"""Delivered / Returned: polled once a day while the window is open.

	The Sales Order status deliberately does not matter here: a delivered order
	is usually already `Closed`, and a late return is discovered precisely by
	re-checking those orders.
	"""
	return (status or "") in LATE_RETURN_STATUSES


def is_final(status, sales_order_status=None) -> bool:
	"""Terminal with nothing left to learn."""
	status = (status or "").strip()
	if status in FINAL_STATUSES:
		return True
	if status in ("Cancelled", "Expired"):
		return (sales_order_status or "") in CLOSED_SALES_ORDER_STATUSES
	return False


def window_start(days: int = LATE_RETURN_WINDOW_DAYS) -> str:
	"""First day still inside the late-return window (`YYYY-MM-DD`)."""
	return str(add_days(getdate(nowdate()), -int(days or LATE_RETURN_WINDOW_DAYS)))


def in_late_window(changed_on, today=None, days: int = LATE_RETURN_WINDOW_DAYS) -> bool:
	"""True when `changed_on` is inside the last `days` days (inclusive).

	`changed_on` may be a datetime, a date or a string; None is treated as
	outside the window so a row without the marker is never swept.
	"""
	if not changed_on:
		return False
	try:
		changed = getdate(changed_on)
	except Exception:
		return False
	today = getdate(today) if today else getdate(nowdate())
	return changed >= getdate(add_days(today, -int(days or LATE_RETURN_WINDOW_DAYS)))


def needs_attention(flag) -> bool:
	return bool(int(flag or 0))


def scan_mode(status, sales_order_status=None) -> str | None:
	"""Which stream an order belongs to, or None when it is out of scope."""
	if is_active(status):
		return SCAN_ACTIVE
	if is_late_return_candidate(status, sales_order_status):
		return SCAN_LATE_RETURNS
	return None


def age_bucket(days) -> str:
	"""Payout ageing bucket for a number of days."""
	try:
		days = int(days)
	except (TypeError, ValueError):
		days = 0
	if days <= 7:
		return "0-7"
	if days <= 14:
		return "8-14"
	if days <= 30:
		return "15-30"
	return "30+"


AGE_BUCKETS = ("0-7", "8-14", "15-30", "30+")
