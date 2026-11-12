"""Console UI helpers: render a drafted email like a mail client preview."""

import shutil
import sys
import textwrap

from nodes import split_draft


CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"


def ensure_utf8_stdout() -> None:
    """Windows consoles often default to cp1252; switch to UTF-8 when possible."""
    try:
        encoding = (sys.stdout.encoding or "").lower()
        if encoding and "utf" not in encoding:
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


ensure_utf8_stdout()

# Box characters, with an ASCII fallback for consoles that reject Unicode.
_UNICODE = True
try:
    "╭─│╮╰╯├┤✉✓".encode(sys.stdout.encoding or "utf-8")
except (UnicodeEncodeError, LookupError):
    _UNICODE = False

if _UNICODE:
    TOP_LEFT, TOP_RIGHT = "╭", "╮"
    BOTTOM_LEFT, BOTTOM_RIGHT = "╰", "╯"
    TEE_LEFT, TEE_RIGHT = "├", "┤"
    HORIZONTAL, VERTICAL = "─", "│"
    MAIL_ICON, CHECK_ICON = "✉", "✓"
else:
    TOP_LEFT = BOTTOM_LEFT = TEE_LEFT = "+"
    TOP_RIGHT = BOTTOM_RIGHT = TEE_RIGHT = "+"
    HORIZONTAL, VERTICAL = "-", "|"
    MAIL_ICON, CHECK_ICON = "*", "+"


def _box_width() -> int:
    columns = shutil.get_terminal_size((88, 24)).columns
    return max(64, min(columns - 1, 88))


def render_email_preview(recipient_email: str, email_genre, email_draft: str) -> None:
    """Print the draft as a bordered, mail-client-style preview."""
    subject, body = split_draft(email_draft)

    width = _box_width()
    inner = width - 4
    genres = ", ".join(email_genre or []) or "not specified"

    def border(char_left: str, char_right: str) -> str:
        return f"{CYAN}{char_left}{HORIZONTAL * (inner + 2)}{char_right}{RESET}"

    def row(text: str = "") -> str:
        return f"{CYAN}{VERTICAL}{RESET} {text.ljust(inner)} {CYAN}{VERTICAL}{RESET}"

    def field(label: str, value: str) -> list[str]:
        prefix = f"{label:<8}: "
        wrapped = textwrap.wrap(
            f"{prefix}{value}",
            inner,
            subsequent_indent=" " * len(prefix),
        ) or [prefix.rstrip()]
        return [row(line) for line in wrapped]

    lines = [border(TOP_LEFT, TOP_RIGHT)]
    lines.append(row(f"{BOLD}{MAIL_ICON}  EMAIL PREVIEW{RESET}"))
    lines.append(border(TEE_LEFT, TEE_RIGHT))
    lines.extend(field("To", recipient_email))
    lines.extend(field("Genre", genres))
    lines.extend(field("Subject", subject or "(no subject)"))
    lines.append(border(TEE_LEFT, TEE_RIGHT))

    body_lines = body.splitlines() or [""]
    for raw_line in body_lines:
        wrapped = textwrap.wrap(
            raw_line,
            inner,
            replace_whitespace=False,
            drop_whitespace=True,
        ) or [""]
        for chunk in wrapped:
            lines.append(row(chunk))

    lines.append(border(BOTTOM_LEFT, BOTTOM_RIGHT))

    print("")
    print("\n".join(lines))
    print("")


def render_sent(recipient_email: str, email_draft: str) -> None:
    """Print a short success message after sending."""
    subject, _ = split_draft(email_draft)
    print(
        f"{GREEN}{BOLD}{CHECK_ICON} Email sent{RESET} "
        f"{DIM}to {recipient_email}{RESET}"
        + (f" {DIM}— {subject}{RESET}" if subject else "")
    )
