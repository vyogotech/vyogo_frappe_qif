// Copyright (c) 2026, Vyogo Technologies and contributors
// For license information, please see license.txt

function wrap_existing_handlers() {
    const list = frappe.ui.form.get_event_handler_list("Bank Statement Import", "import_file");
    list.forEach((fn, idx) => {
        if (fn && !fn._qif_wrapped) {
            const orig = fn;
            list[idx] = function (frm, doctype, name) {
                const file_url = (frm && frm.doc && frm.doc.import_file || "").toLowerCase();
                if (file_url.endsWith(".qif")) {
                    return;
                }
                return orig(frm, doctype, name);
            };
            list[idx]._qif_wrapped = true;
        }
    });
}

function set_qif_restrictions(frm) {
    const file_field = frm.get_field("import_file");
    if (file_field && file_field.df) {
        file_field.df.options = file_field.df.options || {};
        file_field.df.options.restrictions = file_field.df.options.restrictions || {};
        let allowed = file_field.df.options.restrictions.allowed_file_types || [".csv", ".xls", ".xlsx", ".TXT", ".txt"];
        if (!allowed.includes(".qif")) {
            allowed.push(".qif", ".QIF");
        }
        file_field.df.options.restrictions.allowed_file_types = allowed;
    }
}

// Wrap handlers on script load
wrap_existing_handlers();

frappe.ui.form.on("Bank Statement Import", {
    onload(frm) {
        wrap_existing_handlers();
        set_qif_restrictions(frm);
    },

    setup(frm) {
        wrap_existing_handlers();
        set_qif_restrictions(frm);

        // Intercept frm.events.import_file to prevent ERPNext's get_preview_from_template call
        if (frm.events && frm.events.import_file && !frm.events.import_file._qif_wrapped) {
            const orig_import_file = frm.events.import_file;
            frm.events.import_file = function (frm, ...args) {
                const file_url = (frm && frm.doc && frm.doc.import_file || "").toLowerCase();
                if (file_url.endsWith(".qif")) {
                    return frm.trigger("render_qif_preview");
                }
                return orig_import_file(frm, ...args);
            };
            frm.events.import_file._qif_wrapped = true;
        }
    },

    render_qif_preview(frm) {
        const file_url = (frm.doc.import_file || "").toLowerCase();
        if (!file_url.endsWith(".qif")) return;

        frm.toggle_display("section_import_preview", true);
        const wrapper = frm.get_field("import_preview").$wrapper;
        wrapper.empty();
        $(`
            <div class="alert alert-info" style="margin-top: 10px; display: flex; align-items: center; justify-content: space-between;">
                <div>
                    <strong>QIF Bank Statement Detected:</strong> <code>${frappe.utils.escape_html(frm.doc.import_file)}</code>
                    <div class="text-muted" style="margin-top: 4px;">
                        Managed by Vyogo QIF Importer (O(1) streaming parser with occurrence deduplication). Click <strong>"Start QIF Import"</strong> to process.
                    </div>
                </div>
            </div>
        `).appendTo(wrapper);
        frm.trigger("update_primary_action");
    },

    import_file(frm) {
        const file_url = (frm.doc.import_file || "").toLowerCase();
        if (file_url.endsWith(".qif")) {
            frm.trigger("render_qif_preview");
        }
    },

    refresh(frm) {
        wrap_existing_handlers();
        set_qif_restrictions(frm);

        const file_url = (frm.doc.import_file || "").toLowerCase();
        const is_qif = file_url.endsWith(".qif");

        if (is_qif) {
            frm.trigger("render_qif_preview");
        }

        if (is_qif && !frm.is_new()) {
            if (frm.doc.status === "Pending" || !frm.doc.status) {
                frm.page.set_primary_action(__("Start QIF Import"), () => {
                    frappe.call({
                        method: "vyogo_frappe_qif.api.import_qif",
                        args: {
                            bank_statement_import: frm.doc.name,
                        },
                        freeze: true,
                        freeze_message: __("Staging QIF statement..."),
                        callback(r) {
                            if (!r.exc && r.message) {
                                frappe.msgprint({
                                    title: __("QIF Import Started"),
                                    indicator: "green",
                                    message: __("Staged {0} records. Background worker has been queued.", [r.message.total_staged]),
                                });
                                frm.reload_doc();
                            }
                        },
                    });
                });
            }

            if (frm.doc.status === "Success" || frm.doc.status === "Partial Success") {
                frm.add_custom_button(__("Undo QIF Import"), () => {
                    frappe.confirm(
                        __("Are you sure you want to rollback this QIF import? Unreconciled Bank Transactions will be deleted."),
                        () => {
                            frappe.call({
                                method: "vyogo_frappe_qif.api.undo_import",
                                args: {
                                    import_session: frm.doc.name,
                                },
                                freeze: true,
                                freeze_message: __("Rolling back import..."),
                                callback(r) {
                                    if (!r.exc && r.message) {
                                        frappe.msgprint({
                                            title: __("Rollback Complete"),
                                            indicator: "blue",
                                            message: r.message.message,
                                        });
                                        frm.reload_doc();
                                    }
                                },
                            });
                        }
                    );
                }).addClass("btn-danger");
            }
        }
    },
});
