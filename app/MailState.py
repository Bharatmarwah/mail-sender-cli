from typing import TypedDict


class EmailState(TypedDict, total=False):
    # User's original request
    user_request: str

    # Email addresses
    recipient_email: str

    # Tone / genre tags, e.g. ["professional", "friendly"]
    email_genre: list[str]

    # Generated email
    email_draft: str

    # Human approval
    approval: str

    # User feedback for revisions
    feedback: str

    # Revision tracking
    revision_count: int

    # Workflow status
    status: str

    # Friendly message shown when the request is not about email
    agent_message: str
