# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMatchProfile(TransactionCase):
    """Covers import.match.profile._find_candidates and the phone-tier
    scoring it relies on - this is the module's actual "point" (fuzzy
    matching against an existing target model) and the piece most
    likely to silently regress, so it gets the most direct coverage."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.profile = cls.env.ref("contact_import.import_match_profile_contacts")
        cls.Partner = cls.env["res.partner"]

    def test_exact_email_match(self):
        partner = self.Partner.create({"name": "Jane Exact", "email": "jane@example.com"})
        results = self.profile._find_candidates({"email": "jane@example.com", "name": "Someone Else"})
        matched_ids = [r["record"].id for r in results]
        self.assertIn(partner.id, matched_ids)
        hit = next(r for r in results if r["record"].id == partner.id)
        self.assertEqual(hit["confidence"], 1.0)

    def test_email_match_is_case_sensitive_by_design(self):
        # The rule uses an exact "=" comparison (see import_match_rule
        # data: match_method 'exact'), not "=ilike" - this test pins
        # that current behavior down so a future change to it is a
        # deliberate decision, not an accidental regression.
        self.Partner.create({"name": "Case Test", "email": "Mixed@Example.com"})
        results = self.profile._find_candidates({"email": "mixed@example.com", "name": "x"})
        self.assertEqual(results, [])

    def test_phone_exact_match_full_confidence(self):
        partner = self.Partner.create({"name": "Phone Exact", "phone": "+34 93 123 45 67"})
        results = self.profile._find_candidates({"phone": "+34931234567", "name": "x"})
        hit = next((r for r in results if r["record"].id == partner.id), None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["confidence"], 1.0)

    def test_phone_national_number_partial_confidence(self):
        # Same national number, different/missing country code - tier
        # 2 in _phone_match_score (0.9), not a full match.
        partner = self.Partner.create({"name": "Phone National", "phone": "+34931234567"})
        results = self.profile._find_candidates({"phone": "931234567", "name": "x"})
        hit = next((r for r in results if r["record"].id == partner.id), None)
        self.assertIsNotNone(hit)
        self.assertLess(hit["confidence"], 1.0)
        self.assertGreater(hit["confidence"], 0.5)

    def test_phone_too_short_is_never_matched(self):
        # _find_candidates skips phone_normalized entirely below 7
        # digits - a short/garbage value shouldn't return noise matches.
        self.Partner.create({"name": "Short Phone", "phone": "123456"})
        results = self.profile._find_candidates({"phone": "123456", "name": "x"})
        self.assertEqual(results, [])

    def test_trigram_name_similarity_scores_between_zero_and_one(self):
        partner = self.Partner.create({"name": "Joachim Fermenteria"})
        results = self.profile._find_candidates({"name": "Joachim Fermenteri"})  # 1-char typo
        hit = next((r for r in results if r["record"].id == partner.id), None)
        self.assertIsNotNone(hit, "a 1-character typo should still surface as a trigram candidate")
        self.assertGreater(hit["confidence"], 0.0)
        self.assertLessEqual(hit["confidence"], 1.0)

    def test_multiple_matching_rules_combine_into_one_higher_score(self):
        # email (weight 3.0) + phone (weight 2.0) both matching should
        # score higher than either alone, and still be capped at 1.0 -
        # _add_score divides by the SUM of contributing rules' own
        # weights, not a fixed denominator, so this should land at 1.0
        # exactly (both rules fully satisfied), not something >1.
        partner = self.Partner.create({
            "name": "Both Match", "email": "both@example.com", "phone": "+34931234567",
        })
        results = self.profile._find_candidates({
            "email": "both@example.com", "phone": "+34931234567", "name": "Unrelated Name Text",
        })
        hit = next(r for r in results if r["record"].id == partner.id)
        self.assertEqual(hit["confidence"], 1.0)

    def test_no_candidates_when_nothing_provided(self):
        results = self.profile._find_candidates({})
        self.assertEqual(results, [])

    def test_results_limited_and_sorted_by_confidence_desc(self):
        # Create more matches than the default limit to confirm both
        # the limit and the sort order hold.
        for i in range(8):
            self.Partner.create({"name": "Trigram Candidate %d" % i, "phone": "+3493123450%d" % i})
        results = self.profile._find_candidates({"phone": "+34931234500", "name": "Trigram Candidate 0"}, limit=3)
        self.assertLessEqual(len(results), 3)
        confidences = [r["confidence"] for r in results]
        self.assertEqual(confidences, sorted(confidences, reverse=True))
