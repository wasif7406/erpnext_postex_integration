"""Thin PostEx HTTP client.

One instance per PostEx Account, so the token of company A can never be used
for company B. Every failure is raised as PostExAPIError and logged once.

Every request is **paced** (`request_delay_seconds`, floor 0.25 s — PostEx's
published limit is ~1 request/second) and counted against the run's
`run_request_budget`. When the budget is exhausted `PostExBudgetExceeded` is
raised, which callers treat as "stop cleanly, the cursor keeps the rest for the
next run" — never as an order failure.
"""

import time

import frappe
import requests

from .constants import (
	DEFAULT_BASE_URL,
	DEFAULT_REQUEST_DELAY,
	DEFAULT_RUN_REQUEST_BUDGET,
	MIN_REQUEST_DELAY,
)
from .exceptions import PostExAPIError, PostExBudgetExceeded
from .utils import dump_snippet, log_error

# Transient failures (connection reset, timeout, 5xx) are retried; 4xx are not.
RETRIES = 2


class PostExClient:
	def __init__(
		self,
		account,
		timeout: int = 30,
		request_delay: float | None = None,
		request_budget: int | None = None,
	):
		if isinstance(account, str):
			account = frappe.get_doc("PostEx Account", account)
		self.account = account
		self.name = account.name
		self.company = account.company
		self.base_url = (account.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
		self.timeout = timeout
		self._token = account.get_password("api_token", raise_exception=False)

		delay = request_delay
		if delay is None:
			delay = account.get("request_delay_seconds")
		self.request_delay = max(MIN_REQUEST_DELAY, _flt(delay, DEFAULT_REQUEST_DELAY))

		budget = request_budget
		if budget is None:
			budget = account.get("run_request_budget")
		self.request_budget = _int(budget, DEFAULT_RUN_REQUEST_BUDGET)

		self.requests_made = 0
		self._last_request_at = None

	# -- pacing ------------------------------------------------------------
	@property
	def budget_left(self) -> int:
		if not self.request_budget:
			return -1
		return max(0, self.request_budget - self.requests_made)

	@property
	def budget_exhausted(self) -> bool:
		return bool(self.request_budget) and self.requests_made >= self.request_budget

	def _pace(self):
		"""Wait for the next request slot, or refuse to send one at all."""
		if self.budget_exhausted:
			raise PostExBudgetExceeded(
				f"PostEx request budget of {self.request_budget} exhausted for {self.name}; "
				"the remaining orders stay queued for the next run."
			)
		if self._last_request_at is not None and self.request_delay > 0:
			wait = self.request_delay - (time.monotonic() - self._last_request_at)
			if wait > 0:
				time.sleep(wait)

	def _spent(self):
		self.requests_made += 1
		self._last_request_at = time.monotonic()

	# -- plumbing ----------------------------------------------------------
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

	def _send(self, method: str, path: str, **kwargs) -> dict:
		"""Paced, budgeted, retried request. Raises PostExBudgetExceeded/APIError."""
		attempt = 0
		while True:
			self._pace()
			try:
				if method == "POST":
					response = requests.post(
						self.base_url + path,
						headers=self._headers(json_body=True),
						timeout=self.timeout,
						**kwargs,
					)
				else:
					response = requests.get(
						self.base_url + path,
						headers=self._headers(),
						timeout=self.timeout,
						**kwargs,
					)
			except Exception as exc:
				self._spent()
				if attempt < RETRIES:
					attempt += 1
					continue
				message = f"PostEx {method} {path} failed for account {self.name}: {exc}"
				log_error("PostEx API error", message)
				raise PostExAPIError(message) from exc

			self._spent()
			if response.status_code >= 500 and attempt < RETRIES:
				attempt += 1
				continue
			return self._handle(response, path)

	def get(self, path: str, params: dict | None = None) -> dict:
		self._ensure_token()
		return self._send("GET", path, params=params)

	def get_dist(self, path: str, params: dict | None = None) -> dict:
		"""GET returning the `dist` object (or {} when absent)."""
		payload = self.get(path, params=params)
		dist = (payload or {}).get("dist")
		return dist if isinstance(dist, dict) else {}

	def post(self, path: str, payload: dict) -> dict:
		self._ensure_token()
		return self._send("POST", path, json=payload)

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


def _flt(value, fallback: float) -> float:
	try:
		return float(value)
	except (TypeError, ValueError):
		return float(fallback)


def _int(value, fallback: int) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return int(fallback)
