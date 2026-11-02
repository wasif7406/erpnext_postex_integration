"""P4 — one-off, idempotent backfill of `Sales Order.custom_postex_account`.

Settlement and every stock/revenue helper filter on `custom_postex_account`, so
an order without it is invisible to payout reconciliation. Orders booked before
the app existed (or by the old server scripts) carry a tracking number but no
account.

Rules:

* only submitted orders of the account's company,
* only when the courier is blank or `PostEx` (Quick Delivery / Fast And Quick
  are never touched),
* only when the order has no account yet (never reassign),
* **only when the company has exactly one enabled PostEx account** — with
  several accounts the attribution is genuinely ambiguous, so those rows are
  reported and left for the sync, which claims them from the account's own
  PostEx feed (the only reliable evidence).

Dry run by default; the caller must pass `dry_run=False` to write.
"""

import frappe

from .config import accounts_for_company, get_account
from .constants import POSTEX_COURIER
from .utils import log_error

CHUNK = 500
SAMPLE = 20


def assign_accounts(account: str | None = None, company: str | None = None, dry_run: bool = True) -> dict:
	"""Assign unassigned PostEx orders to their company's PostEx account.

	`account` pins one account; otherwise every enabled account is processed
	(one per company, since a company with several enabled accounts is skipped).
	"""
	report = {"dry_run": bool(dry_run), "accounts": {}, "total_assigned": 0}

	for account_doc in _target_accounts(account, company):
		try:
			report["accounts"][account_doc.name] = _assign_for_account(account_doc, dry_run)
			report["total_assigned"] += report["accounts"][account_doc.name]["assigned"]
		except Exception:
			log_error(
				"PostEx account backfill failed",
				f"{account_doc.name}\n{frappe.get_traceback()}",
			)
			report["accounts"][account_doc.name] = {"error": "exception"}

	if not dry_run:
		frappe.db.commit()
	return report


def _target_accounts(account, company):
	if account:
		return [get_account(account)]
	filters = {"enabled": 1}
	if company:
		filters["company"] = company
	rows = frappe.get_all(
		"PostEx Account", filters=filters, fields=["name", "company"], order_by="company asc"
	)
	return [get_account(row.name) for row in rows]


def _assign_for_account(account_doc, dry_run: bool) -> dict:
	company = account_doc.company
	siblings = accounts_for_company(company)
	out = {
		"company": company,
		"enabled_accounts_for_company": len(siblings),
		"submitted_orders": _count(company),
		"with_account": _count(company, "with"),
		"without_account": _count(company, "without"),
		"postex_candidates": 0,
		"assigned": 0,
		"ambiguous": 0,
		"ambiguous_orders": [],
		"other_courier": _count(company, "other_courier"),
		"skipped": None,
	}

	if len(siblings) > 1:
		out["skipped"] = (
			f"{company} has {len(siblings)} enabled PostEx accounts; attribution is ambiguous. "
			"The sync claims each order from the account's own PostEx feed instead."
		)
		out["ambiguous"] = _count(company, "without")
		out["ambiguous_orders"] = _names(company, SAMPLE)
		return out

	names = _names(company, 0)
	out["postex_candidates"] = len(names)
	if dry_run or not names:
		out["assigned"] = 0
		out["would_assign"] = len(names)
		out["sample"] = names[:SAMPLE]
		return out

	assigned = 0
	for start in range(0, len(names), CHUNK):
		chunk = names[start : start + CHUNK]
		frappe.db.sql(
			"""
			update `tabSales Order`
			set custom_postex_account = %s
			where name in %s
			  and (custom_postex_account is null or custom_postex_account = '')
			""",
			(account_doc.name, tuple(chunk)),
		)
		assigned += len(chunk)
		frappe.db.commit()

	out["assigned"] = assigned
	out["with_account_after"] = _count(company, "with")
	out["without_account_after"] = _count(company, "without")
	return out


# ---------------------------------------------------------------------------
def _where(company, mode) -> tuple[str, tuple]:
	"""SQL predicate for one company.

	`without` = submitted, no account yet, courier blank/PostEx.
	`with`    = submitted and already labelled.
	`other_courier` = submitted and booked on a different courier.
	`any`     = every submitted order of the company.
	"""
	if mode == "other_courier":
		return (
			"docstatus = 1 and company = %s and ifnull(custom_courier, '') not in ('', %s)",
			(company, POSTEX_COURIER),
		)
	clauses = ["docstatus = 1", "company = %s"]
	params = [company]
	if mode == "with":
		clauses.append("ifnull(custom_postex_account, '') != ''")
	elif mode != "any":
		clauses.append("ifnull(custom_postex_account, '') = ''")
		clauses.append("ifnull(custom_courier, '') in ('', %s)")
		params.append(POSTEX_COURIER)
	return " and ".join(clauses), tuple(params)


def _count(company, mode="any") -> int:
	where, params = _where(company, mode)
	rows = frappe.db.sql(f"select count(*) from `tabSales Order` where {where}", params)
	return int(rows[0][0]) if rows else 0


def _names(company, limit=0) -> list:
	where, params = _where(company, "without")
	sql = f"select name from `tabSales Order` where {where} order by name asc"
	if limit:
		sql += f" limit {int(limit)}"
	return [row[0] for row in frappe.db.sql(sql, params)]
