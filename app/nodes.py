import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from MailState import EmailState


EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Rule-based check: does the request actually talk about an email?
EMAIL_INTENT_PATTERN = re.compile(
    r"\b(e-?mail|mail|draft|compose|write|send|reply|forward|"
    r"message|invite|invitation|letter)\b",
    re.IGNORECASE,
)

# Rule-based tone detection. First matching tags win, capped at three.
GENRE_KEYWORDS = {
    "professional": ("professional", "business", "work", "corporate", "polite"),
    "friendly": ("friendly", "warm", "kind", "nice"),
    "formal": ("formal", "official", "respectful"),
    "casual": ("casual", "informal", "relaxed", "chatty"),
    "apologetic": ("apologetic", "apology", "apologize", "sorry"),
    "persuasive": ("persuasive", "convincing", "pitch", "sales", "promote"),
    "thankful": ("thankful", "thanks", "thank you", "grateful", "appreciation"),
    "urgent": ("urgent", "asap", "immediately", "deadline", "time-sensitive"),
}

_llm = None


# --------------------------------------------------------------------------
# Schemas used for LLM structured output
# --------------------------------------------------------------------------

class RecipientAnswer(BaseModel):
    """Recipient parsed from the user's answer."""

    recipient_email: str = Field(
        default="",
        description="The email address the user provided, or an empty string.",
    )


class GenreSelection(BaseModel):
    """Tone tags inferred from the user's answer."""

    email_genre: list[str] = Field(
        default_factory=list,
        description="1-3 short tone/style tags.",
    )


class EmailDraft(BaseModel):
    """A drafted email ready for human review."""

    subject: str = Field(description="A short, clear subject line.")
    greeting: str = Field(description='Greeting line, e.g. "Hi Chand,".')
    paragraphs: list[str] = Field(
        description="2-4 short body paragraphs, excluding greeting and sign-off."
    )
    closing: str = Field(description='Short sign-off such as "Best," or "Sincerely,".')
    signature: str = Field(
        default="",
        description="Sender name for the signature; may be left empty.",
    )


# --------------------------------------------------------------------------
# LLM / config helpers
# --------------------------------------------------------------------------

def _get_llm() -> ChatGoogleGenerativeAI:
    """Create the Gemini client lazily so env vars can be loaded first."""
    global _llm
    if _llm is None:
        _llm = ChatGoogleGenerativeAI(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            google_api_key=os.environ.get("GOOGLE_API_KEY"),
            # JSON / structured output requires v1beta, not v1.
            api_version=os.environ.get("GEMINI_API_VERSION", "v1beta"),
            max_output_tokens=int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "500")),
            thinking_budget=int(os.environ.get("GEMINI_THINKING_BUDGET", "0")),
            temperature=0.7,
        )
    return _llm


def _clean_email(value: str) -> str:
    """Keep only a plausible email address from an LLM/user answer."""
    match = EMAIL_PATTERN.search(str(value or ""))
    return match.group(0) if match else ""


def _clean_genres(genres) -> list[str]:
    """Normalize LLM genre output into a clean, de-duplicated list."""
    cleaned: list[str] = []
    seen: set[str] = set()

    for genre in genres or []:
        text = str(genre).strip().strip(".,;:\"'").lower()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)

    return cleaned[:3]


def _detect_genres(text: str) -> list[str]:
    """Pick tone tags out of the request using keyword rules."""
    lowered = (text or "").lower()
    genres = [
        genre
        for genre, keywords in GENRE_KEYWORDS.items()
        if any(keyword in lowered for keyword in keywords)
    ]
    return genres[:3]


def _sender_name() -> str:
    return (os.environ.get("SENDER_NAME") or "").strip()


def _signature_rule(sender_name: str) -> str:
    if sender_name:
        return (
            "Set signature to this exact sender name: "
            f"{sender_name}."
        )
    return "Leave signature empty."


GREETING_NAME_PATTERN = re.compile(
    r"^(hi|hello|hey|dear|good morning|good afternoon|good evening)\s+([^,\n!]+)",
    re.IGNORECASE,
)


