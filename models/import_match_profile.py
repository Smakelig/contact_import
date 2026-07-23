# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
from odoo import fields, models
from psycopg2 import sql


class ImportMatchProfile(models.Model):
    """One row per 'thing being imported' - e.g. Contacts. Bundles a
    target model with the set of rules used to find candidate matches.

    v1 ships with a single hardcoded 'Contacts' profile (see
    data/import_match_profile_data.xml) and no UI to create new profiles
    or edit rules - that's deliberately deferred until there's a second
    real use case (e.g. Products) to design the config UI against. The
    schema itself is already generic enough to support that without
    changes: just add a new profile record + rule records, in code or
    via a future admin UI, pointing target_model_id at product.template.
    """
    _name = "import.match.profile"
    _description = "Contact/record import matching profile"

    name = fields.Char(required=True)
    target_model_id = fields.Many2one(
        "ir.model", required=True, ondelete="cascade",
        help="The model records get linked to or created in (e.g. res.partner).",
    )
    target_model_name = fields.Char(related="target_model_id.model", store=True)
    rule_ids = fields.One2many("import.match.rule", "profile_id", string="Matching Rules")
    active = fields.Boolean(default=True)

    def _phone_digits(self, phone):
        """Digits only, no + / spaces / dashes."""
        if not phone:
            return ""
        return "".join(c for c in phone if c.isdigit())

    def _phone_match_score(self, value_digits, target_digits):
        """Graduated confidence (0.0-1.0) for how well two digit-only
        phone strings match, instead of a binary yes/no. Phone numbers
        are a more stable identifier than free-text names, so it's
        worth extracting partial credit from a partial match rather
        than only ever awarding full credit or none.

        Tiers: 1.0 exact, 0.9 last-9-digit (national number, country
        code differs), 0.7 last-7-digit (partial), 0.0 no match."""
        if not value_digits or not target_digits:
            return 0.0
        if value_digits == target_digits:
            return 1.0
        if len(value_digits) >= 9 and len(target_digits) >= 9 and value_digits[-9:] == target_digits[-9:]:
            return 0.9
        if len(value_digits) >= 7 and len(target_digits) >= 7 and value_digits[-7:] == target_digits[-7:]:
            return 0.7
        return 0.0

    def _find_candidates(self, record_vals, limit=5):
        """Given a dict of {field_name: value} for one staged record,
        returns up to `limit` candidate matches in the target model,
        each as a dict: {record, confidence (0-100), breakdown (str)}.
        Read-only - never creates or modifies anything. Pure suggestion
        for a human to review."""
        self.ensure_one()
        Target = self.env[self.target_model_name].sudo()

        # scores: {record_id: {'record': recordset, 'score': float, 'max_score': float, 'lines': [str]}}
        scores = {}

        for rule in self.rule_ids:
            value = record_vals.get(rule.field_name)
            if not value:
                continue

            if rule.match_method == "exact":
                matches = Target.search([(rule.field_name, "=", value)])
                for m in matches:
                    self._add_score(scores, m, rule.weight, rule.weight,
                                     "%s: exact match" % rule.field_name)

            elif rule.match_method == "phone_normalized":
                value_digits = self._phone_digits(value)
                if len(value_digits) < 7:
                    continue
                suffix = value_digits[-7:]
                matches = Target.search([(rule.field_name, "ilike", suffix)])
                for m in matches:
                    target_value = m[rule.field_name]
                    target_digits = self._phone_digits(target_value)
                    tier_score = self._phone_match_score(value_digits, target_digits)
                    if tier_score <= 0:
                        continue
                    contribution = rule.weight * tier_score
                    if tier_score == 1.0:
                        label = "exact match"
                    elif tier_score == 0.9:
                        label = "national number match (country code differs)"
                    else:
                        label = "partial match (last 7 digits)"
                    self._add_score(scores, m, contribution, rule.weight,
                                     "%s: %s (%d%%)" % (rule.field_name, label, round(tier_score * 100)))

            elif rule.match_method == "trigram":
                # Field/table names are quoted as SQL identifiers (not
                # string-interpolated) even though v1 only ever calls
                # this with our own hardcoded rule data - this is the
                # code a future rule-editing UI would call too, so it's
                # worth being correct about identifier safety now rather
                # than retrofitting it later.
                query = sql.SQL(
                    "SELECT id, similarity({field}, %(value)s) AS sim "
                    "FROM {table} WHERE {field} %% %(value)s "
                    "ORDER BY sim DESC LIMIT 20"
                ).format(
                    field=sql.Identifier(rule.field_name),
                    table=sql.Identifier(Target._table),
                )
                self.env.cr.execute(query, {"value": value})
                for row_id, sim in self.env.cr.fetchall():
                    m = Target.browse(row_id)
                    contribution = rule.weight * sim
                    self._add_score(scores, m, contribution, rule.weight,
                                     "%s: %d%% similar" % (rule.field_name, round(sim * 100)))

        results = []
        for entry in scores.values():
            # Stored as a 0.0-1.0 fraction, not 0-100 - Odoo's
            # percentage widget multiplies the stored value by 100 for
            # display, so storing 0-100 here produced "10000%" on
            # screen instead of "100%".
            confidence = round(entry["score"] / entry["max_score"], 3) if entry["max_score"] else 0.0
            results.append({
                "record": entry["record"],
                "confidence": confidence,
                "breakdown": "; ".join(entry["lines"]),
            })
        results.sort(key=lambda r: r["confidence"], reverse=True)
        return results[:limit]

    def _add_score(self, scores, record, contribution, max_contribution, line):
        entry = scores.setdefault(record.id, {
            "record": record, "score": 0.0, "max_score": 0.0, "lines": [],
        })
        entry["score"] += contribution
        entry["max_score"] += max_contribution
        entry["lines"].append(line)
