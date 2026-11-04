"""Stock and revenue flows for one account: pickup, delivery, invoice, return.

Policy (unchanged from the single-company implementation):
  * pickup     : Stores -> In-Transit warehouse (Material Transfer)
  * delivered  : In-Transit -> customer (Delivery Note), then Sales Invoice
  * returned   : In-Transit -> Stores (Material Transfer) + courier cost
  * a returned order has no invoice at all (delivered => invoice only)
  * terminal   : a parcel that will not be delivered is finished by
                 `close_terminals()` — stock back (or written off when lost),
                 courier cost, and the Sales Order closed. A return that arrives
                 *after* the invoice is reversed with a credit note, because the
                 revenue is already reported and the money may already be paid.

Every creator is guarded twice: the query only selects orders that are still
missing the document, and the loop re-checks the Sales Order before creating
anything (an invoice from any source, a fully delivered order, a quarantine
flag). Running the helpers twice in a row therefore creates nothing the second
time. Verify-after-create still protects against two concurrent runs.

**All selection predicates live in SQL**, including the quarantine and the
historical-import filters. `LIMIT` is applied by the database, so anything
skipped in Python still consumes one of the run's slots — and a quarantined
order's `modified` is never bumped, so it would keep the same slot on every run
until the whole helper stalled. `helper_backlog()` reports what is still waiting
(and what only a human can release) so a green sync with stuck documents is
visible instead of silent.
"""

import frappe
from frappe.utils import cint, flt, getdate, now_datetime, nowdate