def _safe_greeting(greeting: str, source_text: str) -> str:
    """Drop a recipient name the user never actually provided."""
    greeting = (greeting or "").strip() or "Hi,"
    match = GREETING_NAME_PATTERN.match(greeting)
    if not match:
        return greeting

    name = match.group(2).strip().rstrip(",!.").strip()
    if name and name.lower() in (source_text or "").lower():
        return greeting
    return "Hi,"


def _format_draft(draft: EmailDraft, sender_name: str = "", source_text: str = "") -> str:
    """Assemble the email body so spacing is always consistent."""
    greeting = _safe_greeting(draft.greeting, source_text)
    paragraphs = [p.strip() for p in draft.paragraphs if p and p.strip()]
    closing = draft.closing.strip() or "Best,"
    signature = (sender_name or draft.signature).strip()

    sections = [greeting, *paragraphs]
    sections.append(f"{closing}\n{signature}" if signature else closing)

    return f"Subject: {draft.subject.strip()}\n\n" + "\n\n".join(sections)


def split_draft(email_draft: str) -> tuple[str, str]:
    """Split a stored draft into (subject, body)."""
    subject = ""
    body = email_draft or ""

    if body.lstrip().lower().startswith("subject:"):
        header, _, rest = body.partition("\n")
        subject = header.split(":", 1)[1].strip()
        body = rest.lstrip("\n")

    return subject, body


def _parse_approval(response) -> tuple[str, str]:
    """Read an approval answer, keeping the LLM verdict when provided as a dict."""
    if isinstance(response, dict):
        approval = str(response.get("approval", "")).strip().lower()
        feedback = str(response.get("feedback") or "").strip()
    else:
        approval = str(response or "").strip()
        feedback = approval

    words = approval.split()
    verdict = words[0] if words else ""

    if verdict in {"yes", "y", "approve", "approved", "send", "ok", "okay", "sure"}:
        return "yes", feedback

    # Anything typed after the rejection becomes revision feedback.
    remainder = " ".join(words[1:]).strip()
    return "no", remainder or feedback


# --------------------------------------------------------------------------
# Prompt templates
# --------------------------------------------------------------------------

RECIPIENT_PROMPT = """Extract the recipient's email address from the user's answer below.

Return the address exactly as the user wrote it. If there is no valid address, return an \
empty string. Never invent an address.

User's answer:
{answer}
"""

GENRE_PROMPT = """A user wants to write this email:
{request}

They were asked what tone or genre the email should have. Interpret their answer as 1-3 short \
tone/style tags (for example: professional, friendly, formal, casual, apologetic, persuasive).

If the answer is vague (such as "normal", "whatever", or "you decide"), infer the most sensible \
tone for the request and return at least one tag.

User's answer:
{answer}
"""

DRAFT_PROMPT = """You are an expert email writer.

Write a complete email for the following request.

Recipient: {recipient}
Tone / genre: {genres}
Sender name: {sender_name}

User request:
{request}

Rules:
- Fully apply every tone tag. A "friendly" email must sound warm; a "professional" email must \
sound polished and business-appropriate.
- Be concise and efficient: 60-120 words total across 2-4 short paragraphs. No filler, no \
repetition, no restating the whole request.
- Do not invent facts (dates, places, names, numbers, links) that are not in the request.
- greeting: use a recipient name only if the request explicitly provides one; otherwise use "Hi,". \
Never use the email address or its local part, and never guess a name.
- paragraphs: 2-4 short body paragraphs. Do not include the greeting or sign-off in them.
- closing: a short sign-off such as "Best," or "Sincerely,".
- {signature_rule}
- Never use placeholders like [Your Name] or [Company].
- Do not mention that you are an AI, do not explain your choices, and do not add markdown.
- subject: concise and specific.

Return the subject, greeting, paragraphs, closing, and signature.
"""

