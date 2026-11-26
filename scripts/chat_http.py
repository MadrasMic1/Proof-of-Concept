# scripts/chat_http.py
"""Talk to the RUNNING API's chat endpoints exactly like the app does.

    --site 03340 --user priya.store-manager --say "rf gun 2 keeps dying, raise a ticket"
    --site 03340 --user priya.store-manager --session
    --site 03340 --user priya.store-manager --confirm      (or --cancel, --new)
(each after: uv run python -m scripts.chat_http)

Needs the API (uvicorn app.main:app) and the MCP server (scripts.mcp_server) running.
--confirm / --cancel use the pending Confirm action of the active chat.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

import httpx

from app.agentic.config import get_agentic_settings


def show(data: dict[str, Any]) -> None:
    print(f"RIC> {data.get('answer')}")
    for card in data.get("cards") or []:
        print(f"  [{card['kind']}] {card['title']}")
    pending = data.get("pending_action")
    if pending:
        print(
            f"  [ACTION] Confirm: {pending['short_description']} (priority "
            f"{pending['priority']}, version {pending['version']}, draft {pending['draft_id']})"
        )
    if data.get("suggested_replies"):
        print("  replies: " + " | ".join(data["suggested_replies"]))
    flags = " ".join(f for f in ("busy", "replayed") if data.get(f))
    warnings = "; ".join(data.get("warnings") or [])
    note = f"state {data.get('state')}" + (f" {flags}" if flags else "")
    print(f"  ({note}" + (f" | warnings: {warnings}" if warnings else "") + ")")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--site", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--say", action="append", default=[])
    p.add_argument("--session", action="store_true")
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--cancel", action="store_true")
    p.add_argument("--new", action="store_true")
    args = p.parse_args()
    timeout = get_agentic_settings().agent_app_client_timeout_hint_seconds
    headers = {"X-Site-Id": args.site, "X-User": args.user}
    base = f"{args.api.rstrip('/')}/api/v1/chat"
    with httpx.Client(timeout=timeout, headers=headers) as http:
        if args.new:
            r = http.post(f"{base}/session/new")
            print(r.status_code, json.dumps(r.json(), indent=2)[:600])
        for text in args.say:
            print(f"\nyou> {text}")
            body = {"text": text, "clientMessageId": str(uuid.uuid4())}
            r = http.post(f"{base}/messages", json=body)
            if r.status_code != 200:
                print(f"[HTTP {r.status_code}] {r.text}")
                return 1
            show(r.json())
        if args.confirm or args.cancel:
            session = http.get(f"{base}/session").json()
            pending = session.get("pending_action")
            if not pending:
                print("[FAIL] no pending Confirm action in the active chat")
                return 1
            action = "confirm_incident" if args.confirm else "cancel_draft"
            body = {"action": action, "draftId": pending["draft_id"], "version": pending["version"]}
            r = http.post(f"{base}/actions", json=body)
            if r.status_code != 200:
                print(f"[HTTP {r.status_code}] {r.text}")
                return 1
            print(f"\n[{action}]")
            show(r.json())
        if args.session:
            r = http.get(f"{base}/session")
            data = r.json()
            print(
                f"session {data.get('session_id')} state {data.get('state')} "
                f"role {data.get('role')}"
            )
            for m in data.get("messages") or []:
                print(f"  {m['turn_no']:>3} {m['role']:9} {m['content'][:110]}")
            if data.get("pending_action"):
                print(f"  pending: {data['pending_action']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
