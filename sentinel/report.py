"""Write up a run.

Three artefacts, because they answer different questions:

    report.md / report.html   every case, with expected, actual and evidence
    defects.md                the two lists a reviewer actually acts on

The defect list is split into *Bautech defects* and *automation failures*, and
that split is the point. A run that reports thirty failures is useless if nobody
can tell which of them are the app's fault. The failure class is carried on
every result precisely so this separation is mechanical rather than a judgement
call made at write-up time.

Negative cases print their adjudication ladder, so a reader can see which rungs
a prohibition actually cleared instead of taking the verdict on trust.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

from sentinel.schema import AssertionResult, CaseResult, FailureClass, Verdict
from sentinel.verdict import summarise

_VERDICT_COLOUR = {
    Verdict.PASS: "#1a7f37",
    Verdict.FAIL: "#cf222e",
    Verdict.BLOCKED: "#9a6700",
}

_LADDER_ORDER = ("reachability", "affordance", "differential", "enforcement", "state")


def _ladder_text(result: AssertionResult) -> str:
    if not result.ladder:
        return ""
    parts = [
        f"{name} {'yes' if result.ladder[name] else 'NO'}"
        for name in _LADDER_ORDER
        if name in result.ladder
    ]
    return " | ".join(parts)


def _run_header(results: list[CaseResult], meta: dict) -> dict:
    counts = summarise(results)
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **meta,
        **counts,
    }


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def render_markdown(results: list[CaseResult], meta: dict) -> str:
    header = _run_header(results, meta)
    lines: list[str] = [
        "# Bautech Sentinel - run report",
        "",
        f"- Run: `{header.get('run_id', 'unknown')}`",
        f"- Build: `{header.get('build', 'unknown')}`",
        f"- Device: `{header.get('device', 'unknown')}`",
        f"- Generated: {header['generated']}",
        "",
        f"**{header['total']} cases** - "
        f"{header['PASS']} passed, {header['FAIL']} failed, {header['BLOCKED']} blocked.",
        "",
        f"Of the non-passing cases, {header['app_defects']} "
        f"{'is a Bautech defect' if header['app_defects'] == 1 else 'are Bautech defects'} "
        f"and {header['automation_failures']} "
        f"{'is' if header['automation_failures'] == 1 else 'are'} our own automation failing.",
        "",
        "| ID | Persona | Verdict | Expected | Actual |",
        "|---|---|---|---|---|",
    ]

    def cell(text: str, limit: int = 90) -> str:
        text = (text or "").replace("|", "\\|").replace("\n", " ")
        return text if len(text) <= limit else text[: limit - 1] + "…"

    for result in sorted(results, key=lambda r: r.case_id):
        lines.append(
            f"| {result.case_id} | {cell(result.persona, 24)} | **{result.verdict.value}** "
            f"| {cell(result.expected)} | {cell(result.actual)} |"
        )

    lines += ["", "## Case detail", ""]
    for result in sorted(results, key=lambda r: r.case_id):
        lines += [
            f"### {result.case_id} - {result.verdict.value}",
            "",
            f"- **Persona:** {result.persona}",
            f"- **Expected:** {result.expected}",
            f"- **Actual:** {result.actual}",
        ]
        if result.failure_class:
            lines.append(f"- **Classification:** {result.failure_class.value}")
        if result.probable_cause:
            lines.append(f"- **Probable cause:** {result.probable_cause}")
        if result.duration_seconds:
            lines.append(f"- **Duration:** {result.duration_seconds:.1f}s")
        lines.append(f"- **Evidence:** {', '.join(f'`{e}`' for e in result.evidence)}")

        for assertion in result.assertions:
            status = "pass" if assertion.passed else "FAIL"
            lines.append(f"  - `{assertion.id}` ({assertion.kind.value}): {status}")
            ladder = _ladder_text(assertion)
            if ladder:
                lines.append(f"    - adjudication: {ladder}")
            if assertion.reasoning:
                lines.append(f"    - {assertion.reasoning}")
        lines.append("")

    return "\n".join(lines)


def render_defects(results: list[CaseResult], meta: dict) -> str:
    """The two lists, kept apart."""
    defects = [r for r in results if r.failure_class is FailureClass.APPLICATION_DEFECT]
    automation = [r for r in results if r.failure_class is FailureClass.AUTOMATION_FAILURE]
    environment = [r for r in results if r.failure_class is FailureClass.ENVIRONMENT_BLOCK]
    missing = [r for r in results if r.failure_class is FailureClass.MISSING_INTERFACE]

    lines = [
        "# Defect list",
        "",
        f"Run `{meta.get('run_id', 'unknown')}` against build `{meta.get('build', 'unknown')}`.",
        "",
        "## Bautech defects",
        "",
        "Behaviour that differed from what the test case required, observed cleanly.",
        "",
    ]
    if not defects:
        lines.append("_None found._")
    for result in defects:
        lines += [
            f"### {result.case_id} - {result.persona}",
            "",
            f"- **Expected:** {result.expected}",
            f"- **Actual:** {result.actual}",
            f"- **Probable cause:** {result.probable_cause or 'not determined'}",
            f"- **Evidence:** {', '.join(f'`{e}`' for e in result.evidence)}",
            "",
        ]

    lines += [
        "## Automation failures",
        "",
        "Cases where *our* automation broke. These are not Bautech defects and must not "
        "be counted as findings.",
        "",
    ]
    if not automation:
        lines.append("_None._")
    for result in automation:
        lines += [
            f"- **{result.case_id}** ({result.persona}): {result.actual}",
            f"  - {result.probable_cause}",
        ]

    if environment:
        lines += ["", "## Environment blocks", ""]
        for result in environment:
            lines.append(f"- **{result.case_id}**: {result.actual}")

    if missing:
        lines += [
            "",
            "## Not testable through available interfaces",
            "",
            "These need something we do not have - a real payment, a billing cycle to "
            "elapse, a database. Recorded rather than approximated.",
            "",
        ]
        for result in missing:
            lines.append(f"- **{result.case_id}**: {result.probable_cause}")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #


def render_html(results: list[CaseResult], meta: dict) -> str:
    header = _run_header(results, meta)
    e = html.escape

    rows: list[str] = []
    for result in sorted(results, key=lambda r: r.case_id):
        colour = _VERDICT_COLOUR[result.verdict]
        ladders = "".join(
            f"<div class='rung'>{e(a.id)}: {e(_ladder_text(a))}</div>"
            for a in result.assertions
            if a.ladder
        )
        rows.append(
            f"<tr class='{result.verdict.value.lower()}'>"
            f"<td class='id'>{e(result.case_id)}</td>"
            f"<td>{e(result.persona)}</td>"
            f"<td><span class='verdict' style='background:{colour}'>"
            f"{result.verdict.value}</span></td>"
            f"<td>{e(result.expected)}</td>"
            f"<td>{e(result.actual)}{ladders}"
            + (
                f"<div class='cause'>{e(result.probable_cause)}</div>"
                if result.probable_cause
                else ""
            )
            + "</td>"
            f"<td class='ev'>{'<br>'.join(e(x) for x in result.evidence)}</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<meta charset="utf-8">
<title>Bautech Sentinel - {e(str(header.get('run_id', '')))}</title>
<style>
  body {{ font: 14px/1.5 system-ui, sans-serif; margin: 2rem; color: #1f2328; }}
  h1 {{ margin-bottom: .25rem; }}
  .meta {{ color: #656d76; margin-bottom: 1.5rem; }}
  .counts span {{ display: inline-block; padding: .35rem .7rem; border-radius: 6px;
                  margin-right: .5rem; color: #fff; font-weight: 600; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1.5rem; }}
  th, td {{ border: 1px solid #d0d7de; padding: .5rem .6rem; text-align: left;
            vertical-align: top; }}
  th {{ background: #f6f8fa; }}
  td.id {{ font-family: ui-monospace, monospace; white-space: nowrap; }}
  td.ev {{ font-family: ui-monospace, monospace; font-size: 11px; color: #656d76; }}
  .verdict {{ color: #fff; padding: .1rem .5rem; border-radius: 4px; font-size: 12px;
              font-weight: 700; }}
  .rung {{ font-size: 11px; color: #656d76; font-family: ui-monospace, monospace;
           margin-top: .3rem; }}
  .cause {{ font-size: 12px; color: #953800; margin-top: .35rem; }}
  tr.fail td {{ background: #fff8f8; }}
  tr.blocked td {{ background: #fffdf5; }}
</style>
<h1>Bautech Sentinel</h1>
<div class="meta">
  run <code>{e(str(header.get('run_id', 'unknown')))}</code> &middot;
  build <code>{e(str(header.get('build', 'unknown')))}</code> &middot;
  device <code>{e(str(header.get('device', 'unknown')))}</code> &middot;
  {e(header['generated'])}
</div>
<div class="counts">
  <span style="background:{_VERDICT_COLOUR[Verdict.PASS]}">{header['PASS']} pass</span>
  <span style="background:{_VERDICT_COLOUR[Verdict.FAIL]}">{header['FAIL']} fail</span>
  <span style="background:{_VERDICT_COLOUR[Verdict.BLOCKED]}">{header['BLOCKED']} blocked</span>
  <span style="background:#57606a">{header['total']} total</span>
</div>
<p>{header['app_defects']} Bautech defect(s), {header['automation_failures']}
automation failure(s).</p>
<table>
  <tr><th>ID</th><th>Persona</th><th>Verdict</th><th>Expected</th>
      <th>Actual &amp; adjudication</th><th>Evidence</th></tr>
  {''.join(rows)}
</table>
"""


# --------------------------------------------------------------------------- #


def write_all(results: list[CaseResult], meta: dict, out_dir: str | Path) -> list[Path]:
    """Write every artefact. Returns the paths written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    artefacts = {
        "report.md": render_markdown(results, meta),
        "report.html": render_html(results, meta),
        "defects.md": render_defects(results, meta),
        "results.json": json.dumps(
            {
                "meta": _run_header(results, meta),
                "cases": [r.model_dump(mode="json") for r in results],
            },
            indent=2,
        ),
    }

    written: list[Path] = []
    for name, content in artefacts.items():
        path = out_dir / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
