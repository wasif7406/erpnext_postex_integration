import frappe


class PostExError(frappe.ValidationError):
	"""Base error for the PostEx integration."""


class PostExConfigError(PostExError):
	"""The company has no usable PostEx Account, or its mapping is incomplete."""


class PostExAPIError(PostExError):
	"""An HTTP call to PostEx failed or returned a non-200 statusCode."""


class PostExSkipFlow(Exception):
	"""Raised to abort a single document's flow without failing the whole run."""
