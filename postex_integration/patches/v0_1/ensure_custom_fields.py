from postex_integration.fields import ensure_custom_fields


def execute():
	"""Create the custom fields the integration needs, if missing."""
	ensure_custom_fields()
