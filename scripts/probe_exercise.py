#!/usr/bin/env python3
"""Find the exercise write shape against a live Cronometer account.

The read side of this API was reverse-engineered from real payloads; the write
side for exercise entries was not. What is now known:

  * `DELETE /api/v3/user/{id}/diary-entries` works — `delete_entries` has been
    using it, and Cronometer answers 204.
  * `POST` on that same collection **exists**: it answers **400 "Not able to
    deserialize data provided."**, not 404 or 405. So the endpoint is right
    and the body is wrong.

A 400 for one body tells you nothing about the others, and finding the right
one an edit at a time is a round trip each. So this tries a *matrix* of
candidate bodies in one run and reports what Cronometer said to each, stopping
at the first that is accepted.

The strongest candidate is not invented: it is **a real exercise row from the
rider's own diary, cloned**. Whatever fields Cronometer's deserializer insists
on, an object it produced itself has them. The minimal bodies are there to say,
afterwards, which of those fields were actually load-bearing.

    uv run python scripts/probe_exercise.py [YYYY-MM-DD]

Credentials come from `.env` in the repo root (gitignored), the same file
`server.main()` reads, or from the environment if already set there. Prefer the
file: a password with a `!`, a `$` or a space in it does not survive being
typed on a shell command line, and Cronometer's answer to a mangled password is
indistinguishable from its answer to a wrong one.

    printf 'CRONOMETER_USERNAME=%s\\nCRONOMETER_PASSWORD=%s\\n' \\
        'you@example.com' 'the password' > .env

**It always cleans up after itself**, including when a step fails — but read
the last section of the output rather than assuming, and if it says something
was left behind, delete it in the app.
"""

import copy
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

# How far back to look for a real exercise row to clone. Wearable rows are
# daily, so a fortnight finds one unless every tracker has been disconnected
# for that long.
TEMPLATE_DAYS = 14


def show(rows):
    if not rows:
        print("      (no exercise rows)")
    for r in rows:
        print(
            f"      id={r.get('exerciseId')} "
            f"name={r.get('name')!r} "
            f"calories={r.get('calories')} "
            f"minutes={r.get('minutes')} "
            f"override={r.get('calorieOverride')} "
            f"activityId={r.get('activityId')} "
            f"source={r.get('source')}"
        )


def find_template(client, day):
    """A real exercise row from the diary, to clone. None if there are none."""
    for back in range(0, TEMPLATE_DAYS + 1):
        d = day - timedelta(days=back)
        try:
            rows = client.get_exercises(d)
        except Exception as exc:  # noqa: BLE001 — a missing day is not fatal
            print(f"      ({d}: {exc})")
            continue
        if rows:
            print(f"      found one on {d}:")
            print(f"      {json.dumps(rows[0], indent=6, default=str)}")
            return rows[0]
    return None


def minimal_entry(client, day):
    """What the client currently sends — the body that got the 400."""
    return {
        "type": "Exercise",
        "userId": client._user_id,
        "day": day.isoformat(),
        "name": PROBE_NAME,
        "activityId": 0,
        "activitySpecId": 0,
        "minutes": PROBE_MINUTES,
        "calories": -float(PROBE_KCAL),
        "calorieOverride": True,
        "weight": 0,
        "order": 0,
        "meta": {},
    }


def cloned_entry(template, client, day, *, drop_id=False, keep_source=True):
    """A real row with our values written over it.

    The point is to change as little as possible: every field left alone is a
    field Cronometer itself chose to include, and if the clone is accepted the
    minimal bodies below say which of them mattered.
    """
    entry = copy.deepcopy(template)
    entry["day"] = day.isoformat()
    entry["name"] = PROBE_NAME
    entry["minutes"] = PROBE_MINUTES
    entry["calories"] = -float(PROBE_KCAL)
    entry["calorieOverride"] = True
    entry["userId"] = client._user_id
    if drop_id:
        entry.pop("exerciseId", None)
    else:
        entry["exerciseId"] = None
    if not keep_source:
        # Ours is not synced from anywhere, and claiming it was could make
        # Cronometer's own sync overwrite or delete the row later.
        entry.pop("source", None)
        entry.pop("externalId", None)
    return entry


