# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
from odoo import fields, models


class ImportStagingCandidate(models.Model):
    """One suggested match for a staging record, with a confidence score
    and a human-readable breakdown of why it matched. Purely advisory -
    linking only happens via ImportStagingRecord.action_link_to_candidate,
    an explicit action a person takes."""
    _name = "import.staging.candidate"
    _description = "A suggested match for a staged import record"
    _order = "confidence desc"

    staging_id = fields.Many2one("import.staging.record", required=True, ondelete="cascade")
    matched_record_ref = fields.Reference(
        selection=lambda self: self.env["import.staging.record"]._selection_target_models(),
        required=True,
    )
    confidence = fields.Float(string="Confidence %")
    match_breakdown = fields.Char(help="Which rules matched and how strongly, e.g. 'email: exact match; name: 82% similar'.")

    def action_link_candidate_button(self):
        """Called from the button in the embedded candidate list on the
        staging record's form - delegates to the parent record's link
        action, since that's where state/linked_record_ref actually live."""
        for candidate in self:
            candidate.staging_id.action_link_to_candidate(candidate.id)