from .config import AccountConfig
from .constants import (
	CLOSABLE_STATUSES,
	LOSS_STATUSES,
	MAX_DOCS_PER_HELPER,
	PAYMENT_NOT_DUE,
	PAYMENT_NOT_PAID,
	PICKUP_STATUSES,
	POSTEX_COURIER,
)
from .utils import (
	bump_failures,
	claim_field,
	clear_failures,
	exclude_historical_sql,
	exclude_quarantined_sql,
	existing_invoice,
	log_error,
	net_rate,
	submit,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _where_owned(allow_quarantined: bool = False) -> str:
	"""Scope shared by every helper query: this account's submitted orders.

	Everything that can be decided from the row is decided **in SQL** (quarantine,
	courier, historical imports), because `LIMIT` is applied by the database and a
	Python-side skip would consume one of the run's slots. `ifnull(custom_courier,
	'')` matters: an empty Select is stored as NULL and `IN ('', 'PostEx')` alone
	never matches NULL.

	`allow_quarantined` is only for the backlog counter, which has to see the
	blocked rows the helpers deliberately exclude.
	"""
	clauses = [
		"docstatus = 1",
		"company = %(company)s",
		"custom_postex_account = %(account)s",
		"ifnull(custom_courier, '') in ('', %(courier)s)",
	]
	if not allow_quarantined:
		clauses.append(exclude_quarantined_sql())
	return " and ".join(clauses)


def _waiting_orders(
	config: AccountConfig, where: str, params: dict, fields: str, limit: int
) -> list:
	"""Orders this account still owes a document, newest first.

	Every predicate is SQL so `LIMIT` only ever returns rows the loop can act on.
	"""
	historical = exclude_historical_sql()
	if historical:
		where = f"{where} and {historical}"

	rows = frappe.db.sql(
		f"""
		select {fields}
		from `tabSales Order`
		where {_where_owned()}
		  and ({where})
		order by modified desc
		limit %(limit)s
		""",
		{
			"company": config.company,
			"account": config.name,
			"courier": POSTEX_COURIER,
			"limit": int(limit),
			**params,
		},
		as_dict=True,
	)
	return [frappe._dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Backlog (what the helpers still have to do, and what a human must release)
# ---------------------------------------------------------------------------
# The four helper work sets, used by the helpers and by the backlog counter so
# the two can never drift apart.
PICKUP_WAITING = (
	"ifnull(custom_postex_pickup_entry, '') = ''"
	" and custom_postex_status in %(pickup_statuses)s"
	# Stock that already left through a delivery must never move twice.
	" and ifnull(per_delivered, 0) < 100"
)
DELIVERY_WAITING = (
	"custom_postex_status = 'Delivered'"
	" and ifnull(custom_postex_delivery_note, '') = ''"
	# already invoiced (by this app or by hand) => stock already moved
	" and ifnull(custom_postex_sales_invoice, '') = ''"
	" and ifnull(per_delivered, 0) < 100"
)
INVOICE_WAITING = (
	"custom_postex_status = 'Delivered'"
	" and ifnull(custom_postex_delivery_note, '') != ''"
	" and ifnull(custom_postex_sales_invoice, '') = ''"
)
RETURN_WAITING = (
	"custom_postex_status = 'Returned'"
	" and ifnull(custom_postex_delivery_note, '') = ''"
	" and ifnull(custom_postex_sales_invoice, '') = ''"
	" and ifnull(custom_postex_return_entry, '') = ''"
	# Only a parcel that actually left can come back. A return that was never
	# picked up needs no stock movement; without this the loop skipped it in
	# Python (no failure recorded) and it would sit in the return window forever.
	" and exists ("
	" select 1 from `tabStock Entry` se"
	" where se.name = `tabSales Order`.custom_postex_pickup_entry and se.docstatus = 1"
	" )"
)
TERMINAL_WAITING = (
	"custom_postex_status in %(closable)s"
	" and ifnull(custom_postex_closed_on, '') = ''"
	# A booking cancelled from this app is the merchant's own decision: the Sales
	# Order is theirs to cancel or ship with another courier.
	" and ifnull(custom_postex_booking_cancelled, 0) = 0"
	# Never close a sale that already produced documents — except a return, which
	# needs the credit note precisely because it was invoiced. A cancelled/lost
	# order with a delivery note is a data contradiction, not something to close.
	" and ("
	" custom_postex_status = 'Returned'"
	" or ("
	" ifnull(custom_postex_delivery_note, '') = ''"
	" and ifnull(custom_postex_sales_invoice, '') = ''"
	" and ifnull(per_delivered, 0) < 100"
	" )"
	" )"
)


def _backlog_specs() -> dict:
	"""The helper work sets, as {step: (WHERE clause, params)}."""
	return {
		"pickup": (PICKUP_WAITING, {"pickup_statuses": tuple(PICKUP_STATUSES)}),
		"delivery": (DELIVERY_WAITING, {}),
		"invoice": (INVOICE_WAITING, {}),
		"return": (RETURN_WAITING, {}),
		"terminal": (TERMINAL_WAITING, {"closable": tuple(CLOSABLE_STATUSES)}),
	}


def helper_backlog(config: AccountConfig) -> dict:
	"""Count the work still waiting, per step, split into ready and blocked.

	`ready` is what the next run will pick up. `blocked` is the same work on a
	quarantined order: the helpers exclude it, so only a human can release it by
	re-queueing. A sync that reports Success while `blocked` stays above zero and
	no document is being created is exactly the silent stall this counter exists
	to expose, so it is printed in the run summary and the daily digest.
	"""
	return {
		step: _count_backlog(config, where, params)
		for step, (where, params) in _backlog_specs().items()
	}


def _count_backlog(config: AccountConfig, where: str, params: dict) -> dict:
	historical = exclude_historical_sql()
	if historical:
		where = f"{where} and {historical}"

	rows = frappe.db.sql(
		f"""
		select
			sum(ifnull(custom_postex_needs_attention, 0) = 0) as ready,
			sum(ifnull(custom_postex_needs_attention, 0) = 1) as blocked
		from `tabSales Order`
		where {_where_owned(allow_quarantined=True)}
		  and ({where})
		""",
		{
			"company": config.company,
			"account": config.name,
			"courier": POSTEX_COURIER,
			**params,
		},
		as_dict=True,
	)
	row = rows[0] if rows else {}
	return {"ready": int(row.get("ready") or 0), "blocked": int(row.get("blocked") or 0)}


def _courier_is_ours(doc) -> bool:
	return (doc.get("custom_courier") or "") in ("", POSTEX_COURIER)


def _is_quarantined(doc) -> bool:
	return bool(cint(doc.get("custom_postex_needs_attention")))


def _skip_order(doc) -> bool:
	"""Re-check the two guards on the loaded document.

	Quarantine is *also* a SQL predicate (`exclude_quarantined_sql`): the query is
	the fix, this is the belt for a flag that changed while the run was in flight.
	The courier check can only happen here (an empty Select is stored as NULL).
	"""
	return _is_quarantined(doc) or not _courier_is_ours(doc)


def _pickup_submitted(order) -> bool:
	entry = order.get("custom_postex_pickup_entry")
	if not entry:
		return False
	return frappe.db.get_value("Stock Entry", entry, "docstatus") == 1


def _fully_delivered(so) -> bool:
	return flt(so.get("per_delivered")) >= 100


def _pending_rows(so):
	rows = []
	for item in so.items:
		qty = flt(item.qty) - flt(item.delivered_qty)
		if qty > 0:
			rows.append((item, qty))
	return rows


def _delivery_posting_date(so):
	"""Posting date for the delivery note (and the invoice that bills it).

	Revenue belongs on the day the parcel was delivered, not the day the sync
	found out. The courier's date can only be used when it is safe:

	* never in the future (clock/timezone),
	* never before the pickup transfer — the transit warehouse would have no
	  stock on that date and ERPNext would refuse the delivery note,
	* never before the Sales Order itself,
	* never outside an existing Fiscal Year (a year rollover would hard-fail).

	When the delivery date is unknown, today is used (the old behaviour).
	"""
	today = getdate(nowdate())
	posting = today
	if so.get("custom_postex_delivered_on"):
		posting = min(getdate(so.get("custom_postex_delivered_on")), today)

	pickup = so.get("custom_postex_pickup_entry")
	if pickup:
		transfer_date = frappe.db.get_value("Stock Entry", pickup, "posting_date")
		if transfer_date and getdate(transfer_date) > posting:
			posting = getdate(transfer_date)

	if so.get("transaction_date") and getdate(so.get("transaction_date")) > posting:
		posting = getdate(so.get("transaction_date"))

	# a pre-ordered (future) Sales Order date, or a clock-skewed transfer, must
	# never produce a document dated in the future
	if posting > today:
		posting = today

	# Never post into a period the site has no Fiscal Year for: ERPNext would
	# refuse the document and the order would quarantine on a date rollover.
	if not _fiscal_year_covers(posting):
		return today
	return posting


def _fiscal_year_covers(date) -> bool:
	"""True when a Fiscal Year contains `date`.

	Read straight from the table on purpose: `get_fiscal_year` lives in
	`erpnext.accounts.utils` (not `frappe.utils`) and importing it there broke
	every sync run on the deployed bench.
	"""
	try:
		return bool(
			frappe.db.exists(
				"Fiscal Year",
				{"year_start_date": ("<=", date), "year_end_date": (">=", date)},
			)
		)
	except Exception:
		# Never block a delivery because the guard itself failed.
		return True


def _existing_invoice(sales_order):
	"""Any submitted-or-draft sales invoice (not a credit note) for this order.

	Shared with the booking-cancel guard: see `utils.existing_invoice`.
	"""
	return existing_invoice(sales_order)


# Child-table plumbing that must never be copied between documents: Frappe
# manages it on `append`, and a copied `name`/`parent` would corrupt the row.
TAX_ROW_SKIP = {
	"name",
	"owner",
	"creation",
	"modified",
	"modified_by",
	"docstatus",
	"idx",
	"parent",
	"parentfield",
	"parenttype",
	"doctype",
}

# Document-level discount, applied after taxes. Copied only onto doctypes that
# actually have the fields (the Delivery Note does not, the Sales Invoice does).
DISCOUNT_FIELDS = (
	"apply_discount_on",
	"additional_discount_percentage",
	"discount_amount",
)


def _copy_charges(source, target):
	"""Copy the Sales Order tax/discount block onto its Delivery Note / invoice.

	Every field is copied, **`tax_amount` included**: an `Actual` row keeps its
	amount there (`rate` is 0), so the old explicit field list silently dropped
	the charge — and the total check that compared grand totals then refused
	the invoice forever (one non-zero tax row was enough).
	"""
	meta = frappe.get_meta("Sales Taxes and Charges")
	for row in source.get("taxes") or []:
		values = {}
		for df in meta.fields:
			if df.fieldname in TAX_ROW_SKIP:
				continue
			values[df.fieldname] = row.get(df.fieldname)
		target.append("taxes", values)

	target_meta = frappe.get_meta(target.doctype)
	for fieldname in DISCOUNT_FIELDS:
		if target_meta.has_field(fieldname):
			target.set(fieldname, source.get(fieldname))


def _fail(result, sales_order, step, message, reference=None):
	"""Record a failure and make it count towards quarantine."""
	bump_failures(sales_order, step=step, error=message)
	result["failed"].append(
		{
			"sales_order": sales_order,
			"step": step,
			"reference": reference,
			"error": (message or "")[:500],
		}
	)


def _claim_field(doctype, name, fieldname, value) -> bool:
	"""Atomically claim a link field for our document (see `utils.claim_field`).

	Only an empty field accepts the value, so when two runs create the same
	document only one keeps it; the loser is cancelled by the caller with its
	stock/GL effect reversed.
	"""
	return claim_field(doctype, name, fieldname, value)


def _cancel_and_drop(doc):
	try:
		doc.flags.ignore_permissions = True
		doc.cancel()
	except Exception:
		pass
	try:
		frappe.delete_doc(doc.doctype, doc.name, force=True, ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		log_error(
			"PostEx: could not remove duplicate document",
			f"{doc.doctype} {doc.name}\n{frappe.get_traceback()}",
		)


# ---------------------------------------------------------------------------
# 1. Pickup transfer
# ---------------------------------------------------------------------------
def pickup_transfer(config: AccountConfig, limit: int = MAX_DOCS_PER_HELPER) -> dict:
	"""Move stock from the order's warehouse into the account's transit warehouse."""
	result = {"created": [], "skipped": 0, "failed": []}

	orders = _waiting_orders(
		config,
		PICKUP_WAITING,
		{"pickup_statuses": tuple(PICKUP_STATUSES)},
		"name, set_warehouse, per_delivered",
		limit,
	)

	for order in orders:
		try:
			if flt(order.get("per_delivered")) >= 100:
				# Stock already left through a delivery: never move it twice.
				result["skipped"] += 1
				continue
			so = frappe.get_doc("Sales Order", order.name)
			if _skip_order(so):
				result["skipped"] += 1
				continue
			rows = []
			for item, qty in _pending_rows(so):
				source = (
					(item.warehouse or "").strip()
					or (so.set_warehouse or "").strip()
					or config.stores_warehouse
				)
				rows.append(
					{
						"item_code": item.item_code,
						"item_name": item.item_name,
						"description": item.description,
						"qty": qty,
						"uom": item.stock_uom,
						"stock_uom": item.stock_uom,
						"conversion_factor": 1,
						"s_warehouse": source,
						"t_warehouse": config.in_transit_warehouse,
					}
				)
			if not rows:
				result["skipped"] += 1
				continue

			se = frappe.get_doc(
				{
					"doctype": "Stock Entry",
					"stock_entry_type": "Material Transfer",
					"purpose": "Material Transfer",
					"company": so.company,
					"posting_date": nowdate(),
					"remark": f"PostEx pickup {so.name} / {so.get('shopify_order_number') or ''} "
					f"[{config.name}]",
					"items": rows,
				}
			)
			submit(se)

			if not _claim_field("Sales Order", so.name, "custom_postex_pickup_entry", se.name):
				_cancel_and_drop(se)
				_fail(
					result,
					so.name,
					"pickup",
					"duplicate pickup transfer prevented",
					so.get("custom_postex_tracking_number"),
				)
				continue

			clear_failures(so.name)
			result["created"].append(f"{so.name}={se.name}")
			frappe.db.commit()
		except Exception as exc:
			log_error("PostEx pickup transfer failed", f"{order.name}: {exc}", "Sales Order", order.name)
			_fail(result, order.name, "pickup", str(exc)[:120])

	frappe.db.commit()
	return result


# ---------------------------------------------------------------------------
# 2. Delivery note + invoice
# ---------------------------------------------------------------------------
def delivery_and_return(config: AccountConfig, limit: int = MAX_DOCS_PER_HELPER) -> dict:
	result = {
		"delivery_notes": [],
		"invoices": [],
		"linked_invoices": [],
		"returns": [],
		"return_charges": [],
		# terminal outcomes (see `close_terminals`)
		"closed": [],
		"credit_notes": [],
		"write_offs": [],
		"terminal_charges": [],
		"failed": [],
	}
	_create_delivery_notes(config, result, limit)
	_create_invoices(config, result, limit)
	_process_returns(config, result, limit)
	close_terminals(config, result, limit)
	return result


def _create_delivery_notes(config, result, limit):
	orders = _waiting_orders(
		config,
		DELIVERY_WAITING,
		{},
		"name, custom_postex_pickup_entry, per_delivered, custom_postex_tracking_number",
		limit,
	)

	for order in orders:
		if not _pickup_submitted(order):
			_fail(
				result,
				order.name,
				"delivery",
				"no submitted pickup entry",
				order.get("custom_postex_tracking_number"),
			)
			continue
		if flt(order.get("per_delivered")) >= 100:
			result["skipped"] = result.get("skipped", 0) + 1
			continue
		try:
			so = frappe.get_doc("Sales Order", order.name)
			if _skip_order(so):
				continue
			if so.get("custom_postex_delivery_note") or so.get("custom_postex_sales_invoice"):
				# another run (or a human) already produced the chain
				continue
			existing_dn_invoice = _existing_invoice(so.name)
			if existing_dn_invoice:
				_fail(
					result,
					so.name,
					"delivery",
					f"invoice {existing_dn_invoice} already bills this order",
					so.get("custom_postex_tracking_number"),
				)
				continue
			dn = frappe.get_doc(
				{
					"doctype": "Delivery Note",
					"customer": so.customer,
					"customer_name": so.customer_name,
					"company": so.company,
					"posting_date": _delivery_posting_date(so),
					"ignore_pricing_rule": 1,
					"instructions": f"PostEx delivered {so.name} / {so.get('shopify_order_number') or ''} "
					f"[{config.name}]",
				}
			)
			for item, qty in _pending_rows(so):
				dn.append(
					"items",
					{
						"item_code": item.item_code,
						"item_name": item.item_name,
						"description": item.description,
						"qty": qty,
						"uom": item.uom,
						"stock_uom": item.stock_uom,
						"conversion_factor": 1,
						# net rate, so a discounted or zero-priced order keeps its amount
						"rate": net_rate(item.get("net_amount") or item.amount, qty),
						"price_list_rate": net_rate(item.get("net_amount") or item.amount, qty),
						"warehouse": config.in_transit_warehouse,
						"against_sales_order": so.name,
						"so_detail": item.name,
						"cost_center": item.cost_center or config.cost_center,
					},
				)
			if not dn.items:
				_fail(
					result,
					so.name,
					"delivery",
					"nothing left to deliver",
					so.get("custom_postex_tracking_number"),
				)
				continue

			# The delivery note carries the same tax/discount block as the order,
			# so the document pair never disagrees about the amount billed.
			_copy_charges(so, dn)
			submit(dn)
			if not _claim_field("Sales Order", so.name, "custom_postex_delivery_note", dn.name):
				_cancel_and_drop(dn)
				_fail(
					result,
					so.name,
					"delivery",
					"duplicate delivery note prevented",
					so.get("custom_postex_tracking_number"),
				)
				continue

			clear_failures(so.name)
			result["delivery_notes"].append(f"{so.name}={dn.name}")
			frappe.db.commit()
		except Exception as exc:
			log_error("PostEx delivery note failed", f"{order.name}: {exc}", "Sales Order", order.name)
			_fail(result, order.name, "delivery", str(exc)[:120])


def _create_invoices(config, result, limit):
	orders = _waiting_orders(
		config,
		INVOICE_WAITING,
		{},
		"name, custom_postex_delivery_note, custom_postex_tracking_number",
		limit,
	)

	for order in orders:
		try:
			so = frappe.get_doc("Sales Order", order.name)
			if _skip_order(so):
				continue
			existing = _existing_invoice(so.name)
			if existing:
				# Never a second invoice for the same delivery, whatever created
				# the first one. Record the link instead of failing: it repairs
				# the Sales Order and lets settlement see the invoice.
				if _claim_field("Sales Order", so.name, "custom_postex_sales_invoice", existing):
					clear_failures(so.name)
					result.setdefault("linked_invoices", []).append(f"{so.name}={existing}")
					frappe.db.commit()
				continue
			dn = frappe.get_doc("Delivery Note", order.custom_postex_delivery_note)
			if dn.docstatus != 1:
				_fail(
					result,
					order.name,
					"invoice",
					"delivery note not submitted",
					order.get("custom_postex_tracking_number"),
				)
				continue

			# A zero-value invoice has nothing for the courier to remit.
			pay_status = (
				PAYMENT_NOT_DUE if flt(dn.grand_total) <= 0 else PAYMENT_NOT_PAID
			)
			si = frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"customer": so.customer,
					"customer_name": so.customer_name,
					"company": so.company,
					# the invoice is dated on the delivery it bills, so revenue and
					# the delivery note always land in the same period
					"posting_date": dn.posting_date or nowdate(),
					"currency": so.currency,
					# No price list: the invoice must inherit the delivery note's
					# amounts, never re-price a zero/discounted line.
					"ignore_pricing_rule": 1,
					"custom_courier": POSTEX_COURIER,
					"custom_postex_account": config.name,
					"custom_postex_tracking_number": so.get("custom_postex_tracking_number"),
					"custom_postex_status": "Delivered",
					"custom_courier_payment_status": pay_status,
					"custom_postex_delivery_fee": flt(so.get("custom_postex_delivery_fee")),
					"custom_postex_delivery_tax": flt(so.get("custom_postex_delivery_tax")),
					"remarks": f"COD - PostEx {so.get('shopify_order_number') or so.name} delivered, "
					"cash collected by courier",
				}
			)
			# DeliveryNoteItem has no income_account; the account lives on the
			# Sales Order item, which is the correct source anyway.
			income_by_so_item = {item.name: item.get("income_account") for item in so.items}
			for dn_item in dn.items:
				qty = flt(dn_item.qty)
				amount = dn_item.get("net_amount") or dn_item.amount
				rate = net_rate(amount, qty)
				si.append(
					"items",
					{
						"item_code": dn_item.item_code,
						"item_name": dn_item.item_name,
						"description": dn_item.description,
						"qty": qty,
						"rate": rate,
						"price_list_rate": rate,
						"discount_percentage": 0,
						"discount_amount": 0,
						"uom": dn_item.uom,
						"stock_uom": dn_item.stock_uom,
						"conversion_factor": dn_item.conversion_factor,
						"warehouse": config.in_transit_warehouse,
						"delivery_note": dn.name,
						"dn_detail": dn_item.name,
						"sales_order": so.name,
						"so_detail": dn_item.so_detail,
						"cost_center": dn_item.cost_center or config.cost_center,
						"income_account": income_by_so_item.get(dn_item.so_detail)
						or dn_item.get("income_account"),
					},
				)
			if not si.items:
				_fail(result, order.name, "invoice", "delivery note has no items")
				continue
			_copy_charges(so, si)

			si.flags.ignore_permissions = True
			si.insert(ignore_permissions=True)

			# Last line of defence: never post an invoice that disagrees with the
			# delivery it bills. Both documents now carry the same tax/discount
			# block, so comparing the grand totals is safe — it used to fail for
			# every Sales Order with a non-zero tax, because only the invoice got
			# the tax rows (and an `Actual` row lost its amount entirely).
			#
			# The item total is checked first so a re-priced or discounted line
			# produces a precise error message.
			if abs(flt(si.net_total) - flt(dn.net_total)) > 0.01:
				si.delete()
				raise ValueError(
					f"invoice items total {si.net_total} differs from the delivery note "
					f"{dn.net_total}; not posted"
				)
			if abs(flt(si.grand_total) - flt(dn.grand_total)) > 0.01:
				si.delete()
				raise ValueError(
					f"invoice total {si.grand_total} differs from the delivery note "
					f"{dn.grand_total}; not posted"
				)

			si.submit()
			if not _claim_field("Sales Order", so.name, "custom_postex_sales_invoice", si.name):
				_cancel_and_drop(si)
				_fail(
					result,
					so.name,
					"invoice",
					"duplicate invoice prevented",
					so.get("custom_postex_tracking_number"),
				)
				continue

			clear_failures(so.name)
			result["invoices"].append(f"{so.name}={si.name}")
			frappe.db.commit()
		except Exception as exc:
			log_error("PostEx sales invoice failed", f"{order.name}: {exc}", "Sales Order", order.name)
			_fail(result, order.name, "invoice", str(exc)[:120])


# ---------------------------------------------------------------------------
# 3. Return leg
# ---------------------------------------------------------------------------
def _process_returns(config, result, limit):
	orders = _waiting_orders(
		config,
		RETURN_WAITING,
		{},
		"name, custom_postex_pickup_entry, custom_postex_tracking_number,"
		" custom_postex_reversal_fee, custom_postex_reversal_tax",
		limit,
	)

	for order in orders:
		if not _pickup_submitted(order):
			continue
		try:
			so = frappe.get_doc("Sales Order", order.name)
			if _skip_order(so):
				continue
			_book_return_charges(config, so, result)
			_move_return_stock(config, so, result)
			clear_failures(so.name)
			frappe.db.commit()
		except Exception as exc:
			log_error("PostEx return transfer failed", f"{order.name}: {exc}", "Sales Order", order.name)
			_fail(result, order.name, "return", str(exc)[:120], order.get("custom_postex_tracking_number"))


def _book_return_charges(config, so, result):
	"""A returned parcel cost the delivery attempt plus the reverse leg.

	No invoice exists (returned => no invoice), so the wallet takes the hit.
	"""
	total = flt(so.get("custom_postex_delivery_fee")) + flt(so.get("custom_postex_delivery_tax")) + flt(
		so.get("custom_postex_reversal_fee")
	) + flt(so.get("custom_postex_reversal_tax"))
	_book_courier_cost(
		config,
		so,
		total,
		"custom_postex_return_charges_entry",
		"return_charges",
		"PostEx return charges (delivery + reversal)",
		"return",
		result,
	)


def _book_courier_cost(config, so, total, guard_field, bucket, label, step, result):
	"""Dr Delivery Charges / Cr courier wallet for a parcel that produced no sale."""
	if total <= 0 or so.get(guard_field):
		return

	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": "Journal Entry",
			"company": so.company,
			"posting_date": nowdate(),
			"user_remark": f"{label} {so.get('shopify_order_number') or so.name} "
			f"[{config.name}]",
			"accounts": [
				{
					"account": config.delivery_charges,
					"debit_in_account_currency": total,
					"cost_center": config.cost_center,
				},
				{
					"account": config.finance,
					"credit_in_account_currency": total,
					"cost_center": config.cost_center,
				},
			],
		}
	)
	submit(je)
	if not _claim_field("Sales Order", so.name, guard_field, je.name):
		_cancel_and_drop(je)
		_fail(
			result,
			so.name,
			step,
			"duplicate courier cost prevented",
			so.get("custom_postex_tracking_number"),
		)
		return
	result[bucket].append(je.name)


