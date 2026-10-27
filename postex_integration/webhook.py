"""Per-account webhook endpoint: /api/method/postex_integration.webhook.receive/<account>/<secret>

Guest-callable (PostEx cannot authenticate as an ERPNext user). Authorisation is
the per-account `webhook_secret`, compared in constant time. The webhook only
ever updates status/tracking on a Sales Order — it never creates invoices,
stock entries or accounting documents, so there is exactly one writer for those.
"""

import hmac

import frappe

from .constants import POSTEX_COURIER
from .utils import dump_snippet, log_error, map_status, now

LOG = "PostEx Webhook Log"
METHOD_PATH = "/api/method/postex_integration.webhook.receive"


@frappe.whitelist(allow_guest=True)
def receive(account=None, secret=None):
	account_arg, secret_arg = _resolve_args(account, secret)
	if not account_arg or not secret_arg:
		return _respond(False, "missing account or secret", 401)

	account_doc = _find_account(account_arg)
	if not account_doc:
		return _respond(False, "unknown account", 404)

	expected = account_doc.get_password("webhook_secret", raise_exception=False) or ""
	if not expected or not hmac.compare_digest(str(expected), str(secret_arg)):
		log_error(
			"PostEx webhook auth failed",
			f"account={account_doc.name} remote={getattr(frappe.local.request, 'remote_addr', '?')}",
		)
		return _respond(False, "invalid secret", 403)

	payload = {k: v for k, v in (frappe.form_dict or {}).items() if k != "cmd"}
	body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
	if isinstance(body.get("trackingResponse"), dict):
		body = body["trackingResponse"]

	tracking = _first_string(
		payload, body, ("trackingNumber", "tracking_number", "orderNumber", "order_number")
	)
	status_raw = _first_string(
		payload, body, ("transactionStatus", "orderStatus", "status", "transactionStatusMessage")
	)
	history = payload.get("transactionStatusHistory") or (
		body.get("transactionStatusHistory") if isinstance(body, dict) else None
	)

	mapped = map_status(status_raw, history) if (status_raw or history) else ""
	matched, result = _apply(account_doc, tracking, mapped)

	try:
		frappe.get_doc(
			{
				"doctype": LOG,
				"tracking_number": tracking or None,
				"order_status": status_raw or None,
				"matched_order": matched,
				"postex_account": account_doc.name,
				"company": account_doc.company,
				"result": result,
				"raw_payload": dump_snippet(payload, 60000),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		log_error("PostEx webhook log failed", frappe.get_traceback())

	return {
		"ok": True,
		"account": account_doc.name,
		"tracking": tracking,
		"status": mapped,
		"result": result,
	}


# ---------------------------------------------------------------------------
def _apply(account_doc, tracking, mapped):
	if not tracking:
		return None, "no tracking number found in payload"

	order = frappe.db.get_value(
		"Sales Order",
		{"custom_postex_account": account_doc.name, "custom_postex_tracking_number": tracking},
		"name",
	)
	if not order:
		# The order may predate the account stamp; only accept an unassigned
		# order of the same company, so one account can never overwrite another.
		order = frappe.db.get_value(
			"Sales Order",
			{
				"company": account_doc.company,
				"custom_postex_tracking_number": tracking,
				"custom_postex_account": ("is", "not set"),
			},
			"name",
		)
		if order:
			frappe.db.set_value("Sales Order", order, "custom_postex_account", account_doc.name)

	if not order:
		return None, f"no sales order with tracking {tracking} for account {account_doc.name}"

	if (frappe.db.get_value("Sales Order", order, "custom_courier") or "") != POSTEX_COURIER:
		frappe.db.set_value("Sales Order", order, "custom_courier", POSTEX_COURIER)

	old = frappe.db.get_value("Sales Order", order, "custom_postex_status")
	if mapped and old != mapped:
		frappe.db.set_value("Sales Order", order, "custom_postex_status", mapped)
		result = f"updated: {mapped}"
	else:
		result = f"no change: {old} (incoming: {mapped})"

	frappe.db.set_value(
		"Sales Order", order, "custom_postex_last_synced_on", now(), update_modified=False
	)
	frappe.db.commit()
	return order, result


def _find_account(value):
	value = (value or "").strip()
	if not value:
		return None
	if frappe.db.exists("PostEx Account", value):
		return frappe.get_doc("PostEx Account", value)
	name = frappe.db.get_value("PostEx Account", {"account_name": value}, "name")
	return frappe.get_doc("PostEx Account", name) if name else None


def _resolve_args(account, secret):
	"""Accept both query/form args and path segments after the method name."""
	account = account or frappe.form_dict.get("account")
	secret = secret or frappe.form_dict.get("secret")
	if account and secret:
		return account, secret

	path = ""
	try:
		path = frappe.local.request.path or ""
	except Exception:
		path = ""
	if METHOD_PATH in path:
		tail = path.split(METHOD_PATH, 1)[1].strip("/")
		parts = [part for part in tail.split("/") if part]
		if len(parts) >= 2:
			return parts[0], parts[1]
		if len(parts) == 1:
			return parts[0], secret
	return account, secret


def _first_string(payload, body, keys):
	for source in (payload, body):
		if not isinstance(source, dict):
			continue
		for key in keys:
			value = source.get(key)
			if isinstance(value, (str, int)) and str(value).strip():
				return str(value).strip()
	return ""


def _respond(ok, message, status):
	frappe.local.response["http_status_code"] = status
	return {"ok": ok, "error": message}
