"""Which modules may open a session that crosses tenants.

The system role is bound by no tenant, so every use of it is a place where one tenant's
rows could be handed to another by mistake. The database keeps it away from customer
content (see test_database_rules.py); this keeps it to the few modules that find work.
A new use fails here until it is added to the list, which is a line a reviewer sees.
"""

from pathlib import Path

SOURCE = Path(__file__).parents[2] / "src" / "standingproof"

ALLOWED = {
    "db.py": "defines it",
    "api/deps.py": "finds which tenant a request is for, before there is one",
    "api/events.py": "the same, for a live connection",
    "api/webhooks.py": "records a delivery, which may be about no tenant",
    "workflow/worker.py": "finds whose question a job names; sweeps and prunes",
    "drift/watcher.py": "works through deliveries; finds settings that are due",
    "library/__main__.py": "lists the tenants whose libraries to re-embed",
    "drift/examine.py": "finds which tenants have an observation waiting",
}
MARKS = ("across_tenants", "SystemSessionDep", "get_system_session", 'role_url("system")')


def test_only_the_listed_modules_cross_tenants() -> None:
    users = {
        str(path.relative_to(SOURCE))
        for path in SOURCE.rglob("*.py")
        if any(mark in path.read_text() for mark in MARKS)
    }

    assert users == set(ALLOWED)
