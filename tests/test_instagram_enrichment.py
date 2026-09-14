# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestInstagramEnrichment(TransactionCase):
    """Covers the soft/optional integration with instagram_manager's
    instagram.contact. contact_import has no manifest dependency on
    that module, so every test here checks for it at runtime and skips
    cleanly if it isn't installed in this database - these tests are
    only meaningful when both modules are installed together, but the
    module must still work correctly (i.e. just do nothing here)
    without it, which test_matching.py/test_standardization.py already
    exercise implicitly by not needing it at all."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.profile = cls.env.ref("contact_import.import_match_profile_contacts")
        cls.batch = cls.env["import.batch"].create({
            "name": "Instagram enrichment test batch",
            "source_type": "csv",
            "profile_id": cls.profile.id,
        })

    def setUp(self):
        super().setUp()
        if "instagram.contact" not in self.env.registry:
            self.skipTest("instagram_manager is not installed in this database")

    def _make_staging(self, **vals):
        base = {
            "profile_id": self.profile.id,
            "batch_id": self.batch.id,
            "display_name": "Enrichment Test Person",
        }
        base.update(vals)
        return self.env["import.staging.record"].create(base)

    def test_exact_unambiguous_match_fills_blank_email_and_phone(self):
        self.env["instagram.contact"].create({
            "username": "enrichmenttestperson",
            "display_name": "Enrichment Test Person",
            "email": "from-instagram@example.com",
            "phone": "+34900000000",
        })
        staging = self._make_staging()
        staging.action_compute_candidates()
        self.assertEqual(staging.email, "from-instagram@example.com")
        self.assertEqual(staging.phone, "+34900000000")
        self.assertIn("enrichmenttestperson", staging.instagram_enrichment_note or "")

    def test_never_overwrites_a_value_already_present(self):
        self.env["instagram.contact"].create({
            "username": "enrichmenttestperson2",
            "display_name": "Already Has Email",
            "email": "from-instagram@example.com",
            "phone": "+34900000000",
        })
        staging = self._make_staging(display_name="Already Has Email", email="from-csv@example.com")
        staging.action_compute_candidates()
        self.assertEqual(staging.email, "from-csv@example.com", "source-file data must win over enrichment")
        self.assertEqual(staging.phone, "+34900000000", "the blank field is still fair game to fill")

    def test_ambiguous_name_match_is_never_used(self):
        self.env["instagram.contact"].create({
            "username": "ambiguous1", "display_name": "Ambiguous Person", "email": "a1@example.com",
        })
        self.env["instagram.contact"].create({
            "username": "ambiguous2", "display_name": "Ambiguous Person", "email": "a2@example.com",
        })
        staging = self._make_staging(display_name="Ambiguous Person")
        staging.action_compute_candidates()
        self.assertFalse(staging.email, "an ambiguous match must never be guessed at")
        self.assertFalse(staging.instagram_enrichment_note)

    def test_no_match_leaves_row_untouched(self):
        staging = self._make_staging(display_name="Nobody Matches This Name At All XYZ")
        staging.action_compute_candidates()
        self.assertFalse(staging.email)
        self.assertFalse(staging.instagram_enrichment_note)
