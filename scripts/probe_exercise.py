#!/usr/bin/env python3
"""Verify the exercise write path against a live Cronometer account.

The read side of this API was reverse-engineered from real payloads; the
write side for exercise entries was not. `DELETE /api/v3/user/{id}/diary-
entries` is proven — `delete_entries` has been using it — and POST and PUT on
the same collection are *inferred* from it. This script is how that inference
gets checked, once, against a real account, instead of being discovered in
production three weeks later.

It creates one throwaway entry, reads it back, updates it, reads it again and
deletes it, printing what Cronometer said at every step. **It always cleans up
after itself**, including when a step fails.

    CRONOMETER_USERNAME=... CRONOMETER_PASSWORD=... \\
        python3 scripts/probe_exercise.py [YYYY-MM-DD]

What to look for in the output:

  * whether the fallback warning appears — if it does, v3 POST/PUT are not
    real and the v2 verb-per-action endpoints are what this API wants
  * whether `calorie_override` reads back **true**. If it comes back false, or
    the calories come back as something other than what was sent, Cronometer
    is recomputing the row from its own MET tables and any externally
    measured figure would be silently discarded — which is the failure this
    whole feature exists to avoid.
  * whether `exercise_id` is populated on create. Without it there is nothing
    to update or delete against, and the design has to change to
    delete-and-recreate.
"""

import logging
import sys
from datetime import date

sys.path.insert(0, "src")

from cronometer_api_mcp.client import CronometerClient  # noqa: E402

PROBE_NAME = "zz probe — delete me"
PROBE_KCAL = 123
PROBE_MINUTES = 45


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


def main():
    logging.basicConfig(level=logging.INFO, format="  %(levelname)-7s %(message)s")
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    client = CronometerClient()
    created = None

    try:
        print(f"\n[1] exercise rows already on {day}")
        before = client.get_exercises(day)
        show(before)

        print(f"\n[2] add — {PROBE_KCAL} kcal / {PROBE_MINUTES} min")
        created = client.add_exercise(
            name=PROBE_NAME, calories=PROBE_KCAL, minutes=PROBE_MINUTES, day=day
        )
        print(f"      returned: {created}")

        print("\n[3] read back")
        rows = client.get_exercises(day)
        mine = [r for r in rows if r.get("name") == PROBE_NAME]
        show(mine)
        if not mine:
            print("      ✗ the entry did not land — nothing else below is meaningful")
            return 1
        entry_id = mine[0].get("exerciseId")
        if entry_id is None:
            print("      ✗ no exerciseId — update and delete have nothing to act on")
        got = abs(mine[0].get("calories") or 0)
        if round(got) != PROBE_KCAL:
            print(f"      ✗ calories came back {got}, not {PROBE_KCAL} — recomputed")
        if not mine[0].get("calorieOverride"):
            print("      ✗ calorieOverride is false — the number is not pinned")

        if entry_id is not None:
            print("\n[4] update to 456 kcal")
            client.update_exercise(entry_id, calories=456, day=day)
            after = [
                r for r in client.get_exercises(day) if r.get("name") == PROBE_NAME
            ]
            show(after)
            if after and round(abs(after[0].get("calories") or 0)) != 456:
                print("      ✗ the update did not take")

        print("\n[5] verdict")
        print("      create/update/delete all answered — see the warnings above,")
        print("      and check for a 'falling back to /api/v2/...' line, which")
        print("      means v3 POST/PUT are not real endpoints.")
        return 0

    finally:
        if created is not None:
            print("\n[6] cleaning up")
            leftovers = [
                r for r in client.get_exercises(day) if r.get("name") == PROBE_NAME
            ]
            ids = [str(r.get("exerciseId")) for r in leftovers if r.get("exerciseId")]
            if ids:
                print(f"      removing {ids}")
                try:
                    client.delete_exercises(ids, day)
                except Exception as exc:  # noqa: BLE001 — cleanup is best effort
                    print(f"      ✗ could not remove {ids}: {exc}")
                    print("      DELETE THESE BY HAND in the app.")
            elif leftovers:
                print("      ✗ probe rows exist but carry no id — remove them by hand")
            else:
                print("      nothing left behind")


if __name__ == "__main__":
    sys.exit(main())