def _move_return_stock(config, so, result):
	rows = []
	for item, qty in _pending_rows(so):
		rows.append(
			{
				"item_code": item.item_code,
				"item_name": item.item_name,
				"description": item.description,
				"qty": qty,
				"uom": item.stock_uom,
				"stock_uom": item.stock_uom,
				"conversion_factor": 1,
				"s_warehouse": config.in_transit_warehouse,
				"t_warehouse": config.stores_warehouse,
			}
		)
	if not rows:
		return

	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Transfer",
			"purpose": "Material Transfer",
			"company": so.company,
			"posting_date": nowdate(),
			"remark": f"PostEx return {so.name} / {so.get('shopify_order_number') or ''} [{config.name}]",
			"items": rows,
		}
	)
	submit(se)
	if not _claim_field("Sales Order", so.name, "custom_postex_return_entry", se.name):
		_cancel_and_drop(se)
		_fail(
			result,
			so.name,
			"return",
			"duplicate return transfer prevented",
			so.get("custom_postex_tracking_number"),
		)
		return
	result["returns"].append(f"{so.name}={se.name}")


# ---------------------------------------------------------------------------
# 4. Terminal outcomes
# ---------------------------------------------------------------------------
def close_terminals(config: AccountConfig, result: dict, limit: int = MAX_DOCS_PER_HELPER) -> dict:
	"""Finish every order whose parcel will not be delivered.

	This is the missing half of the state machine: without it a returned,
	cancelled, expired, lost, damaged or stolen parcel leaves its stock in the
	transit warehouse and its Sales Order open forever.

	| courier status            | stock                    | cost            |
	|---------------------------|--------------------------|-----------------|
	| Returned (not invoiced)   | In-Transit -> Stores     | delivery + reversal |
	| Returned (already invoiced)| **credit note** (+stock back) | delivery + reversal |
	| Lost / Damage / Stolen    | write-off (Material Issue)| delivery attempt |
	| Cancelled / Expired / Un-Assigned | In-Transit -> Stores | if PostEx billed it |

	Every branch ends by closing the Sales Order. The sync reopens a closed
	order if the parcel revives (e.g. a cancelled booking that is re-booked).
	"""
	orders = _waiting_orders(
		config,
		TERMINAL_WAITING,
		{"closable": tuple(CLOSABLE_STATUSES)},
		"name, custom_postex_status, custom_postex_tracking_number,"
		" custom_postex_sales_invoice, custom_postex_pickup_entry",
		limit,
	)
	for order in orders:
		try:
			so = frappe.get_doc("Sales Order", order.name)
			if _skip_order(so):
				continue
			_handle_terminal(config, so, result)
			clear_failures(so.name)
			frappe.db.commit()
		except Exception as exc:
			log_error(
				"PostEx terminal handling failed",
				f"{order.name}: {exc}\n{frappe.get_traceback()}",
				"Sales Order",
				order.name,
			)
			_fail(result, order.name, "terminal", str(exc)[:120], order.get("custom_postex_tracking_number"))
	return result


