"""Compile whatever is not yet cached, writing real progress to disk as it
goes - a lesson from a real failure earlier this session, where an orphaned
background job's buffered stdout was lost entirely when it had to be killed.
Every line here is flushed immediately, and the cache write for each case
happens independently of whether this process survives to print a summary.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import run as sentinel_run  # noqa: E402
sentinel_run.load_dotenv()

from sentinel.brains import make_compiler_client, BrainError  # noqa: E402
from sentinel.compiler import PlanCompiler, CompilerError  # noqa: E402
from sentinel.parser import load_suite  # noqa: E402
from sentinel.screen_map import ScreenMap  # noqa: E402

NEGATIVE_IDS = {"TC-009", "TC-019", "TC-020", "TC-021", "TC-030", "TC-031", "TC-044", "TC-045",
                "TC-051", "TC-056", "TC-064", "TC-067", "TC-068", "TC-072", "TC-073", "TC-074",
                "TC-077", "TC-080", "TC-083"}

sm = ScreenMap.load()
client = make_compiler_client()
comp = PlanCompiler(sm, client=client)
print(f"brain: {client.brain.provider}/{client.brain.model}", flush=True)

already = {p.name.split("-", 2)[0] + "-" + p.name.split("-", 2)[1]
           for p in Path(".plan_cache").glob("*.json")}
cases = [c for c in load_suite("tests/bautech_suite.csv") if c.case_id not in already]
print(f"{len(already)} already cached, compiling {len(cases)} more\n", flush=True)

ok = fail = 0
for c in cases:
    tag = " [NEGATIVE per brief]" if c.case_id in NEGATIVE_IDS else ""
    try:
        plan = comp.compile(c)
        kinds = ",".join(sorted({a.kind.value for a in plan.assertions})) or plan.feasibility.value
        prohibition = any(a.is_prohibition for a in plan.assertions)
        flag = " -> PROHIBITION" if prohibition else ""
        print(f"  {c.case_id}  {kinds}{flag}{tag}", flush=True)
        ok += 1
    except (CompilerError, BrainError) as e:
        print(f"  {c.case_id}  FAILED: {str(e)[:100]}{tag}", flush=True)
        fail += 1

print(f"\ndone: {ok} compiled, {fail} failed this run", flush=True)
print(f"cost: {client.brain.usage.summary()}", flush=True)
