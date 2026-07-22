# Copyright 2026 Fermenteria Smakelig
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).
from . import models
from . import wizard


def post_init_hook(env):
    """Enable Postgres's pg_trgm extension, needed for trigram-similarity
    name matching. Requires the DB user to have CREATE EXTENSION rights
    (true for the default Odoo DB owner in a standard install)."""
    env.cr.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
