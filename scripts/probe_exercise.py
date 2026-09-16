#!/usr/bin/env python3
"""Pin down `del_exercise`, the last unknown in the exercise write path.

Probed against a live account, 2026-09-16. Settled:

    POST /api/v2/add_exercise    ✓ 200, returns {"id": <exerciseId>}
    POST /api/v2/edit_exercise   ✓ 200, calories stay pinned
    POST /api/v2/del_exercise    EXISTS — it answered
        {"result":"FAIL","error":"JSONObject[\\"exerciseId\\"] not found."}

Everything v3 is a dead end for exercise: POST 400 for every shape including a
clone of a row Cronometer wrote itself, PUT 405, DELETE 400, and /exercises,
/exercise-entries and /diary-entries/{id} all 404. That collection handles
servings and does not know about exercise.

**The v2 dispatcher answers two distinguishable ways, and both are useful.**
An unknown command says `Invalid Command`; a real one with a bad body names
the field it wanted, as `JSONObject["x"] not found.` The first found
`del_exercise` where `delete_exercise`, `remove_exercise` and six other
guesses were all wrong. The second means the required body does not have to be
guessed at all: send the minimum, read which key it asks for, add that key,
send again. This script does exactly that loop and prints the body it
converged on.

    uv run python scripts/probe_exercise.py [YYYY-MM-DD]

It then uses the discovered shape to remove every probe row earlier runs left
behind. Credentials come from `.env` in the repo root — see the README.
"""

import json
import logging
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, "src")

from dotenv import find_dotenv, load_dotenv  # noqa: E402

from cronometer_api_mcp.client import CronometerClient  # noqa: E402

_dotenv = find_dotenv(usecwd=True)
if _dotenv:
    load_dotenv(_dotenv, override=False)

PROBE_NAME = "zz probe delete me"
SWEEP_DAYS = 7

# `JSONObject["exerciseId"] not found.` -- the server naming its own
# requirement, which is the whole mechanism this script runs on.
MISSING = re.compile(r'JSONObject\["([^"]+)"\] not found')

# Now that the naming style is known to be abbreviated, the same sweep is
# worth re-running over `del_` forms: delete_entries currently goes through v3,
# and a v2 sibling of del_exercise would be more consistent.
MORE_NAMES = ["del_serving", "del_food", "del_entry", "del_biometric", "del_note"]


def show(rows, indent="      "):
    if not rows:
        print(f"{indent}(none)")
    for r in rows:
        print(
            f"{indent}id={r.get('exerciseId')} "
            f"name={r.get('name')!r} "
            f"calories={r.get('calories')} "
            f"day={r.get('day')}"
        )


def probe_rows(client, day):
    """(day, row) for every row earlier runs left, across the sweep window."""
    found = []
    for back in range(SWEEP_DAYS + 1):
        d = day - timedelta(days=back)
        try:
            found += [
                (d, r) for r in client.get_exercises(d) if r.get("name") == PROBE_NAME
            ]
        except Exception as exc:  # noqa: BLE001 — one bad day is not fatal
            print(f"      ({d}: {exc})")
    return found


def value_for(key, entry, client, day):
    """What to put under a key the server has just asked for.

    The entry itself first -- it came from Cronometer and so uses Cronometer's
    own names and types. The rest are the handful of fields that live on the
    request rather than on the row.
    """
    if key in entry:
        return entry[key]
    return {
        "userId": client._user_id,
        "day": client._format_day(day),
        "id": entry.get("exerciseId"),
        "exerciseId": entry.get("exerciseId"),
    }.get(key)


def discover_body(client, day, entry, endpoint="/api/v2/del_exercise"):
    """Send, read which key it wanted, add it, send again.

    Returns (body, response) on success, or (body, None) when it stops making
    progress -- which is either an error that is not a missing field, or a key
    nothing here knows how to fill.
    """
    body = {"config": {"call_version": 2}}
    for step in range(10):
        try:
            data = client._request(endpoint, dict(body))
        except Exception as exc:  # noqa: BLE001 — that IS the result
            print(f"      step {step}: raised {exc}")
            return body, None

        err = data.get("error") if isinstance(data, dict) else None
        if not err:
            print(f"      step {step}: ✓ {json.dumps(data)[:120]}")
            return body, data

        print(f"      step {step}: {json.dumps(data)[:120]}")
        match = MISSING.search(err)
        if not match:
            return body, None  # not a missing-field error
        key = match.group(1)
        value = value_for(key, entry, client, day)
        if value is None and key not in entry:
            print(f"      step {step}: asked for {key!r} and nothing here has it")
            return body, None
        print(f"                  -> adding {key}={value!r}")
        body[key] = value
    return body, None


def main():
    logging.basicConfig(level=logging.WARNING, format="  %(levelname)-7s %(message)s")
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    if _dotenv:
        print(f"  credentials from {_dotenv}")
    client = CronometerClient()

    print(f"\n[1] probe rows in the diary, back {SWEEP_DAYS} days")
    rows = probe_rows(client, day)
    show([r for _, r in rows])
    if not rows:
        print("\n      nothing to delete — creating one row to work on")
        created = client.add_exercise(name=PROBE_NAME, calories=123, day=day)
        rows = [(day, created)]
        print(f"      created id={created.get('exerciseId')}")

    target_day, target = rows[0]
    print("\n[2] del_exercise — asking it what body it wants")
    print(f"      target id={target.get('exerciseId')} on {target_day}")
    body, ok = discover_body(client, target_day, target)

    print("\n[3] did the row actually go?")
    still = [
        r
        for _, r in probe_rows(client, target_day)
        if str(r.get("exerciseId")) == str(target.get("exerciseId"))
    ]
    gone = not still
    print(f"      target row {'GONE' if gone else 'still present'}")

    if gone:
        shape = {k: v for k, v in body.items() if k != "config"}
        print(f"\n[4] the body del_exercise needs: {json.dumps(shape)}")
        remaining = probe_rows(client, day)
        if remaining:
            print(f"      clearing {len(remaining)} leftover row(s)")
            for d, r in remaining:
                out = {k: value_for(k, r, client, d) for k in shape}
                out["config"] = {"call_version": 2}
                try:
                    client._request("/api/v2/del_exercise", out)
                    print(f"      removed {r.get('exerciseId')}")
                except Exception as exc:  # noqa: BLE001
                    print(f"      ✗ {r.get('exerciseId')}: {exc}")
    else:
        print(f"\n[4] no luck. Last body tried: {json.dumps(body)}")
        if ok is None:
            print("      The loop stopped because the error was not a missing")
            print("      field. That error text is the thing to read.")

    print("\n[5] other `del_` names, now that the naming style is known")
    for name in MORE_NAMES:
        try:
            data = client._request(f"/api/v2/{name}", {"config": {"call_version": 2}})
            text = json.dumps(data)
        except Exception as exc:  # noqa: BLE001 — that IS the result
            text = f"raised: {exc}"
        known = "Invalid Command" not in text
        print(f"      {'•' if known else ' '} {name:16} {text[:100]}")

    left = probe_rows(client, day)
    if left:
        print(f"\n[6] {len(left)} probe row(s) still in the diary:")
        for d, r in left:
            print(f"      {d}  id={r.get('exerciseId')}")
        return 1
    print("\n[6] nothing left behind")
    return 0


if __name__ == "__main__":
    sys.exit(main())