REVISE_PROMPT = """You are an expert email writer improving a draft.

Recipient: {recipient}
Tone / genre: {genres}
Sender name: {sender_name}

Current draft:
{draft}

Reviewer feedback:
{feedback}

Rewrite the email so it fully satisfies the feedback while keeping everything the reviewer did \
not criticize. Keep it to 60-120 words across 2-4 short body paragraphs. Preserve the tone / \
genre unless the feedback asks for a different one. Apply the feedback to the correct field: a \
request to change the greeting belongs in greeting, not in a paragraph. Use a recipient name only \
if the request or the reviewer feedback explicitly provides one; never use the email address or \
guess a name. Do not invent facts. No \
placeholders, no markdown, no commentary. {signature_rule}

Return the improved subject, greeting, paragraphs, closing, and signature.
"""


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------

def classify_request(state: EmailState):
    """Rule-based gate: is this an email request, and what do we already know?"""
    user_request = (state.get("user_request") or "").strip()

    if not EMAIL_INTENT_PATTERN.search(user_request):
        return {
            "status": "unsupported",
            "agent_message": (
                "I'm an email assistant, so I can only draft and send emails. "
                "Try something like: 'Draft a friendly email to "
                "alex@example.com about the project update.'"
            ),
        }

    return {
        "recipient_email": _clean_email(user_request),
        "email_genre": _detect_genres(user_request),
        "status": "classified",
    }


def ask_recipient(state: EmailState):
    """Ask the human for the recipient and have the LLM parse their answer."""
    answer = interrupt(
        {
            "kind": "recipient",
            "question": "I couldn't find a recipient email address. "
            "Who should this email be sent to?",
            "user_request": state.get("user_request", ""),
        }
    )

    try:
        parsed = _get_llm().with_structured_output(RecipientAnswer).invoke(
            RECIPIENT_PROMPT.format(answer=answer)
        )
        recipient_email = _clean_email(parsed.recipient_email)
    except Exception:
        recipient_email = _clean_email(answer)

    return {
        "recipient_email": recipient_email,
        "status": "recipient_found" if recipient_email else "recipient_missing",
    }


def ask_genre(state: EmailState):
    """Ask the human what tone/genre the email should have."""
    answer = interrupt(
        {
            "kind": "genre",
            "question": "What tone should this email have? "
            "(for example: professional, friendly, formal, apologetic, persuasive)",
            "user_request": state.get("user_request", ""),
        }
    )

    try:
        parsed = _get_llm().with_structured_output(GenreSelection).invoke(
            GENRE_PROMPT.format(
                request=state.get("user_request", ""),
                answer=answer,
            )
        )
        email_genre = _clean_genres(parsed.email_genre)
    except Exception:
        email_genre = _clean_genres(str(answer).split(",")) or _detect_genres(str(answer))

    return {
        "email_genre": email_genre,
        "status": "genre_found" if email_genre else "genre_missing",
    }


def draft_email(state: EmailState):
    """Generate the first draft with the recipient and tone in context."""
    sender_name = _sender_name()

    draft = _get_llm().with_structured_output(EmailDraft).invoke(
        DRAFT_PROMPT.format(
            recipient=state.get("recipient_email", ""),
            genres=", ".join(state.get("email_genre", [])) or "professional",
            sender_name=sender_name or "not provided",
            signature_rule=_signature_rule(sender_name),
            request=state.get("user_request", ""),
        )
    )

    return {
        "email_draft": _format_draft(
            draft, sender_name, state.get("user_request", "")
        ),
        "approval": "",
        "feedback": "",
        "revision_count": state.get("revision_count", 0),
        "status": "draft_ready",
    }


def approval_node(state: EmailState):
    """Pause the graph and ask the human to approve or reject the draft."""
    response = interrupt(
        {
            "kind": "approval",
            "question": "Do you want to send this email? Reply YES, or NO with feedback.",
            "email_draft": state.get("email_draft", ""),
            "recipient_email": state.get("recipient_email", ""),
            "email_genre": state.get("email_genre", []),
            "revision_count": state.get("revision_count", 0),
        }
    )

    approval, feedback = _parse_approval(response)

    return {
        "approval": approval,
        "feedback": feedback,
        "status": "approved" if approval == "yes" else "rejected",
    }


