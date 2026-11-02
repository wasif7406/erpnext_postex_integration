import frappe


class PostExError(frappe.ValidationError):
	"""Base error for the PostEx integration."""


class PostExConfigError(PostExError):
	"""The company has no usable PostEx Account, or its mapping is incomplete."""


class PostExAPIError(PostExError):
	"""An HTTP call to PostEx failed or returned a non-200 statusCode."""


class PostExBudgetExceeded(PostExAPIError):
	"""The run's request budget is spent: stop cleanly, resume next run.

	Deliberately *not* a per-order failure — the order was never really tried,
	so it must not count towards quarantine.
	"""


class PostExSkipFlow(Exception):
	"""Raised to abort a single document's flow without failing the whole run."""