def _handle_terminal(config: AccountConfig, so, result):
	status = (so.get("custom_postex_status") or "").strip()

	if status == "Returned":
		invoice = _billed_invoice(so)
		if invoice:
			return _late_return(config, so, invoice, result)
		# The stock leg is normally done by `_process_returns`; do it here when
		# that helper has not run yet (e.g. more returns than one page).
		_ensure_return_stock(config, so, result)
		_book_return_charges(config, so, result)
		return _close_sales_order(so, "Returned", result)

	if status in LOSS_STATUSES:
		_write_off_stock(config, so, result)
		_book_terminal_charges(config, so, result)
		return _close_sales_order(so, status, result)

	# Cancelled / Expired / Un-Assigned By Me: nothing was delivered, so bring
	# back whatever left the store and finish the cost.
	_ensure_return_stock(config, so, result)
	_book_terminal_charges(config, so, result)
	return _close_sales_order(so, status, result)


def _billed_invoice(so):
	"""The submitted (non-return) invoice of this order, or None.

	An invoice created outside the app still counts: the order has reported
	revenue and a return must reverse it. A **draft** invoice is an anomaly that
	cannot be reversed automatically, so it is reported instead of guessing.
	"""
	name = so.get("custom_postex_sales_invoice") or existing_invoice(so.name)
	if not name:
		return None
	docstatus = frappe.db.get_value("Sales Invoice", name, "docstatus")
	if docstatus == 1:
		return frappe.get_doc("Sales Invoice", name)
	if docstatus == 0:
		raise ValueError(
			f"invoice {name} is still a draft: submit or delete it before the return can be reversed"
		)
	return None


