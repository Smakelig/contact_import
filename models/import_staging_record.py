# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
import hashlib

from odoo import api, fields, models
from odoo.tools import email_normalize


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
    street = fields.Char(help="Maps to res.partner.street.")
    street2 = fields.Char(help="Maps to res.partner.street2 (second address line).")
    city = fields.Char(help="Maps to res.partner.city.")
    zip_code = fields.Char(string="ZIP/Postal Code", help="Maps to res.partner.zip.")
    country_name = fields.Char(
        help="Raw country name/code from the source. Resolved to a real "
             "res.country record by name search when this row is created "
             "(not stored as a Many2one here, since the source's spelling "
             "may not exactly match Odoo's country names until resolved).",
    )
    state_name = fields.Char(
        string="State/Province",
        help="Raw state/province name or code from the source. Resolved to "
             "a real res.country.state record (scoped to the resolved "
             "country, since state codes repeat across countries - e.g. "
             "res.country.state's own uniqueness constraint is per-country, "
             "not global) when this row is created. Left unresolved if no "
             "match is found - same policy as country_name, this is fixed "
             "reference data, not something to invent from source text.",
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
    instagram_enrichment_note = fields.Char(
        readonly=True,
        help="Set when action_compute_candidates found an exact, unambiguous "
             "name match in the Instagram Contact Directory (instagram_manager's "
             "instagram.contact) and used it to fill a blank email/phone on "
             "this row. Kept separate from Notes/comment so system-provenance "
             "text never gets mixed up with actual source-file notes.",
    )

    def _selection_target_models(self):
        # Populated from whatever import.match.profile target models
        # actually exist, so this stays correct if/when a Products
        # profile is added later without any code change here.
        profiles = self.env["import.match.profile"].sudo().search([])
        return [(p.target_model_name, p.target_model_id.name) for p in profiles if p.target_model_name]

    @api.model
    def _create_from_import_row(self, profile, batch, vals, raw_row):
        """Given already-resolved column values for one imported row
        (the shape produced by contact.import.csv.wizard._row_values,
        or any future import source that resolves to the same dict
        shape), creates the staging record - or returns None for an
        expected skip (empty name, or an already-seen duplicate from an
        EARLIER batch). This is the single place that turns "resolved
        row values" into an actual staging record, used identically
        whether it's called from a small synchronous import or from
        import.batch's background chunk processor - see
        models/import_batch.py._process_pending_chunk.

        Any exception here is the caller's problem to catch and roll
        back via savepoint - this assumes it's already running inside
        one, exactly like the per-row processing it replaced."""
        name = (vals.get("name") or "").strip()
        if not name:
            return None

        email = vals.get("email") or ""
        phone = vals.get("phone") or ""

        # Stable ref so re-importing the same file doesn't create
        # duplicate staging rows for rows already seen before.
        source_ref = hashlib.sha256(
            ("%s|%s|%s" % (name, email, phone)).encode("utf-8")
        ).hexdigest()

        # Only dedupe against OTHER batches (a genuine repeat import of
        # a previously-seen row), not rows within this same batch. Two
        # different people sharing a name with no email/phone on file
        # (common for e.g. an event/workshop attendee list) hash
        # identically - deduping within one file would silently drop
        # the second one entirely, with no human ever seeing it. A
        # true duplicate LINE within one file staging twice is a much
        # cheaper mistake (a human just ignores/dismisses the extra
        # row) than a distinct contact vanishing with no record of it.
        existing = self.search([
            ("profile_id", "=", profile.id),
            ("source_ref", "=", source_ref),
            ("batch_id", "!=", batch.id),
        ], limit=1)
        if existing:
            return None

        return self.create({
            "profile_id": profile.id,
            "batch_id": batch.id,
            "source_ref": source_ref,
            "display_name": name,
            "email": email or False,
            "phone": phone or False,
            "function": vals.get("job_title") or False,
            "tag_names": vals.get("tag_names") or False,
            "street": vals.get("street") or False,
            "street2": vals.get("street2") or False,
            "city": vals.get("city") or False,
            "zip_code": vals.get("zip_code") or False,
            "state_name": vals.get("state_name") or False,
            "country_name": vals.get("country_name") or False,
            "notes": vals.get("notes") or False,
            "raw_data": str(raw_row),
        })

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
            rec._enrich_from_instagram()
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

    def _instagram_contact_model(self):
        """Returns the instagram.contact model, sudo'd for read access, or
        None if instagram_manager isn't installed. Soft/optional
        integration - contact_import has no manifest dependency on
        instagram_manager and stays fully installable without it. Same
        duck-typing approach instagram_manager's own res_partner.py already
        uses for ITS optional Facebook integration ('facebook_contact_ids'
        in self._fields) - not a new pattern, just this codebase's
        established way of doing an optional cross-module link."""
        if "instagram.contact" not in self.env.registry:
            return None
        return self.env["instagram.contact"].sudo()

    def _enrich_from_instagram(self):
        """Fills a blank email/phone on this staged row from the business's
        own Instagram Contact Directory, when instagram_manager happens to
        be installed. Those phone/email values are themselves only ever
        populated there from Meta's 'Download Your Information' synced-
        contacts export on an exact name match (see instagram.contact's own
        field help text) - a real, deliberately-vetted source, not
        something scraped from a casual DM.

        Matching policy mirrors instagram_manager's own
        instagram.contact.partner.reconcile.wizard exactly: only an EXACT,
        UNAMBIGUOUS display_name match is used. A fuzzy or ambiguous match
        is a real mistake risk here (the wrong person's phone number lands
        on this row) - that wizard treats fuzzy matches as suggestions a
        human must confirm, never auto-applies them, and this does the
        same by simply skipping rather than guessing.

        Fill-blanks-only, same as instagram_manager's own
        action_enrich_from_social: never overwrites a value the source
        file itself provided, since that's a more direct source for this
        specific row than a same-name match in another system."""
        self.ensure_one()
        if not self.display_name or (self.email and self.phone):
            return
        Contact = self._instagram_contact_model()
        if Contact is None:
            return

        name = self.display_name.strip()
        if not name:
            return
        matches = Contact.search([("display_name", "=ilike", name)])
        if len(matches) != 1:
            return
        contact = matches

        filled = []
        if not self.email and contact.email:
            self.email = contact.email
            filled.append("email")
        if not self.phone and contact.phone:
            self.phone = contact.phone
            filled.append("phone")
        if filled:
            self.instagram_enrichment_note = (
                "%s filled from Instagram Directory match '@%s'"
                % (" & ".join(filled).capitalize(), contact.username)
            )

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

    def _resolve_country(self):
        """Looks up a res.country by name (case-insensitive) or ISO code
        from the raw country_name text. Read-only, returns an empty
        recordset if nothing matches rather than creating one - country
        lists are fixed reference data, not something to invent from a
        typo'd CSV value."""
        self.ensure_one()
        if not self.country_name:
            return self.env["res.country"].browse()
        name = self.country_name.strip()
        country = self.env["res.country"].search([("name", "=ilike", name)], limit=1)
        if not country and len(name) in (2, 3):
            country = self.env["res.country"].search([("code", "=ilike", name)], limit=1)
        return country

    def _resolve_state(self, country):
        """Looks up a res.country.state by name or code from the raw
        state_name text, scoped to `country` when known. Scoping matters:
        res.country.state's own uniqueness constraint is (country_id,
        code), not code alone, so e.g. code 'B' means something different
        per country - searching without a country scope risks matching
        the wrong state entirely rather than just failing to match. Same
        read-only, no-invention policy as _resolve_country: an empty
        recordset if nothing matches, never a created record."""
        self.ensure_one()
        if not self.state_name:
            return self.env["res.country.state"].browse()
        name = self.state_name.strip()
        domain = [("country_id", "=", country.id)] if country else []
        state = self.env["res.country.state"].search(domain + [("name", "=ilike", name)], limit=1)
        if not state:
            state = self.env["res.country.state"].search(domain + [("code", "=ilike", name)], limit=1)
        return state

    def _normalized_email(self):
        """Odoo's own standard for email sanitization (odoo.tools.mail.
        email_normalize): lowercases the domain, trims whitespace, strips
        a 'Name <addr>' wrapper if present. Falls back to the raw staged
        value if normalization can't make sense of it (e.g. more than one
        address in the field) rather than dropping the data - a human
        reviewing the created contact can still see and fix a slightly
        odd but present value, whereas a blanked field just looks like
        nothing was ever imported."""
        self.ensure_one()
        if not self.email:
            return self.email
        return email_normalize(self.email, strict=False) or self.email

    def action_create_new(self):
        """Explicit human action: create a brand-new record in the
        target model from this staged row's data."""
        self.ensure_one()
        Target = self.env[self.profile_id.target_model_name]
        vals = {"name": self.display_name}
        if self.email and "email" in Target._fields:
            vals["email"] = self._normalized_email()
        if self.phone and "phone" in Target._fields:
            vals["phone"] = self.phone
        if self.function and "function" in Target._fields:
            vals["function"] = self.function
        if self.notes and "comment" in Target._fields:
            vals["comment"] = self.notes
        if self.street and "street" in Target._fields:
            vals["street"] = self.street
        if self.street2 and "street2" in Target._fields:
            vals["street2"] = self.street2
        if self.city and "city" in Target._fields:
            vals["city"] = self.city
        if self.zip_code and "zip" in Target._fields:
            vals["zip"] = self.zip_code
        country = self.env["res.country"].browse()
        if self.country_name and "country_id" in Target._fields:
            country = self._resolve_country()
            if country:
                vals["country_id"] = country.id
        if self.state_name and "state_id" in Target._fields:
            state = self._resolve_state(country)
            if state:
                vals["state_id"] = state.id
        if "category_id" in Target._fields:
            tags = self._resolve_tags()
            if tags:
                vals["category_id"] = [(6, 0, tags.ids)]
        new_record = Target.create(vals)

        # Odoo's standard phone formatting (phone_validation's
        # _phone_format, mixed onto every model via `base`) only runs as
        # a form onchange - a server-side create() like this one never
        # triggers it, so without this the number lands exactly as typed
        # in the source file. Format to international form now that the
        # record (and its country_id, which _phone_format needs to pick
        # the right dialing rules) actually exists. If the number can't
        # be parsed for that country, _phone_format returns False and we
        # leave the original raw value in place rather than blanking it.
        if "phone" in Target._fields and new_record.phone:
            formatted = new_record._phone_format(fname="phone", force_format="INTERNATIONAL")
            if formatted:
                new_record.phone = formatted

        self.linked_record_ref = "%s,%s" % (Target._name, new_record.id)
        self.state = "created"
        return new_record

    def action_ignore(self):
        self.write({"state": "ignored"})
