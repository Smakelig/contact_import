# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
{
    'name': 'Contact Import Staging',
    'version': '17.0.1.1.0',
    'summary': 'Fuzzy-match import staging: review and link/create records without duplicating your database',
    'description': """
        Generic staging/review layer for importing contacts (and, later,
        other models such as products) from external sources without
        automatically creating duplicate records.

        - Nothing is ever written to res.partner (or any target model)
          without an explicit human action per row.
        - Fuzzy matching (exact email, normalized phone, trigram name
          similarity) suggests candidate matches with a confidence score.
        - Currently wired up for Contacts via CSV upload. The underlying
          schema (import.match.profile / import.match.rule) is deliberately
          generic so a second profile (e.g. Products) can be added later
          without a schema change - see models/import_match_profile.py.
        - Large imports are queued and processed in the background in
          bounded chunks (see import.batch and data/ir_cron_data.xml)
          instead of blocking the browser/request for the whole file.

        Requires PostgreSQL's pg_trgm extension for name similarity
        matching (enabled automatically on install via post_init_hook).
    """,
    'category': 'Contacts',
    'author': 'Fermenteria Smakelig',
    'website': 'https://fermenteria.es',
    'license': 'LGPL-3',
    'depends': ['base', 'contacts'],
    'data': [
        'security/ir.model.access.csv',
        'data/import_match_profile_data.xml',
        'data/ir_cron_data.xml',
        'views/import_staging_views.xml',
        'views/import_match_profile_views.xml',
        'wizard/csv_import_wizard_views.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'installable': True,
    'application': False,
    'auto_install': False,
}