def _ensure_return_stock(config: AccountConfig, so, result):
	"""Move the in-transit stock back to Stores, if it ever left."""
	if so.get("custom_postex_return_entry") or not _pickup_submitted(so):
		return
	_move_return_stock(config, so, result)


def _book_terminal_charges(config: AccountConfig, so, result):
	total = flt(so.get("custom_postex_delivery_fee")) + flt(so.get("custom_postex_delivery_tax")) + flt(
		so.get("custom_postex_reversal_fee")
	) + flt(so.get("custom_postex_reversal_tax"))
	_book_courier_cost(
		config,
		so,
		total,
		"custom_postex_terminal_entry",
		"terminal_charges",
		"PostEx terminal charges",
		"terminal",
		result,
	)


def _write_off_stock(config: AccountConfig, so, result):
	"""Lost/damaged/stolen: the units in transit are gone, not sold and not back."""
	if so.get("custom_postex_loss_entry") or not _pickup_submitted(so):
		return

	rows = []
	for item, qty in _pending_rows(so):
		row = {
			"item_code": item.item_code,
			"item_name": item.item_name,
			"description": item.description,
			"qty": qty,
			"uom": item.stock_uom,
			"stock_uom": item.stock_uom,
			"conversion_factor": 1,
			"s_warehouse": config.in_transit_warehouse,
		}
		# Optional mapping: without it ERPNext falls back to the company's stock
		# adjustment account, which is exactly what this write-off is.
		if config.stock_loss:
			row["expense_account"] = config.stock_loss
		if config.cost_center:
			row["cost_center"] = config.cost_center
		rows.append(row)
	if not rows:
		return

	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Issue",
			"purpose": "Material Issue",
			"company": so.company,
			"posting_date": nowdate(),
			"remark": f"PostEx loss {so.name} / {so.get('shopify_order_number') or ''} "
			f"({so.get('custom_postex_status')}) [{config.name}]",
			"items": rows,
		}
	)
	submit(se)
	if not _claim_field("Sales Order", so.name, "custom_postex_loss_entry", se.name):
		_cancel_and_drop(se)
		_fail(
			result,
			so.name,
			"terminal",
			"duplicate stock write-off prevented",
			so.get("custom_postex_tracking_number"),
		)
		return
	result["write_offs"].append(f"{so.name}={se.name}")


