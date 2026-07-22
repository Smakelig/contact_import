# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
from odoo import api, fields, models


class ImportStagingRecord(models.Model):
    """One row per record seen from an external source, awaiting human
    review. Never auto-creates or auto-links anything in the target
    model (res.partner) - that only happens via the explicit
    action_link_to_candidate() / action_create_new() methods below,
    each a direct result of a person clicking a button on this record.
    """
    _name = "import.staging.record"
    _description = "A record staged for import review"
    _order = "create_date desc"

    profile_id = fields.Many2one("import.match.profile", required=True, index=True)
    batch_id = fields.Many2one("import.batch", required=True, ondelete="cascade")
    source_type = fields.Selection(related="batch_id.source_type", store=True)

    source_ref = fields.Char(
        help="Stable identifier from the source (CSV row hash, Google "
             "resource_name, vCard UID, etc.) - used to avoid re-staging "
             "the same external record on a repeat import.",
    )
    display_name = fields.Char(required=True)
    raw_data = fields.Text(help="Original imported row/payload, kept for reference.")

    # Generic import field storage: v1 only needs email/phone for the
    # Contacts profile's matching rules, stored as plain columns rather
    # than a fully dynamic key-value structure. If a future profile
    # needs different fields, add columns here rather than building a
    # generic EAV structure prematurely - the actual field set imported
    # sources care about doesn't change often enough to justify that
    # complexity yet.
    email = fields.Char()
    phone = fields.Char()
    function = fields.Char(string="Job Title", help="Maps to res.partner.function.")
    notes = fields.Text(
        help="Maps to res.partner.comment. Includes any source data that "
             "doesn't have a dedicated field on res.partner (e.g. Google's "
             "Birthday, Nickname, Organization Department, or a second "
             "email/phone - res.partner only supports one of each natively).",
    )
    tag_names = fields.Char(
        help="Comma or semicolon-separated tag names (e.g. from Google's "
             "'Labels' column). Matching/created res.partner.category "
             "records are applied when this row is linked or created.",
    )

    state = fields.Selection([
        ("new", "New"),
        ("reviewed", "Reviewed"),
        ("linked", "Linked to Existing"),
        ("created", "Created New"),
        ("ignored", "Ignored"),
    ], default="new", required=True, index=True)

    candidate_ids = fields.One2many("import.staging.candidate", "staging_id", string="Candidate Matches")
    best_confidence = fields.Float(compute="_compute_best_confidence", store=True)
    linked_record_ref = fields.Reference(
        selection=lambda self: self._selection_target_models(),
        readonly=True,
        help="Set once this row has been linked to or used to create a target record.",
    )

    def _selection_target_models(self):
        # Populated from whatever import.match.profile target models
        # actually exist, so this stays correct if/when a Products
        # profile is added later without any code change here.
        profiles = self.env["import.match.profile"].sudo().search([])
        return [(p.target_model_name, p.target_model_id.name) for p in profiles if p.target_model_name]

    @api.depends("candidate_ids.confidence")
    def _compute_best_confidence(self):
        for rec in self:
            rec.best_confidence = max(rec.candidate_ids.mapped("confidence"), default=0.0)

    def action_compute_candidates(self):
        """(Re-)runs the fuzzy matching engine for this row and replaces
        its candidate list. Safe to call repeatedly - read-only against
        the target model."""
        for rec in self:
            rec.candidate_ids.unlink()
            record_vals = {
                "email": rec.email,
                "phone": rec.phone,
                "mobile": rec.phone,  # same staged value checked against both target fields
                "name": rec.display_name,
            }
            results = rec.profile_id._find_candidates(record_vals)
            for r in results:
                self.env["import.staging.candidate"].create({
                    "staging_id": rec.id,
                    "matched_record_ref": "%s,%s" % (r["record"]._name, r["record"].id),
                    "confidence": r["confidence"],
                    "match_breakdown": r["breakdown"],
                })
            if rec.state == "new" and results:
                rec.state = "reviewed"

    def action_link_to_candidate(self, candidate_id):
        """Explicit human action: link this staged row to a chosen
        candidate match, without modifying the target record itself."""
        self.ensure_one()
        candidate = self.env["import.staging.candidate"].browse(candidate_id)
        if candidate.staging_id != self:
            return
        self.linked_record_ref = "%s,%s" % (
            candidate.matched_record_ref._name, candidate.matched_record_ref.id,
        )
        self.state = "linked"

    def _resolve_tags(self):
        """Splits tag_names on comma/semicolon and gets-or-creates matching
        res.partner.category records. Only meaningful when target model is
        res.partner (or any model with a category_id field) - silently
        does nothing otherwise."""
        self.ensure_one()
        if not self.tag_names:
            return self.env["res.partner.category"].browse()
        Category = self.env["res.partner.category"]
        names = [n.strip() for n in self.tag_names.replace(";", ",").split(",") if n.strip()]
        tags = Category.browse()
        for name in names:
            tag = Category.search([("name", "=", name)], limit=1)
            if not tag:
                tag = Category.create({"name": name})
            tags |= tag
        return tags

    def action_create_new(self):
        """Explicit human action: create a brand-new record in the
        target model from this staged row's data."""
        self.ensure_one()
        Target = self.env[self.profile_id.target_model_name]
        vals = {"name": self.display_name}
        if self.email and "email" in Target._fields:
            vals["email"] = self.email
        if self.phone and "phone" in Target._fields:
            vals["phone"] = self.phone
        if self.function and "function" in Target._fields:
            vals["function"] = self.function
        if self.notes and "comment" in Target._fields:
            vals["comment"] = self.notes
        if "category_id" in Target._fields:
            tags = self._resolve_tags()
            if tags:
                vals["category_id"] = [(6, 0, tags.ids)]
        new_record = Target.create(vals)
        self.linked_record_ref = "%s,%s" % (Target._name, new_record.id)
        self.state = "created"
        return new_record

    def action_ignore(self):
        self.write({"state": "ignored"})
