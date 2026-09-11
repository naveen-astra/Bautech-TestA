"""One folder per demo run, one report inside it, one index of all of them.

Before this existed, every live run scattered a loose screenshot straight
into `results/` with an ad-hoc name - forty-plus of them accumulated with no
way to tell which run produced which file, or what the run actually showed.
That is a real usability problem, not a cosmetic one: an evidence trail
nobody can navigate is not evidence anyone will trust.

The fix is structural, not a naming convention someone has to remember to
follow: `write_report()` is the only thing that writes a demo's output, so
every run's files land in the same predictable shape -

    results/demos/<timestamp>_<case_id>/
        report.html          - the whole run, readable in one open
        screen_start.png      - real screenshot before the agent acted
        screen_final.png      - real screenshot after
        trace.txt             - the full step-by-step log, plain text

- and `update_index()` regenerates one master list after every run, so
"where are my results" always has the same one answer:
results/demos/index.html.
"""

from __future__ import annotations

import html
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEMOS_DIR_NAME = "demos"


def _slug(case_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in case_id)


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def _screenshot(adb: str, path: Path) -> bool:
    try:
        result = subprocess.run(
            [adb, "exec-out", "screencap", "-p"], capture_output=True, timeout=15
        )
        if result.stdout:
            path.write_bytes(result.stdout)
            return True
    except (subprocess.SubprocessError, OSError):
        pass
    return False


def write_report(
    results_root: Path,
    adb: str,
    case: Any,
    run: Any,
    brain: Any,
    screen_render: str,
    start_screenshot: bytes | None = None,
) -> Path:
    """Write one run's complete output to its own folder. Returns the folder.

    The final screenshot is taken fresh, right here - this is the one moment
    guaranteed to still be looking at the real device. The starting one
    cannot be taken here too, because by report-writing time the agent has
    already moved the app on; it has to be captured by the caller before the
    run begins and handed in as raw bytes.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = results_root / DEMOS_DIR_NAME / f"{stamp}_{_slug(case.case_id)}"
    folder.mkdir(parents=True, exist_ok=True)

    if start_screenshot:
        (folder / "screen_start.png").write_bytes(start_screenshot)
    _screenshot(adb, folder / "screen_final.png")

    trace_lines = [f"{s.number:>2}. {s.description} -> {s.result}" for s in run.steps]
    (folder / "trace.txt").write_text("\n".join(trace_lines) or "(no steps taken)",
                                       encoding="utf-8")

    verdict = _verdict_label(run)
    (folder / "report.html").write_text(
        _render_html(case, run, brain, screen_render, trace_lines, verdict, stamp,
                     has_start_shot=bool(start_screenshot)),
        encoding="utf-8",
    )

    update_index(results_root)
    return folder


def _verdict_label(run: Any) -> tuple[str, str]:
    """(label, css_class) - a one-glance answer to "how did this go"."""
    if run.finished and run.reached_target_screen:
        return "COMPLETED - screen confirmed", "ok"
    if run.finished:
        return "COMPLETED - target screen not confirmed", "warn"
    if run.gave_up:
        return "GAVE UP (honest) - " + run.give_up_reason[:80], "warn"
    return "DID NOT FINISH - ran out of steps", "warn"


def _render_html(case, run, brain, screen_render, trace_lines, verdict, stamp,
                  has_start_shot: bool = True) -> str:
    label, css_class = verdict
    provider = getattr(brain, "provider", "claude")
    model = getattr(brain, "model", "?")
    usage = getattr(brain, "usage", None)
    cost_line = usage.summary() if usage else "n/a"

    confirmations = "".join(
        f"<li><b>{_esc(where)}</b>: {_esc(why)}</li>" for where, why in run.confirmations
    ) or "<li><i>none</i></li>"

    observations = "".join(
        f"<tr><td>{_esc(key)}</td><td>{_esc(obs.value)}</td>"
        f"<td>{'yes' if obs.screen_confirmed else 'no'}</td></tr>"
        for key, obs in run.observations.items()
    ) or "<tr><td colspan='3'><i>none recorded</i></td></tr>"

    trace_html = "\n".join(_esc(line) for line in trace_lines) or "(no steps taken)"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_esc(case.case_id)} - demo report</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 900px;
         margin: 2rem auto; padding: 0 1.5rem; color: #1a1a1a; background: #fafafa; }}
  h1 {{ margin-bottom: 0.2rem; }}
  .meta {{ color: #555; font-size: 0.92rem; margin-bottom: 1.2rem; }}
  .badge {{ display: inline-block; padding: 0.35rem 0.9rem; border-radius: 6px;
           font-weight: 600; font-size: 0.95rem; }}
  .badge.ok {{ background: #d4edda; color: #155724; }}
  .badge.warn {{ background: #fff3cd; color: #856404; }}
  section {{ background: white; border: 1px solid #e0e0e0; border-radius: 8px;
            padding: 1.2rem 1.5rem; margin: 1rem 0; }}
  h2 {{ font-size: 1.05rem; margin-top: 0; color: #333; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 0.4rem 0.6rem; border-bottom: 1px solid #eee; font-size: 0.9rem; }}
  pre {{ background: #f4f4f4; padding: 0.8rem; border-radius: 6px; overflow-x: auto;
        font-size: 0.85rem; white-space: pre-wrap; }}
  img {{ max-width: 100%; border: 1px solid #ddd; border-radius: 6px; }}
  .screens {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
  .screens figure {{ margin: 0; flex: 1; min-width: 220px; }}
  figcaption {{ font-size: 0.85rem; color: #666; margin-top: 0.3rem; text-align: center; }}
  a.back {{ font-size: 0.85rem; }}
</style>
</head>
<body>
<a class="back" href="index.html">&larr; all demo runs</a>
<h1>{_esc(case.case_id)} &mdash; {_esc(case.title)}</h1>
<div class="meta">{_esc(stamp)} &middot; persona: {_esc(case.persona)} &middot;
brain: {_esc(provider)}/{_esc(model)}</div>
<span class="badge {css_class}">{_esc(label)}</span>

<section>
  <h2>The case</h2>
  <table>
    <tr><td><b>Expected result</b></td><td>{_esc(case.expected)}</td></tr>
    <tr><td><b>Steps</b></td><td>{_esc(case.steps)}</td></tr>
    <tr><td><b>Agent's summary</b></td><td>{_esc(run.summary or "(none - see trace)")}</td></tr>
  </table>
</section>

<section>
  <h2>Screens confirmed</h2>
  <ul>{confirmations}</ul>
</section>

<section>
  <h2>What it recorded</h2>
  <table>
    <tr><th align="left">key</th><th align="left">value</th><th align="left">screen confirmed?</th></tr>
    {observations}
  </table>
</section>

<section>
  <h2>Real screens</h2>
  <div class="screens">
    {'<figure><img src="screen_start.png" alt="start screen"><figcaption>before the agent acted</figcaption></figure>' if has_start_shot else ''}
    <figure><img src="screen_final.png" alt="final screen"><figcaption>after the run</figcaption></figure>
  </div>
</section>

<section>
  <h2>Full step trace</h2>
  <pre>{trace_html}</pre>
</section>

<section>
  <h2>Real screen at the start (as the agent perceived it)</h2>
  <pre>{_esc(screen_render)}</pre>
</section>

<section>
  <h2>Cost</h2>
  <p>{_esc(cost_line)}</p>
</section>

</body>
</html>
"""


