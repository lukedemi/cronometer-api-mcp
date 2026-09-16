#!/usr/bin/env python3
"""Verify the exercise write path against a live Cronometer account.

Probed 2026-09-16 against a real account. **Add and edit are settled**, and
the v3 diary-entries collection turned out not to be the write path at all:

    POST   /api/v3/user/{id}/diary-entries   400 for every shape tried,
           including a faithful clone of a row Cronometer wrote itself
    PUT    /api/v3/user/{id}/diary-entries   405
    DELETE /api/v3/user/{id}/diary-entries   400 with an exercise entry —
           though the same call with *serving* entries is what
           `delete_entries` has always used and it answers 204

    POST   /api/v2/add_exercise    ✓ 200, returns {"id": <exerciseId>}
    POST   /api/v2/edit_exercise   ✓ 200, calories stayed pinned

So that collection handles servings and does not know about exercise, and
exercise lives entirely in the older v2 verb-per-action API.

**Deleting is the one operation still unknown**, which is what this script is
now mostly for. It confirms add and edit with the exact body the client
sends — not the clone that happened to win the first probe — and then tries
candidate delete endpoints, re-reading the diary after each so that an
endpoint answering 200 while changing nothing is not mistaken for success.

    uv run python scripts/probe_exercise.py [YYYY-MM-DD]

Credentials come from `.env` in the repo root (gitignored), the same file
`server.main()` reads, or from the environment if already set there. Prefer the
file: a password with a `!`, a `$` or a space in it does not survive being
typed on a shell command line, and Cronometer's answer to a mangled password is
indistinguishable from its answer to a wrong one.

    printf 'CRONOMETER_USERNAME=%s\\nCRONOMETER_PASSWORD=%s\\n' \\
        'you@example.com' 'the password' > .env

It sweeps the last few days for rows left behind by earlier runs and tries to
remove those too, so a failed probe does not leave litter accumulating in the
diary. Read the last section: if it says something survived, delete it in the
app, because until a delete endpoint is found nothing here can.
"""

import json
import logging
import sys
from datetime import date, timedelta

sys.path.insert(0, "src")

from dotenv import find_dotenv, load_dotenv  # noqa: E402

from cronometer_api_mcp.client import CronometerClient  # noqa: E402

# Same as server.main(): override=False keeps a real environment variable
# authoritative over the file, so an explicit `FOO=bar uv run ...` still wins.
_dotenv = find_dotenv(usecwd=True)
if _dotenv:
    load_dotenv(_dotenv, override=False)

PROBE_NAME = "zz probe delete me"
PROBE_KCAL = 123
PROBE_MINUTES = 45

# How far back to sweep for rows earlier runs could not delete.
SWEEP_DAYS = 7


def show(rows, indent="      "):
    if not rows:
        print(f"{indent}(none)")
    for r in rows:
        print(
            f"{indent}id={r.get('exerciseId')} "
            f"name={r.get('name')!r} "
            f"calories={r.get('calories')} "
            f"minutes={r.get('minutes')} "
            f"override={r.get('calorieOverride')} "
            f"day={r.get('day')}"
        )


def probe_rows(client, day):
    """Every row this script has ever left, across the sweep window."""
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


def delete_candidates(client, day, entry):
    """(label, how) pairs for removing `entry`, most-likely first.

    Ordered by what the rest of this API looks like. Every v2 endpoint the
    client already uses is verb_noun — add_food, add_serving, get_diary,
    set_complete, edit_exercise — so delete_exercise is the shape to try
    first. The v3 attempts are last and are about the *fields*: DELETE there
    demonstrably works for servings, so it is worth asking whether it refused
    our row because of a null `source` rather than because it cannot take an
    exercise at all.
    """
    eid = entry.get("exerciseId")
    out = []

    def v2(label, endpoint, body):
        def send():
            try:
                return 200, json.dumps(client._request(endpoint, body))[:200]
            except Exception as exc:  # noqa: BLE001 — that IS the result
                return "err", str(exc)[:200]

        out.append((label, send))

    def v3(label, path, body):
        def send():
            resp = client._request_v3("DELETE", path, json_body=body)
            return resp.status_code, resp.text[:200]

        out.append((label, send))

    cfg = {"config": {"call_version": 2}}
    v2(
        "v2 delete_exercise {exercise}",
        "/api/v2/delete_exercise",
        {"exercise": entry, **cfg},
    )
    v2("v2 delete_exercise {id}", "/api/v2/delete_exercise", {"id": eid, **cfg})
    v2(
        "v2 remove_exercise {exercise}",
        "/api/v2/remove_exercise",
        {"exercise": entry, **cfg},
    )
    v2(
        "v2 delete_exercises {exercises:[]}",
        "/api/v2/delete_exercises",
        {"exercises": [entry], **cfg},
    )

    # Soft delete: some diaries mark rather than remove.
    v2(
        "v2 edit_exercise {deleted:true}",
        "/api/v2/edit_exercise",
        {"exercise": {**entry, "deleted": True}, **cfg},
    )

    # Was the v3 refusal about the null `source`, not about exercise?
    no_nulls = {k: v for k, v in entry.items() if v is not None}
    v3(
        "v3 DELETE /diary-entries (nulls stripped)",
        "/diary-entries",
        {"diaryEntries": [no_nulls]},
    )
    v3(
        "v3 DELETE /diary-entries (source='')",
        "/diary-entries",
        {"diaryEntries": [{**entry, "source": "", "externalId": ""}]},
    )
    v3(f"v3 DELETE /diary-entries/{eid} (path, no body)", f"/diary-entries/{eid}", None)
    return out