def _late_return(config: AccountConfig, so, invoice, result):
	"""The parcel came back **after** it was invoiced: reverse the sale.

	The order already reported revenue (and possibly COGS), so the only
	non-destructive correction is a credit note with the stock returned to
	Stores. A credit note that already exists for this invoice (even one created
	by hand) is respected.
	"""
	note = _existing_credit_note(invoice.name)
	if not note:
		note = _create_credit_note(config, so, invoice, result)
		if not note:
			return

	_book_return_charges(config, so, result)
	frappe.db.set_value(
		"Sales Invoice",
		invoice.name,
		{
			"custom_postex_status": "Returned",
			# no courier money is due on a returned sale; the CN carries the refund
			"custom_courier_payment_status": PAYMENT_NOT_DUE,
		},
		update_modified=False,
	)
	_close_sales_order(so, "Returned after invoice", result)


def _existing_credit_note(invoice):
	return frappe.db.get_value(
		"Sales Invoice",
		{"return_against": invoice, "is_return": 1, "docstatus": ("!=", 2)},
		"name",
	)


def _create_credit_note(config: AccountConfig, so, invoice, result):
	"""Build the return with ERPNext's own helper, so taxes/stock reverse correctly."""
	try:
		from erpnext.controllers.sales_and_purchase_return import make_return_doc

		cn = make_return_doc("Sales Invoice", invoice.name)
	except Exception as exc:
		raise ValueError(f"could not build the credit note for {invoice.name}: {exc}")

	cn.update_stock = 1
	for item in cn.items:
		# stock comes back to the store, not to the transit warehouse
		item.warehouse = config.stores_warehouse
		item.cost_center = item.cost_center or config.cost_center
		# link the row to the invoice it reverses (ERPNext warns otherwise) and
		# drop the delivery-note links: this return is not a DN reversal
		item.sales_invoice = invoice.name
		item.delivery_note = None
		item.dn_detail = None

	cn.custom_courier = POSTEX_COURIER
	cn.custom_postex_account = config.name
	cn.custom_postex_tracking_number = so.get("custom_postex_tracking_number")
	cn.custom_postex_status = "Returned"
	cn.custom_courier_payment_status = PAYMENT_NOT_DUE
	cn.remarks = (
		f"COD - PostEx late return {so.get('shopify_order_number') or so.name}: "
		"parcel returned after delivery, stock back to stores"
	)
	cn.flags.ignore_permissions = True
	cn.insert(ignore_permissions=True)
	cn.submit()
	result["credit_notes"].append(f"{so.name}={cn.name}")
	return cn.name


def _close_sales_order(so, reason, result):
	"""Mark the order terminal. The sync reopens it if the parcel revives."""
	if (so.get("status") or "") == "Closed" and so.get("custom_postex_closed_on"):
		return
	frappe.db.set_value(
		"Sales Order",
		so.name,
		{"status": "Closed", "custom_postex_closed_on": now_datetime()},
		update_modified=False,
	)
	so.add_comment(text=f"Sales Order closed by the PostEx integration: courier status {reason}.")
	result["closed"].append(f"{so.name}={reason}")