def update_index(results_root: Path) -> Path:
    """Rebuild the one master list, from what is actually on disk.

    Regenerated fully each time rather than appended to, so it can never
    drift from reality - delete a run's folder and it disappears from here
    on the next write, with no separate bookkeeping to keep in sync.
    """
    demos_dir = results_root / DEMOS_DIR_NAME
    demos_dir.mkdir(parents=True, exist_ok=True)

    runs = sorted(
        (p for p in demos_dir.iterdir() if p.is_dir() and (p / "report.html").exists()),
        key=lambda p: p.name, reverse=True,
    )

    rows = []
    for run_dir in runs:
        name = run_dir.name
        case_id = name.split("_", 1)[1] if "_" in name else name
        timestamp = name.split("_", 1)[0] if "_" in name else ""
        pretty_time = timestamp
        try:
            pretty_time = datetime.strptime(timestamp, "%Y%m%d-%H%M%S").strftime(
                "%d %b %Y, %H:%M:%S"
            )
        except ValueError:
            pass
        rows.append(
            f'<tr><td>{_esc(pretty_time)}</td><td>{_esc(case_id)}</td>'
            f'<td><a href="{run_dir.name}/report.html">open report</a></td></tr>'
        )

    body = "\n".join(rows) or '<tr><td colspan="3"><i>no demo runs yet</i></td></tr>'

    index_path = demos_dir / "index.html"
    index_path.write_text(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Demo runs</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 800px;
         margin: 2rem auto; padding: 0 1.5rem; color: #1a1a1a; background: #fafafa; }}
  h1 {{ margin-bottom: 0.3rem; }}
  p.sub {{ color: #666; margin-top: 0; }}
  table {{ width: 100%; border-collapse: collapse; background: white;
          border: 1px solid #e0e0e0; border-radius: 8px; overflow: hidden; }}
  th, td {{ padding: 0.7rem 1rem; text-align: left; border-bottom: 1px solid #eee; }}
  th {{ background: #f4f4f4; }}
  tr:last-child td {{ border-bottom: none; }}
  a {{ color: #0a58ca; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>Bautech Sentinel &mdash; demo runs</h1>
<p class="sub">Every live-agent run, newest first. Each report is self-contained -
the case, the real screens, the full trace, and the cost.</p>
<table>
<tr><th>When</th><th>Case</th><th></th></tr>
{body}
</table>
</body>
</html>
""", encoding="utf-8")
    return index_path
