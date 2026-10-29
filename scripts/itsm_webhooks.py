# scripts/itsm_webhooks.py
"""Create, show or disable the GLPI webhooks that call the RIC API (Phase 17).

    uv run python -m scripts.itsm_webhooks              create / update the 4 RIC webhooks
    uv run python -m scripts.itsm_webhooks --status     show them (and the Phase 4 test ones)
    uv run python -m scripts.itsm_webhooks --disable    switch the RIC webhooks off

The webhooks post to ITSM_WEBHOOK_BASE_URL + /api/v1/integrations/glpi/webhooks/... with
GLPI's default payload, 3 tries, signed with GLPI_WEBHOOK_SECRET. The Phase 4 test webhooks
("RIC test - ...") are switched off. Uses the GLPI API V1 (Webhook items, as in Phase 4).
GLPI can only call the API when it runs with --host 0.0.0.0 (see README). Safe to run again:
it updates the existing webhooks (the secret is written every time: GLPI never returns it).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from app.core.config import get_settings
from app.itsm.errors import ItsmConfigError, ItsmError
from app.itsm.factory import build_itsm_client
from app.itsm.webhooks import webhook_url

WEBHOOKS = (
    ("RIC - Ticket new", "Ticket", "new"),
    ("RIC - Ticket update", "Ticket", "update"),
    ("RIC - Followup new", "ITILFollowup", "new"),
    ("RIC - Solution new", "ITILSolution", "new"),
)
TEST_PREFIX = "RIC test - "
OWN_NAMES = {name for name, _, _ in WEBHOOKS}


def fields_for(name: str, itemtype: str, event: str, url: str, secret: str) -> dict[str, Any]:
    return {
        "name": name,
        "entities_id": 0,
        "is_recursive": 1,
        "itemtype": itemtype,
        "event": event,
        "url": url,
        "http_method": "post",
        "sent_try": 3,
        "is_active": 1,
        "use_default_payload": 1,
        "payload": "",
        "log_in_item_history": 1,
        "save_response_body": 0,
        "secret": secret,
    }


def show(hooks: list[dict[str, Any]], base_url: str) -> bool:
    """Print the RIC and test webhooks; False when a RIC webhook is missing or wrong."""
    expected = {name: (itemtype, event) for name, itemtype, event in WEBHOOKS}
    ok = True
    for hook in sorted(hooks, key=lambda h: int(h.get("id") or 0)):
        name = str(hook.get("name") or "")
        if name not in OWN_NAMES and not name.startswith(TEST_PREFIX):
            continue
        problems = []
        if name in expected:
            itemtype, event = expected[name]
            if (hook.get("itemtype"), hook.get("event")) != (itemtype, event):
                problems.append("itemtype/event")
            if hook.get("url") != webhook_url(base_url, itemtype, event):
                problems.append("url")
        ok = ok and not problems
        print(
            f"  id {hook.get('id'):<4} {name:<26} active={hook.get('is_active')} "
            f"{hook.get('itemtype')}/{hook.get('event')}"
            + (f"  MISMATCH: {', '.join(problems)}" if problems else "")
        )
        print(f"           url={hook.get('url')}")
    for name in sorted(OWN_NAMES - {str(h.get("name")) for h in hooks}):
        ok = False
        print(f"  MISSING  {name}")
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the GLPI webhooks that call RIC.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="only show the webhooks")
    mode.add_argument("--disable", action="store_true", help="switch the RIC webhooks off")
    args = parser.parse_args()
    settings = get_settings()
    base_url = settings.itsm_webhook_base_url
    secret = settings.glpi_webhook_secret
    if not args.status and not args.disable and secret is None:
        print("[FAIL] GLPI_WEBHOOK_SECRET is not set in .env", file=sys.stderr)
        return 1
    try:
        client = build_itsm_client(settings)
    except ItsmConfigError as exc:
        print(f"[FAIL] configuration: {exc}", file=sys.stderr)
        return 1
    print(f"RIC API as seen from GLPI: {base_url}")
    try:
        hooks = await client.list_webhooks()
        if args.disable:
            for hook in hooks:
                if hook.get("name") in OWN_NAMES and int(hook.get("is_active") or 0):
                    await client.update_webhook(int(hook["id"]), {"is_active": 0})
                    print(f"  disabled {hook['id']} {hook['name']}")
        elif not args.status:
            for name, itemtype, event in WEBHOOKS:
                fields = fields_for(
                    name,
                    itemtype,
                    event,
                    webhook_url(base_url, itemtype, event),
                    secret.get_secret_value(),
                )
                same = [h for h in hooks if h.get("name") == name]
                if not same:
                    print(f"  created  {name} (id {await client.add_webhook(fields)})")
                    continue
                await client.update_webhook(int(same[0]["id"]), fields)
                note = f" (WARN: {len(same)} webhooks with this name)" if len(same) > 1 else ""
                print(f"  updated  {name} (id {same[0]['id']}){note}")
            for hook in hooks:
                name = str(hook.get("name") or "")
                if name.startswith(TEST_PREFIX) and int(hook.get("is_active") or 0):
                    await client.update_webhook(int(hook["id"]), {"is_active": 0})
                    print(f"  disabled {hook['id']} {name} (Phase 4 test webhook)")
        print("== webhooks")
        ok = show(await client.list_webhooks(), base_url)
    except ItsmError as exc:
        print(f"[FAIL] {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()
    print("status: " + ("OK" if ok else "CHECK THE LINES ABOVE"))
    return 0 if ok or args.disable else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
