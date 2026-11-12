import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv


# --- ANSI Color Codes for the Terminal ---
BLUE = "\033[94m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"
CLEAR_LINE = "\r\033[K"


def load_env_file() -> None:
    """Load variables from the project .env (works for source and frozen exes)."""
    if getattr(sys, "frozen", False):
        # PyInstaller onefile: treat the folder next to the exe as the project root.
        base_dir = Path(sys.executable).resolve().parent
    else:
        base_dir = Path(__file__).resolve().parent.parent

    # .env is the single source of truth: override any inherited shell variables.
    load_dotenv(base_dir / ".env", override=True)


load_env_file()

from langgraph.types import Command  # noqa: E402

from graph import graph  # noqa: E402
from ui import render_email_preview, render_sent  # noqa: E402


EXIT_WORDS = {"exit", "quit", "bye"}


def show_welcome_banner():
    print(f"{BLUE}{BOLD}=" * 50)
    print(f"       Welcome to your Personal Mail Agent!       ")
    print(f"=" * 50 + f"{RESET}\n")
    print(f"{YELLOW}Type 'exit', 'quit', or 'bye' to close the app.{RESET}\n")


def simulate_loading(message="Processing"):
    """Displays a professional animated text spinner."""
    spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    for _ in range(12):  # Simulates loading for about 1 second
        for frame in spinner:
            sys.stdout.write(f"{CLEAR_LINE}{YELLOW}{frame} {message}...{RESET}")
            sys.stdout.flush()
            time.sleep(0.01)
    sys.stdout.write(f"{CLEAR_LINE}")  # Clears the spinner line when done
    sys.stdout.flush()


def _interrupt_values(result):
    """Return the values carried by any interrupts in a graph result."""
    raw = result.get("__interrupt__") or ()
    return [getattr(item, "value", item) for item in raw]


def _prompt_for(payload) -> str:
    kind = payload.get("kind")

    if kind == "approval":
        render_email_preview(
            payload.get("recipient_email", ""),
            payload.get("email_genre", []),
            payload.get("email_draft", ""),
        )
        return f"{BOLD}{YELLOW}Send this email? yes / no <feedback> ❯ {RESET}"

    if kind == "genre":
        return f"{BOLD}{YELLOW}Your answer ❯ {RESET}"

    return f"{BOLD}{YELLOW}Recipient ❯ {RESET}"


def run_workflow(user_request: str):
    """Run the graph, handling every interrupt until it finishes."""
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    result = graph.invoke(
        {
            "user_request": user_request,
            "email_genre": [],
            "revision_count": 0,
            "status": "started",
        },
        config,
    )

    while True:
        payloads = _interrupt_values(result)
        if not payloads:
            break

        payload = payloads[0]
        if payload.get("kind") != "approval" and payload.get("question"):
            print(f"{GREEN}{BOLD}Agent ❯{RESET} {payload['question']}")

        answer = input(_prompt_for(payload)).strip()

        if not answer:
            continue
        if answer.lower() in EXIT_WORDS:
            return None

        simulate_loading("Thinking")
        result = graph.invoke(Command(resume=answer), config)

    if result.get("agent_message"):
        print(f"{GREEN}{BOLD}Agent ❯{RESET} {result['agent_message']}")

    if result.get("status") == "sent":
        render_sent(
            result.get("recipient_email", ""),
            result.get("email_draft", ""),
        )
    return result


def main():
    show_welcome_banner()

    while True:
        try:
            # Custom styled input prompt
            user_input = input(f"{BOLD}{BLUE}You ❯ {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            # Gracefully handles Ctrl+C or Ctrl+D crashes
            print(f"\n{RED}Goodbye!{RESET}")
            break

        # Skip empty inputs instead of processing them
        if not user_input:
            continue

        # Check exit conditions
        if user_input.lower() in EXIT_WORDS:
            print(f"\n{RED}{BOLD}Goodbye!{RESET}")
            break

        # Visual feedback processing animation
        simulate_loading("Analyzing your request")

        try:
            result = run_workflow(user_input)
        except KeyboardInterrupt:
            print(f"\n{RED}Cancelled.{RESET}\n")
            continue
        except Exception as exc:
            print(f"\n{RED}{BOLD}Error:{RESET} {RED}{exc}{RESET}\n")
            continue

        if result is None:
            print(f"\n{RED}{BOLD}Goodbye!{RESET}")
            break

        print("-" * 50 + "\n")


if __name__ == "__main__":
    main()
