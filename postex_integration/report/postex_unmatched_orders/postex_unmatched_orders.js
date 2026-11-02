frappe.query_reports["PostEx Unmatched Orders"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "postex_account",
			label: __("PostEx Account"),
			fieldtype: "Link",
			options: "PostEx Account",
			get_query: () => ({
				filters: { company: frappe.query_report.get_filter_value("company") },
			}),
		},
		{
			fieldname: "reason",
			label: __("Reason"),
			fieldtype: "Select",
			options: ["", "No reference", "Ref unknown", "No tracking", "Not yet booked"].join(
				"\n"
			),
		},
		{
			fieldname: "days",
			label: __("Older than (days)"),
			fieldtype: "Int",
			default: 0,
			description: __("Ignore orders younger than this. 0 shows everything."),
		},
	],
};
