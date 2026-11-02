frappe.query_reports["PostEx Payout Ageing"] = {
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
			fieldname: "bucket",
			label: __("Bucket"),
			fieldtype: "Select",
			options: ["", "0-7", "8-14", "15-30", "30+"].join("\n"),
		},
	],
};