def main():
    logging.basicConfig(level=logging.WARNING, format="  %(levelname)-7s %(message)s")
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    if _dotenv:
        print(f"  credentials from {_dotenv}")
    client = CronometerClient()

    print(f"\n[1] exercise rows on {day}")
    show(client.get_exercises(day))

    print("\n[2] add — the exact body the client sends, not a clone")
    created = client.add_exercise(
        name=PROBE_NAME, calories=PROBE_KCAL, minutes=PROBE_MINUTES, day=day
    )
    print(f"      exerciseId returned: {created.get('exerciseId')}")
    mine = [r for r in client.get_exercises(day) if r.get("name") == PROBE_NAME]
    show(mine)
    if not mine:
        print("      ✗ did not land — the minimal body is NOT accepted, only")
        print("        the cloned one. Send me this output.")
        return 1
    if round(abs(mine[0].get("calories") or 0)) != PROBE_KCAL:
        print(f"      ✗ calories came back {mine[0].get('calories')} — recomputed")
    if not mine[0].get("calorieOverride"):
        print("      ✗ calorieOverride is false — the number is not pinned")

    print("\n[3] edit to 456 kcal")
    try:
        client.update_exercise(mine[0]["exerciseId"], calories=456, day=day)
        after = [r for r in client.get_exercises(day) if r.get("name") == PROBE_NAME]
        show(after)
        if after and round(abs(after[0].get("calories") or 0)) != 456:
            print("      ✗ the edit did not take")
    except Exception as exc:  # noqa: BLE001 — report and carry on to delete
        print(f"      ✗ edit failed: {exc}")

    print(f"\n[4] delete — trying candidates, sweeping back {SWEEP_DAYS} days")
    winner = None
    while True:
        rows = probe_rows(client, day)
        if not rows:
            break
        target_day, target = rows[0]
        print(f"      target: id={target.get('exerciseId')} on {target_day}")
        if winner is not None:
            # Already know what works — just use it on the leftovers.
            label, send = next(
                (
                    c
                    for c in delete_candidates(client, target_day, target)
                    if c[0] == winner
                ),
                (None, None),
            )
            if send is None or send()[0] not in (200, 201, 204):
                print("      ✗ the winning endpoint did not clear a leftover")
                break
            continue

        progressed = False
        for label, send in delete_candidates(client, target_day, target):
            try:
                status, body = send()
            except Exception as exc:  # noqa: BLE001 — that IS the result
                status, body = "raise", str(exc)[:200]
            still = any(
                str(r.get("exerciseId")) == str(target.get("exerciseId"))
                for _, r in probe_rows(client, target_day)
            )
            ok = status in (200, 201, 204) and not still
            # A 200 that changed nothing is the failure this re-read catches.
            note = (
                ""
                if ok
                else (
                    "  (200 but the row is still there)"
                    if status in (200, 201, 204)
                    else ""
                )
            )
            print(f"      {'✓' if ok else '✗'} {label}{note}")
            print(f"          HTTP {status}  {body}")
            if ok:
                winner, progressed = label, True
                break
        if not progressed:
            break

    print("\n[5] verdict")
    if winner:
        print(f"      delete works via: {winner}")
    else:
        print("      NO delete endpoint found. Add and edit are usable; the")
        print("      ledger can still run, but it can never retract a row.")

    leftovers = probe_rows(client, day)
    if leftovers:
        print(
            f"\n[6] {len(leftovers)} probe row(s) left in the diary — "
            f"DELETE BY HAND in the app:"
        )
        for d, r in leftovers:
            print(f"      {d}  id={r.get('exerciseId')}  {r.get('name')!r}")
        return 1
    print("\n[6] nothing left behind")
    return 0


if __name__ == "__main__":
    sys.exit(main())
