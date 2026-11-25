frappe.query_reports["PostEx Manual Booking Queue"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
		},
		{
			fieldname: "min_risk",
			label: __("Return risk at least (%)"),
			fieldtype: "Percent",
			default: 35,
			description: __("0 shows every order waiting for a booking, whatever its risk."),
		},
		{
			fieldname: "only_held",
			label: __("Only orders the risk gate held"),
			fieldtype: "Check",
			default: 0,
		},
	],
};
