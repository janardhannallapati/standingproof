"""What a GitHub webhook delivery tells us: which settings to go and look at.

A delivery says that something changed, and where. It is never the source of a fact: the
watcher reads the setting itself afterwards. So only three things are taken from the
payload (a repository name, and the event's action for the record), each checked against a
strict shape, and nothing else in it is read, kept or logged. Pure.
"""

from dataclasses import dataclass

from standingproof.checks.authorize import valid_scope

# Event name -> the check that reads the setting the event is about.
_CHECK_FOR_EVENT = {
    "branch_protection_rule": "github.branch_protection",
    "security_and_analysis": "github.secret_scanning",
}
_MAX_REPO_CHARS = 200
_MAX_ACTION_CHARS = 40
_ACTION_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz_")


@dataclass(frozen=True)
class Look:
    check: str
    # "owner/repository", lower case.
    scope: str


@dataclass(frozen=True)
class Reading:
    # The organisation the delivery is about, lower case. None when it could not be read.
    org: str | None
    action: str | None
    looks: tuple[Look, ...]
    # Why nothing is looked at: "ping", "event_not_handled", "bad_repository". Else None.
    ignored: str | None


def _repository(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    repository = payload.get("repository")
    name = repository.get("full_name") if isinstance(repository, dict) else None
    if not isinstance(name, str) or len(name) > _MAX_REPO_CHARS or not valid_scope("repo", name):
        return None
    return name.lower()


def _action(payload: object) -> str | None:
    action = payload.get("action") if isinstance(payload, dict) else None
    if not isinstance(action, str) or not 0 < len(action) <= _MAX_ACTION_CHARS:
        return None
    return action if set(action) <= _ACTION_CHARS else None


def read(event: str, payload: object) -> Reading:
    """`event` is the X-GitHub-Event header; `payload` is the parsed body, of any shape."""
    if event == "ping":
        return Reading(None, None, (), "ping")
    repository = _repository(payload)
    check = _CHECK_FOR_EVENT.get(event)
    if check is None:
        org = repository.split("/")[0] if repository else None
        return Reading(org, None, (), "event_not_handled")
    if repository is None:
        return Reading(None, None, (), "bad_repository")
    # Any action of these events is a reason to look. GitHub adds actions over time, and
    # an unknown one costs a single read.
    return Reading(repository.split("/")[0], _action(payload), (Look(check, repository),), None)
