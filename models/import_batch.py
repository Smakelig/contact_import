# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
from odoo import fields, models


class ImportBatch(models.Model):
    """One row per import run - e.g. 'contacts.csv uploaded 2026-07-22'.
    Lets you see import runs at a glance rather than a flat, endless
    list of every staged record ever seen."""
    _name = "import.batch"
    _description = "One import run (a CSV upload, a Google sync, etc.)"
    _order = "create_date desc"

    name = fields.Char(required=True)
    source_type = fields.Selection([
        ("csv", "CSV Upload"),
        ("google", "Google Contacts"),
        ("vcard", "vCard File"),
    ], required=True)
    profile_id = fields.Many2one("import.match.profile", required=True)
    create_date = fields.Datetime(readonly=True)
    staging_ids = fields.One2many("import.staging.record", "batch_id", string="Staged Records")
    staging_count = fields.Integer(compute="_compute_counts")
    new_count = fields.Integer(compute="_compute_counts")
    linked_count = fields.Integer(compute="_compute_counts")
    created_count = fields.Integer(compute="_compute_counts")
    ignored_count = fields.Integer(compute="_compute_counts")

    def _compute_counts(self):
        for batch in self:
            recs = batch.staging_ids
            batch.staging_count = len(recs)
            batch.new_count = len(recs.filtered(lambda r: r.state == "new"))
            batch.linked_count = len(recs.filtered(lambda r: r.state == "linked"))
            batch.created_count = len(recs.filtered(lambda r: r.state == "created"))
            batch.ignored_count = len(recs.filtered(lambda r: r.state == "ignored"))
