#!/usr/bin/env python3
"""Find how Cronometer deletes an exercise entry. Add and edit are settled.

Probed against a live account, 2026-09-16:

    POST   /api/v2/add_exercise             ✓ 200, returns {"id": <exerciseId>}
    POST   /api/v2/edit_exercise            ✓ 200, calories stay pinned
    POST   /api/v3/…/diary-entries          400 for every shape, incl. a clone
                                               of a row Cronometer itself wrote
    PUT    /api/v3/…/diary-entries          405
    DELETE /api/v3/…/diary-entries          400 with an exercise entry, though
                                               the same call with *servings* is
                                               what delete_entries uses, and 204s
    DELETE /api/v3/…/diary-entries/{id}     404
    v2 delete_exercise / remove_exercise /
       delete_exercises                     200 {"result":"FAIL",
                                                 "error":"Invalid Command"}
    v2 edit_exercise {deleted: true}        200, and the row stayed put

**"Invalid Command" is the useful part.** The v2 API is a command dispatcher,
so a name it does not know answers that — which makes an unknown endpoint
distinguishable from a real one that merely disliked the body, and makes
guessing *names* nearly free. One request per candidate, and the answer is
unambiguous. That is most of what this script now does.

`get_diary` is included as a positive control: if it also came back "Invalid
Command", the oracle would be measuring something else and every ✗ below would
be meaningless.

The v3 attempts left are about the schema rather than the path. DELETE there
demonstrably accepts *serving* objects fetched from the v2 diary, so v2 objects
are v3-compatible in general — which makes it worth asking whether it rejects
an exercise because of its `type` value or a field name, rather than because it
cannot take one at all.

    uv run python scripts/probe_exercise.py [YYYY-MM-DD]

**This run creates no new rows if any are already there.** Earlier runs left
two, and a probe that adds one every time it fails to find a delete is a probe
that litters. It targets what exists, and only creates a row if the diary has
none to work with.

Credentials come from `.env` in the repo root (gitignored) — see the README.
"""

import json
import logging
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

# Every v2 name worth asking about. Cheap: one request each, and the answer is
# "Invalid Command" or it is not. `get_diary` is the positive control.
V2_NAMES = [
    "get_diary",  # control — must NOT read "Invalid Command"
    "delete_exercise",
    "remove_exercise",
    "delete_exercises",
    "del_exercise",
    "exercise_delete",
    "delete_entry",
    "delete_entries",
    "remove_entry",
    "remove_entries",
    "delete_diary_entry",
    "delete_diary_entries",
    "remove_diary_entry",
    "delete_serving",
    "remove_serving",
    "delete_servings",
    "delete_item",
    "remove_item",
    "delete",
    "remove",
]

INVALID = "Invalid Command"


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


def name_sweep(client, day, entry):
    """Ask the v2 dispatcher which command names exist at all."""
    live = []
    for name in V2_NAMES:
        body = (
            {"day": client._format_day(day), "config": {"call_version": 1}}
            if name == "get_diary"
            else {
                "exercise": entry,
                "id": entry.get("exerciseId"),
                "config": {"call_version": 2},
            }
        )
        try:
            data = client._request(f"/api/v2/{name}", body)
            text = json.dumps(data)
        except Exception as exc:  # noqa: BLE001 — that IS the result
            text = f"raised: {exc}"
        known = INVALID not in text
        flag = "•" if known else " "
        print(f"      {flag} {name:24} {text[:110]}")
        if known:
            live.append(name)
    return live


def v3_variants(client, entry):
    """Schema and path variants for the v3 DELETE, which takes servings fine."""
    eid = entry.get("exerciseId")
    out = []

    def add(label, path, body):
        out.append((label, path, body))

    add(
        "type='EXERCISE'",
        "/diary-entries",
        {"diaryEntries": [{**entry, "type": "EXERCISE"}]},
    )
    add(
        "type='exercise'",
        "/diary-entries",
        {"diaryEntries": [{**entry, "type": "exercise"}]},
    )
    add(
        "id alongside exerciseId",
        "/diary-entries",
        {"diaryEntries": [{**entry, "id": eid}]},
    )
    add("wrapper 'exercises'", "/diary-entries", {"exercises": [entry]})
    add("collection /exercises", "/exercises", {"exercises": [entry]})
    add("collection /exercise-entries", "/exercise-entries", {"diaryEntries": [entry]})
    add("path /exercises/{id}", f"/exercises/{eid}", None)

    results = []
    for label, path, body in out:
        try:
            resp = client._request_v3("DELETE", path, json_body=body)
            status, text = resp.status_code, resp.text[:110]
        except Exception as exc:  # noqa: BLE001 — that IS the result
            status, text = "raise", str(exc)[:110]
        ok = status in (200, 201, 204)
        print(f"      {'✓' if ok else '✗'} {label:30} HTTP {status}  {text}")
        results.append((label, ok))
    return [label for label, ok in results if ok]


def main():
    logging.basicConfig(level=logging.WARNING, format="  %(levelname)-7s %(message)s")
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    if _dotenv:
        print(f"  credentials from {_dotenv}")
    client = CronometerClient()

    print(f"\n[1] probe rows already in the diary, back {SWEEP_DAYS} days")
    rows = probe_rows(client, day)
    show([r for _, r in rows])

    if rows:
        target_day, target = rows[0]
        print(f"      targeting id={target.get('exerciseId')} on {target_day}")
        print("      (no new row created — earlier runs left these)")
    else:
        print("\n[1b] nothing to target — creating one row to work on")
        created = client.add_exercise(
            name=PROBE_NAME, calories=123, minutes=45, day=day
        )
        target_day, target = day, created
        print(f"      created id={created.get('exerciseId')}")

    print("\n[2] which v2 command names exist? (• = real, blank = Invalid Command)")
    live = name_sweep(client, target_day, target)

    print("\n[3] v3 DELETE schema and path variants")
    v3_ok = v3_variants(client, target)

    print("\n[4] did anything actually remove the row?")
    left = [
        r
        for _, r in probe_rows(client, target_day)
        if str(r.get("exerciseId")) == str(target.get("exerciseId"))
    ]
    gone = not left
    print(f"      target row {'GONE' if gone else 'still present'}")

    print("\n[5] verdict")
    unexpected = [n for n in live if n != "get_diary"]
    if unexpected:
        print(f"      v2 names that exist: {unexpected}")
    else:
        print("      no v2 delete command name exists (control get_diary did")
        print("      answer, so the oracle is sound)")
    if v3_ok:
        print(f"      v3 variants accepted: {v3_ok}")
    if gone:
        print("      and the row is gone — that is the delete path.")
    else:
        print("      nothing deletes an exercise entry through this API.")
        print("      Ship without it: editing a row to 0 kcal has exactly the")
        print("      same effect on the day's budget, and the ledger never")
        print("      needs to retract a row it can instead zero.")

    remaining = probe_rows(client, day)
    if remaining:
        print(f"\n[6] {len(remaining)} probe row(s) to delete BY HAND in the app:")
        for d, r in remaining:
            print(f"      {d}  id={r.get('exerciseId')}")
    else:
        print("\n[6] nothing left behind")
    return 0


if __name__ == "__main__":
    sys.exit(main())