def revise_email(state: EmailState):
    """Rewrite the draft using the human's feedback."""
    sender_name = _sender_name()

    draft = _get_llm().with_structured_output(EmailDraft).invoke(
        REVISE_PROMPT.format(
            recipient=state.get("recipient_email", ""),
            genres=", ".join(state.get("email_genre", [])) or "professional",
            sender_name=sender_name or "not provided",
            signature_rule=_signature_rule(sender_name),
            draft=state.get("email_draft", ""),
            feedback=state.get("feedback", "") or "Improve clarity and flow.",
        )
    )

    return {
        "email_draft": _format_draft(
            draft,
            sender_name,
            f"{state.get('user_request', '')} {state.get('feedback', '')}",
        ),
        "approval": "",
        "feedback": "",
        "revision_count": state.get("revision_count", 0) + 1,
        "status": "revised",
    }


def send_email(state: EmailState):
    """Send the approved email over SMTP."""
    recipient_email = state.get("recipient_email", "")
    subject, body = split_draft(state.get("email_draft", ""))

    smtp_host = os.environ.get("SMTP_HOST")
    smtp_ssl_tls = os.environ.get("MAIL_SSL_TLS", "False").lower() == "true"
    smtp_port = int(
        os.environ.get("SMTP_PORT") or (465 if smtp_ssl_tls else 587)
    )
    mail_username = (os.environ.get("MAIL_USERNAME") or "").strip()
    # Gmail shows App Passwords in 4-char groups; the spaces must not be sent.
    mail_password = "".join((os.environ.get("MAIL_PASSWORD") or "").split())
    mail_from = (os.environ.get("MAIL_FROM") or mail_username).strip()
    mail_starttls = os.environ.get("MAIL_STARTTLS", "True").lower() == "true"
    sender_name = _sender_name()

    if not recipient_email:
        raise ValueError("No recipient email address to send to.")
    if not smtp_host or not mail_username or not mail_password:
        raise ValueError(
            "SMTP is not configured. Set SMTP_HOST, MAIL_USERNAME, and MAIL_PASSWORD."
        )

    message = EmailMessage()
    message["From"] = formataddr((sender_name, mail_from)) if sender_name else mail_from
    message["To"] = recipient_email
    message["Subject"] = subject or "Message"
    message.set_content(body)

    try:
        if smtp_ssl_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                smtp_host, smtp_port, timeout=30, context=context
            ) as server:
                if os.environ.get("SMTP_DEBUG", "false").lower() == "true":
                    server.set_debuglevel(1)
                server.login(mail_username, mail_password)
                server.send_message(message)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                if os.environ.get("SMTP_DEBUG", "false").lower() == "true":
                    server.set_debuglevel(1)
                server.ehlo()
                if mail_starttls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                server.login(mail_username, mail_password)
                server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise ValueError(
            "Gmail rejected the login (535 Bad Credentials). Enable 2-Step Verification, "
            "create a new App Password at https://myaccount.google.com/apppasswords, and make "
            "sure MAIL_USERNAME is the same Google account. Spaces in the App Password are "
            "ignored automatically."
        ) from exc
    except smtplib.SMTPServerDisconnected as exc:
        raise ConnectionError(
            "The SMTP server closed the connection during login. This is almost always a "
            "rejected App Password (Gmail answers 535 Bad Credentials, then disconnects). "
            "Enable 2-Step Verification, generate a fresh App Password, and confirm that "
            "MAIL_USERNAME matches that Google account."
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        hint = ""
        if smtp_port == 587:
            hint = " If your network blocks port 587, try SMTP_PORT=465 with MAIL_SSL_TLS=True."
        raise ConnectionError(
            f"Could not send via {smtp_host}:{smtp_port} ({exc}). "
            f"Check the SMTP host/port, your internet connection, and the App Password.{hint}"
        ) from exc

    return {
        "status": "sent",
    }
