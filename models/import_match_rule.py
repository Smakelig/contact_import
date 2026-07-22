# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
from odoo import fields, models


class ImportMatchRule(models.Model):
    """A single matching criterion within a profile - e.g. 'match on
    email, exact, weight 1.0'. field_name is a plain Char (not a proper
    ir.model.fields reference) deliberately: v1's rules are created once
    in XML data, never edited through the UI, so the extra indirection
    of a real field reference isn't earning its complexity yet. If/when
    a rule-configuration UI gets built, this is the field to upgrade to
    a Many2one('ir.model.fields', domain=...) for a proper field picker.
    """
    _name = "import.match.rule"
    _description = "Single field-matching rule within an import profile"
    _order = "sequence, id"

    profile_id = fields.Many2one("import.match.profile", required=True, ondelete="cascade")
    field_name = fields.Char(
        required=True,
        help="Technical field name on the profile's target model, e.g. 'email'.",
    )
    match_method = fields.Selection([
        ("exact", "Exact match"),
        ("phone_normalized", "Phone (normalized, last 9 digits)"),
        ("trigram", "Text similarity (trigram)"),
    ], required=True, default="exact")
    weight = fields.Float(default=1.0, help="Relative contribution to the overall match score.")
    sequence = fields.Integer(default=10)
