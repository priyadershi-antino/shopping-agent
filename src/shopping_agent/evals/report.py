"""Evaluation reports: a JSON file with everything and a Markdown summary for people."""

from pathlib import Path

from shopping_agent.evals.metrics import METRIC_LABELS, THRESHOLDS
from shopping_agent.evals.runner import EvalReport


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def to_markdown(report: EvalReport) -> str:
    s = report.summary
    lines = [
        f"# Evaluation report: {report.created_at:%Y-%m-%d %H:%M} UTC",
        "",
        f"- Model: `{report.llm_model}` (fallbacks: `{report.llm_fallbacks or 'none'}`)"
        + (
            ", roles: " + ", ".join(f"{k} `{v}`" for k, v in report.role_models.items())
            if report.role_models
            else ""
        ),
        f"- Dataset: `{report.dataset}`, {s.scenarios} scenarios",
        "- Prompt versions: "
        + ", ".join(f"{k} v{v}" for k, v in sorted(report.prompt_versions.items())),
        f"- {report.note}",
        "",
        "## Summary",
        "",
        f"**{s.passed} of {s.scenarios} scenarios passed every check.**",
        "",
        "| Metric | Score | Scenarios |",
        "|---|---|---|",
    ]
    for name, m in s.metrics.items():
        label = METRIC_LABELS.get(name, name)
        threshold = THRESHOLDS.get(name)
        if threshold:
            label += f" (pass ≥ {_pct(threshold)})"
        lines.append(f"| {label} | {_pct(m.mean)} | {m.n} |")
    cost = f"${s.total_cost_usd:.4f}" if s.total_cost_usd is not None else "free tier / not priced"
    lines += [
        "",
        f"Latency per run: median {s.latency_p50_s:.0f} s, p95 {s.latency_p95_s:.0f} s. "
        f"Mean {s.mean_llm_calls} model calls and {s.mean_tool_calls} tool calls per run. "
        f"Tokens: {s.total_input_tokens:,} in / {s.total_output_tokens:,} out. Cost: {cost}.",
        "",
        "## Scenarios",
        "",
        "| Scenario | Tags | Result | Top pick | Time | Model calls |",
        "|---|---|---|---|---|---|",
    ]
    for r in report.results:
        result = "pass" if r.passed else "**fail**"
        if r.status not in {"completed", "needs_clarification"}:
            result += f" ({r.status})"
        lines.append(
            f"| `{r.id}` | {', '.join(r.tags)} | {result} | {_cell(r.top_pick or '–')} | "
            f"{r.latency_s:.0f} s | {r.llm_calls} |"
        )
    failed = [r for r in report.results if not r.passed]
    if failed:
        lines += ["", "## Failures", ""]
        for r in failed:
            lines.append(f"### `{r.id}`")
            lines.append("")
            lines.append(f"> {_cell(r.request)}")
            lines.append("")
            for f in r.failures:
                lines.append(f"- {_cell(f)}")
            if r.error:
                lines.append(f"- error: {_cell(r.error)}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: EvalReport, out_dir: Path, name: str | None = None) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = name or f"eval-{report.created_at:%Y%m%d-%H%M%S}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")
    return json_path, md_path
