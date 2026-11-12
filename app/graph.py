from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

from MailState import EmailState
from nodes import (
    classify_request,
    ask_recipient,
    ask_genre,
    draft_email,
    approval_node,
    revise_email,
    send_email,
)


# ---------- Routing functions for the conditional edges ----------

def route_after_classify(state: EmailState) -> str:
    """Not an email request -> stop. Missing info -> ask. Otherwise draft."""
    if state.get("status") == "unsupported":
        return "unsupported"
    if not (state.get("recipient_email") or "").strip():
        return "ask_recipient"
    if not state.get("email_genre"):
        return "ask_genre"
    return "draft_email"


def route_after_ask_recipient(state: EmailState) -> str:
    """Re-ask if still missing, then fill in the genre before drafting."""
    if not (state.get("recipient_email") or "").strip():
        return "ask_recipient"
    if not state.get("email_genre"):
        return "ask_genre"
    return "draft_email"


def route_after_ask_genre(state: EmailState) -> str:
    """Keep asking until a tone is known, then draft."""
    if not state.get("email_genre"):
        return "ask_genre"
    return "draft_email"


def route_after_approval(state: EmailState) -> str:
    """YES -> send_email | NO -> revise_email"""
    if (state.get("approval") or "").strip().lower() == "yes":
        return "send_email"
    return "revise_email"


# ---------- Graph ----------

builder = StateGraph(EmailState)

builder.add_node("classify_request", classify_request)
builder.add_node("ask_recipient", ask_recipient)
builder.add_node("ask_genre", ask_genre)
builder.add_node("draft_email", draft_email)
builder.add_node("approval", approval_node)
builder.add_node("revise_email", revise_email)
builder.add_node("send_email", send_email)

# START -> classify_request
builder.add_edge(START, "classify_request")

# classify_request -> unsupported? recipient? genre? draft?
builder.add_conditional_edges(
    "classify_request",
    route_after_classify,
    {
        "unsupported": END,
        "ask_recipient": "ask_recipient",
        "ask_genre": "ask_genre",
        "draft_email": "draft_email",
    },
)

# ask_recipient -> (still missing? genre? draft?)
builder.add_conditional_edges(
    "ask_recipient",
    route_after_ask_recipient,
    {
        "ask_recipient": "ask_recipient",
        "ask_genre": "ask_genre",
        "draft_email": "draft_email",
    },
)

# ask_genre -> draft_email (rejoins the main path)
builder.add_conditional_edges(
    "ask_genre",
    route_after_ask_genre,
    {
        "ask_genre": "ask_genre",
        "draft_email": "draft_email",
    },
)

# draft_email -> approval
builder.add_edge("draft_email", "approval")

# approval -> YES / NO
builder.add_conditional_edges(
    "approval",
    route_after_approval,
    {
        "send_email": "send_email",
        "revise_email": "revise_email",
    },
)

# NO branch loops back into approval after revising
builder.add_edge("revise_email", "approval")

# YES branch ends the workflow
builder.add_edge("send_email", END)

graph = builder.compile(checkpointer=InMemorySaver())
