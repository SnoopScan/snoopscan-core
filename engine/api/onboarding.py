"""What a caller without a working key is told, so an agent can pass it on.

A bare "Missing or invalid API key" leaves an agent with nothing to say but
"you need a key", and it will often invent a placeholder. Every no-key answer
says instead exactly how a person gets one — the browser login, or the sign-up
page and the keys page — and how to use it once they have it.
"""

from __future__ import annotations

from typing import Any

from engine.settings import settings

LOGIN_COMMAND = "snoopscan login"
# Routes that need nothing installed first, in the order to try them. A Mac's
# own Python is 3.9, below the package's 3.10, so `pip install` alone was a
# dead end on the most common laptop there is.
LOGIN_ROUTES = (
    ("Node 18+", "npx -y snoopscan@latest login"),
    ("uv", "uvx snoopscan login"),
    ("Python 3.10+", "python3 -m pip install --user snoopscan && snoopscan login"),
)
INSTALL_HINT = "npx -y snoopscan@latest login  (Node 18+), or: uvx snoopscan login"


def urls() -> dict[str, str]:
    base = settings.account_url.rstrip("/")
    return {"signupUrl": f"{base}/register", "keysUrl": f"{base}/app/keys"}


def missing_key_message() -> str:
    u = urls()
    return (
        "No API key was sent. Get a free key (no card needed): run "
        "`npx -y snoopscan@latest login` (or `uvx snoopscan login`), which opens the "
        "browser to sign in or sign up and saves the key, or sign up at "
        f"{u['signupUrl']} and create a key at {u['keysUrl']}, with nothing to "
        "install. Then send it as `Authorization: Bearer <key>`."
    )


def invalid_key_message() -> str:
    u = urls()
    return (
        "That API key is not recognised: it may have been revoked, or copied with a "
        f"space or a line break. Check it at {u['keysUrl']}, or run "
        "`npx -y snoopscan@latest login` to get a fresh one."
    )


def detail() -> dict[str, Any]:
    return {**urls(), "login": LOGIN_COMMAND, "install": INSTALL_HINT}


def mcp_setup_text(endpoint: str, *, had_key: bool) -> str:
    """The answer to an MCP tool call made without a working key.

    Written TO the agent, with the words to say to the person, because the
    agent is the one reading it and the person is the one who has to act. It
    includes what to do when nothing will install: the key is only a web page
    away, and the MCP server itself needs no install at all.
    """
    u = urls()
    why = (
        "The API key this MCP server was added with is not recognised"
        if had_key
        else "This MCP server was added without an API key"
    )
    routes = "\n".join(f"  - {needs}: {cmd}" for needs, cmd in LOGIN_ROUTES)
    return (
        f"{why}, so nothing was fetched and nothing was charged.\n\n"
        "Tell the user:\n"
        "\"To use SnoopScan I need your free API key (free plan, no card). I'll open a "
        "login page in your browser: sign in or create an account, check the code "
        'matches the one I show you, and press Approve. The key is saved for me."\n\n'
        "Then run the login with whichever of these the machine can already run "
        "(check `node --version`, `uv --version`, `python3 --version` first):\n"
        f"{routes}\n"
        "A Mac's built-in Python is 3.9, too old for the package: use npx or uvx there.\n\n"
        "If something fails, run `npx -y snoopscan@latest doctor` (or `uvx snoopscan "
        "doctor`): it says what is wrong and how to fix it. If Node or Python is too old, "
        "never change their system without asking: offer to update it, or skip the "
        "install entirely by saying:\n"
        f'"No problem, we can skip the install. Please sign up at {u["signupUrl"]}, '
        f'create a key at {u["keysUrl"]}, and paste it here."\n\n'
        "Once you have the key, reconnect this server with it (in Claude Code):\n"
        "  claude mcp remove snoopscan\n"
        f"  claude mcp add --transport http snoopscan {endpoint} "
        '--header "Authorization: Bearer <THE KEY>" --scope user\n'
        "After a login, `npx -y snoopscan@latest config get api_key` (or `uvx snoopscan "
        "config get api_key`) prints the key, so fill it in yourself rather than asking "
        "them to paste it. Then ask them to restart Claude Code, or run /mcp, so the new "
        "connection is picked up.\n\n"
        "If they use the Claude app (desktop, claude.ai or phone) rather than Claude Code, "
        "there is no terminal step at all. Tell them:\n"
        '"In Claude, open Settings, then Connectors. Click + and choose Add custom '
        f"connector. Name it SnoopScan, paste {endpoint}-oauth and click Add. Then click "
        'Connect, log in or create a free account, and press Approve."'
    )
