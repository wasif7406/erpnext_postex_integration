"""Thin PostEx HTTP client.

One instance per PostEx Account, so the token of company A can never be used
for company B. Every failure is raised as PostExAPIError and logged once.
"""

import frappe
import requests

from .constants import DEFAULT_BASE_URL
from .exceptions import PostExAPIError
from .utils import dump_snippet, log_error


class PostExClient:
	def __init__(self, account, timeout: int = 30):
		if isinstance(account, str):
			account = frappe.get_doc("PostEx Account", account)
		self.account = account
		self.name = account.name
		self.company = account.company
		self.base_url = (account.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
		self.timeout = timeout
		self._token = account.get_password("api_token", raise_exception=False)

	def _headers(self, json_body: bool = False) -> dict:
		headers = {"token": self._token or "", "Accept": "application/json"}
		if json_body:
			headers["Content-Type"] = "application/json"
		return headers

	def _ensure_token(self):
		if not self._token:
			raise PostExAPIError(
				f"PostEx Account {self.name} has no API token configured."
			)

	def _handle(self, response, path: str) -> dict:
		try:
			payload = response.json()
		except Exception:
			payload = {"statusCode": response.status_code, "raw": response.text[:500]}

		status_code = str((payload or {}).get("statusCode", response.status_code))
		if response.status_code >= 400 or status_code not in ("200", "201"):
			message = (
				f"PostEx {path} failed for account {self.name}: "
				f"HTTP {response.status_code} / statusCode {status_code} / {dump_snippet(payload, 300)}"
			)
			log_error("PostEx API error", message)
			raise PostExAPIError(message)
		return payload

	def get(self, path: str, params: dict | None = None) -> dict:
		self._ensure_token()
		try:
			response = requests.get(
				self.base_url + path,
				params=params,
				headers=self._headers(),
				timeout=self.timeout,
			)
		except Exception as exc:
			message = f"PostEx GET {path} failed for account {self.name}: {exc}"
			log_error("PostEx API error", message)
			raise PostExAPIError(message) from exc
		return self._handle(response, path)

	def get_dist(self, path: str, params: dict | None = None) -> dict:
		"""GET returning the `dist` object (or {} when absent)."""
		payload = self.get(path, params=params)
		dist = (payload or {}).get("dist")
		return dist if isinstance(dist, dict) else {}

	def post(self, path: str, payload: dict) -> dict:
		self._ensure_token()
		try:
			response = requests.post(
				self.base_url + path,
				json=payload,
				headers=self._headers(json_body=True),
				timeout=self.timeout,
			)
		except Exception as exc:
			message = f"PostEx POST {path} failed for account {self.name}: {exc}"
			log_error("PostEx API error", message)
			raise PostExAPIError(message) from exc
		return self._handle(response, path)

	# -- typed helpers -----------------------------------------------------
	def list_orders(self, start_date: str, end_date: str, order_status_id: int = 0) -> list:
		"""get-all-order: all orders in the window, normalised to a list of dicts."""
		payload = self.get(
			"/services/integration/api/order/v1/get-all-order",
			params={
				"orderStatusId": order_status_id,
				"startDate": start_date,
				"endDate": end_date,
			},
		)
		return _as_tracking_dicts((payload or {}).get("dist"))

	def list_unbooked_orders(self, start_date: str, end_date: str) -> list:
		payload = self.get(
			"/services/integration/api/order/v2/get-unbooked-orders",
			params={"startDate": start_date, "endDate": end_date},
		)
		return _as_tracking_dicts((payload or {}).get("dist"))

	def track_order(self, tracking_number: str) -> dict:
		payload = self.get("/services/integration/api/order/v1/track-order/" + tracking_number)
		return _as_tracking_dict((payload or {}).get("dist")) or {}

	def payment_status(self, tracking_number: str) -> dict:
		return self.get_dist("/services/integration/api/order/v1/payment-status/" + tracking_number)

	def add_order(self, payload: dict) -> dict:
		response = self.post("/services/integration/api/order/v1/add-order", payload)
		dist = (response or {}).get("dist")
		return dist if isinstance(dist, dict) else {}


def _as_tracking_dict(item):
	if isinstance(item, dict) and isinstance(item.get("trackingResponse"), dict):
		return item["trackingResponse"]
	return item if isinstance(item, dict) else None


def _as_tracking_dicts(items):
	out = []
	for item in items or []:
		item = _as_tracking_dict(item)
		if item and (item.get("orderRefNumber") or "").strip():
			out.append(item)
	return out
