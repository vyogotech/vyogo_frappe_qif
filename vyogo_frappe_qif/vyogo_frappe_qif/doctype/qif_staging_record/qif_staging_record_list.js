// Copyright (c) 2026, Vyogo Technologies and contributors
// For license information, please see license.txt

frappe.listview_settings["QIF Staging Record"] = {
	hide_name_column: true,
	onload(listview) {
		listview.page.clear_primary_action();
	},
};
