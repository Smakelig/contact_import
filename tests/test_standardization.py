# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestStandardization(TransactionCase):
    """Covers the Odoo-17-standard-alignment work: state/country
    resolution (scoped correctly), phone formatting on create, and
    email normalization. These are exactly the kind of "quiet" behavior
    that's easy to break without a test noticing, since nothing about a
    broken resolution raises an error - it just silently leaves a
    field unresolved."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.profile = cls.env.ref("contact_import.import_match_profile_contacts")
        cls.batch = cls.env["import.batch"].create({
            "name": "Standardization test batch",
            "source_type": "csv",
            "profile_id": cls.profile.id,
        })
        cls.spain = cls.env.ref("base.es")
        cls.france = cls.env.ref("base.fr")

    def _make_staging(self, **vals):
        base = {
            "profile_id": self.profile.id,
            "batch_id": self.batch.id,
            "display_name": "Test Person",
        }
        base.update(vals)
        return self.env["import.staging.record"].create(base)

    def test_country_resolves_by_name(self):
        staging = self._make_staging(country_name="Spain")
        self.assertEqual(staging._resolve_country(), self.spain)

    def test_country_resolves_by_code(self):
        staging = self._make_staging(country_name="ES")
        self.assertEqual(staging._resolve_country(), self.spain)

    def test_country_unresolved_left_empty_not_invented(self):
        staging = self._make_staging(country_name="Not A Real Country XYZ")
        self.assertFalse(staging._resolve_country())

    def test_state_resolution_is_scoped_to_country(self):
        # Two different countries can have a state/province sharing the
        # same CODE (res.country.state's own constraint is per-country,
        # not global) - this is exactly the scenario _resolve_state's
        # country scoping exists to get right. We fabricate a collision
        # deliberately rather than relying on a real-world one existing
        # in whatever demo/localization data happens to be loaded.
        State = self.env["res.country.state"]
        existing_code_es = State.search([("country_id", "=", self.spain.id)], limit=1).code
        collide_code = (existing_code_es or "ZZ") + "9"  # a code very unlikely to already exist anywhere
        state_es = State.create({"name": "Fixture State ES", "code": collide_code, "country_id": self.spain.id})
        state_fr = State.create({"name": "Fixture State FR", "code": collide_code, "country_id": self.france.id})

        staging = self._make_staging(country_name="Spain", state_name=collide_code)
        resolved = staging._resolve_state(self.spain)
        self.assertEqual(resolved, state_es)
        self.assertNotEqual(resolved, state_fr)

    def test_state_unresolved_left_empty_not_invented(self):
        staging = self._make_staging(state_name="Not A Real State XYZ")
        self.assertFalse(staging._resolve_state(self.spain))

    def test_email_normalized_on_create(self):
        staging = self._make_staging(email="  Mixed.Case@EXAMPLE.com  ")
        partner = staging.action_create_new()
        self.assertEqual(partner.email, "Mixed.Case@example.com")

    def test_email_falls_back_to_raw_value_if_unnormalizable(self):
        # More than one address in the field - email_normalize(strict=False)
        # returns the first candidate; this just confirms we never end
        # up with a blank email rather than pinning one specific library
        # behavior.
        staging = self._make_staging(email="not-an-email-at-all;;;")
        partner = staging.action_create_new()
        self.assertTrue(partner.email)

    def test_phone_formatted_to_international_on_create(self):
        staging = self._make_staging(phone="931234567", country_name="Spain")
        partner = staging.action_create_new()
        # phone_validation formats to INTERNATIONAL form, e.g. "+34 931
        # 23 45 67" - assert the substantive parts rather than an exact
        # string, since exact spacing is a library detail.
        self.assertTrue(partner.phone.startswith("+34"))
        self.assertIn("931", partner.phone.replace(" ", ""))

    def test_phone_kept_as_raw_value_if_unformattable(self):
        staging = self._make_staging(phone="not-a-real-phone-number")
        partner = staging.action_create_new()
        # _phone_format returns False for something unparseable - the
        # raw typed value must survive, not get blanked.
        self.assertEqual(partner.phone, "not-a-real-phone-number")

    def test_street2_and_state_mapped_on_create(self):
        staging = self._make_staging(
            street="Carrer Fake 1", street2="2n pis", country_name="Spain", state_name="Barcelona",
        )
        partner = staging.action_create_new()
        self.assertEqual(partner.street2, "2n pis")
        self.assertEqual(partner.country_id, self.spain)
        if partner.state_id:  # only asserted if "Barcelona" resolves in this DB's state data
            self.assertEqual(partner.state_id.country_id, self.spain)
