import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sentinel.parser import load_suite

OFFICIAL_19 = {"TC-009", "TC-019", "TC-020", "TC-021", "TC-030", "TC-031", "TC-044", "TC-045",
               "TC-051", "TC-056", "TC-064", "TC-067", "TC-068", "TC-072", "TC-073", "TC-074",
               "TC-077", "TC-080", "TC-083"}
PROHIBITION_KINDS = {"control_absent", "token_absent", "action_rejected"}

latest = {}
for f in Path(".plan_cache").glob("*.json"):
    d = json.load(open(f, encoding="utf-8"))
    latest[d["case_id"]] = d

all_ids = [c.case_id for c in load_suite("tests/bautech_suite.csv")]
print(f"total cases in the sheet: {len(all_ids)}")
print(f"total compiled and cached: {len(latest)}")
missing = [c for c in all_ids if c not in latest]
print(f"still missing: {missing or 'none'}\n")

correct, wrong, infeasible = [], [], []
for cid in OFFICIAL_19:
    plan = latest.get(cid)
    if plan is None:
        wrong.append((cid, "NOT COMPILED"))
        continue
    if plan.get("feasibility") != "automatable":
        infeasible.append((cid, plan.get("feasibility_reason", "")[:70]))
        continue
    kinds = {a["kind"] for a in plan.get("assertions", [])}
    if kinds & PROHIBITION_KINDS:
        correct.append((cid, sorted(kinds)))
    else:
        wrong.append((cid, sorted(kinds) or "no assertions"))

print(f"=== the official 19 negative cases (§20 points) ===")
print(f"correctly derived as a structural prohibition: {len(correct)}/19")
for cid, kinds in sorted(correct):
    print(f"  ok    {cid}  {kinds}")
print(f"\nhonestly missed (compiled, but not as a prohibition): {len(wrong)}")
for cid, kinds in sorted(wrong):
    print(f"  MISS  {cid}  {kinds}")
print(f"\ncorrectly declined as not-yet-automatable (screen-map gap, honest): {len(infeasible)}")
for cid, reason in sorted(infeasible):
    print(f"  ---   {cid}  {reason}")

# Bonus: real prohibitions found OUTSIDE the official 19 (compound rows).
bonus = []
for cid, plan in latest.items():
    if cid in OFFICIAL_19:
        continue
    kinds = {a["kind"] for a in plan.get("assertions", [])}
    if kinds & PROHIBITION_KINDS:
        bonus.append((cid, sorted(kinds)))
print(f"\n=== bonus: prohibitions correctly caught OUTSIDE the official 19 "
      f"(compound rows with an embedded negative) ===")
for cid, kinds in sorted(bonus):
    print(f"  +     {cid}  {kinds}")

feasible_all = sum(1 for p in latest.values() if p.get("feasibility") == "automatable")
infeasible_all = len(latest) - feasible_all
print(f"\n=== whole-suite summary ===")
print(f"automatable: {feasible_all}/{len(latest)}")
print(f"honestly declined (needs_unavailable_interface): {infeasible_all}/{len(latest)}")