def candidates(client, day, template):
    """(label, how) pairs, most-likely first. `how` sends and returns a
    (status, body) pair so every attempt reports the same way."""
    out = []

    def v3(label, body):
        def send():
            resp = client._request_v3("POST", "/diary-entries", json_body=body)
            return resp.status_code, resp.text[:300]

        out.append((label, send))

    def v2(label, endpoint, body):
        def send():
            try:
                data = client._request(endpoint, body)
                return 200, json.dumps(data)[:300]
            except Exception as exc:  # noqa: BLE001 — that IS the result
                return "err", str(exc)[:300]

        out.append((label, send))

    if template is not None:
        clone = cloned_entry(template, client, day, keep_source=False)
        clone_id = cloned_entry(template, client, day, drop_id=True, keep_source=False)
        clone_src = cloned_entry(template, client, day, keep_source=True)
        v3("v3 {diaryEntries:[clone]}", {"diaryEntries": [clone]})
        v3("v3 {diaryEntries:[clone, no exerciseId key]}", {"diaryEntries": [clone_id]})
        v3("v3 [clone] (bare array)", [clone])
        v3("v3 clone (bare object)", clone)
        v3("v3 {diaryEntries:[clone, source kept]}", {"diaryEntries": [clone_src]})
        v2(
            "v2 /api/v2/add_exercise {exercise: clone}",
            "/api/v2/add_exercise",
            {"exercise": clone, "config": {"call_version": 2}},
        )

    minimal = minimal_entry(client, day)
    v3("v3 {diaryEntries:[minimal]}", {"diaryEntries": [minimal]})
    v3("v3 [minimal] (bare array)", [minimal])
    v2(
        "v2 /api/v2/add_exercise {exercise: minimal}",
        "/api/v2/add_exercise",
        {"exercise": minimal, "config": {"call_version": 2}},
    )
    return out


def main():
    logging.basicConfig(level=logging.WARNING, format="  %(levelname)-7s %(message)s")
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    if _dotenv:
        print(f"  credentials from {_dotenv}")
    client = CronometerClient()
    winner = None

    try:
        print(f"\n[1] exercise rows already on {day}")
        show(client.get_exercises(day))

        print(
            f"\n[2] looking for a real exercise row to clone, back {TEMPLATE_DAYS} days"
        )
        template = find_template(client, day)
        if template is None:
            print("      none found — only the minimal bodies can be tried, and")
            print("      if they all fail there is nothing here to learn the")
            print("      required fields from. Re-run on a day with a wearable")
            print("      row on it, before disconnecting the tracker.")

        print("\n[3] trying candidate bodies, stopping at the first accepted")
        for label, send in candidates(client, day, template):
            try:
                status, body = send()
            except Exception as exc:  # noqa: BLE001 — that IS the result
                status, body = "raise", str(exc)[:300]
            ok = status in (200, 201, 204)
            print(f"      {'✓' if ok else '✗'} {label}")
            print(f"          HTTP {status}  {body}")
            if ok:
                winner = label
                break

        if winner is None:
            print("\n[4] nothing was accepted")
            print("      Send me this whole output — the error text differs")
            print("      between shapes and that is the signal.")
            return 1

        print(f"\n[4] accepted: {winner}")
        print("      reading back")
        mine = [r for r in client.get_exercises(day) if r.get("name") == PROBE_NAME]
        show(mine)
        if not mine:
            print("      ✗ accepted but did not land — nothing below is meaningful")
            return 1

        entry_id = mine[0].get("exerciseId")
        if entry_id is None:
            print("      ✗ no exerciseId — update and delete have nothing to act on")
        if round(abs(mine[0].get("calories") or 0)) != PROBE_KCAL:
            print(f"      ✗ calories came back {mine[0].get('calories')} — recomputed")
        if not mine[0].get("calorieOverride"):
            print("      ✗ calorieOverride is false — the number is not pinned")

        if entry_id is not None:
            print("\n[5] update to 456 kcal")
            try:
                client.update_exercise(entry_id, calories=456, day=day)
                after = [
                    r for r in client.get_exercises(day) if r.get("name") == PROBE_NAME
                ]
                show(after)
                if after and round(abs(after[0].get("calories") or 0)) != 456:
                    print("      ✗ the update did not take")
            except Exception as exc:  # noqa: BLE001 — report, still clean up
                print(f"      ✗ update failed: {exc}")
                print("      (PUT may need a different shape from POST)")
        return 0

    finally:
        print("\n[6] cleaning up")
        try:
            leftovers = [
                r for r in client.get_exercises(day) if r.get("name") == PROBE_NAME
            ]
        except Exception as exc:  # noqa: BLE001 — cleanup is best effort
            print(f"      ✗ could not re-read the diary: {exc}")
            print(f"      CHECK {day} IN THE APP for a row named {PROBE_NAME!r}.")
            leftovers = []
        ids = [str(r.get("exerciseId")) for r in leftovers if r.get("exerciseId")]
        if ids:
            print(f"      removing {ids}")
            try:
                client.delete_exercises(ids, day)
                print("      removed")
            except Exception as exc:  # noqa: BLE001 — cleanup is best effort
                print(f"      ✗ could not remove {ids}: {exc}")
                print("      DELETE THESE BY HAND in the app.")
        elif leftovers:
            print("      ✗ probe rows exist but carry no id — remove them by hand")
        else:
            print("      nothing left behind")


if __name__ == "__main__":
    sys.exit(main())
