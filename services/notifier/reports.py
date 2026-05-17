"""Per-job report writer.

When the notifier promotes a scored job to `jobs.new` (i.e. it's NEW and
above threshold), we also persist a human-readable Markdown report under
~/.jobseeker/docs/<dedup_id>/report.md and link it from the Telegram alert.

Why a separate file rather than just inlining everything in the Telegram
message: 6 months from now you want to see WHY the AI scored a particular
job a 0.85. The report is your audit trail. Cheap to write, hard to lose.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.notifier.artifact_store import LocalArtifactStore, get_default_store
from services.notifier.models import ScoredJob

log = logging.getLogger("notifier.reports")


def _legitimacy_tag(tier: str) -> str:
    return {
        "HIGH_CONFIDENCE": "Legitimacy: HIGH (likely real, active opening)",
        "PROCEED_WITH_CAUTION": "Legitimacy: PROCEED WITH CAUTION (mixed signals)",
        "SUSPICIOUS": "Legitimacy: SUSPICIOUS (multiple ghost-job indicators)",
    }.get(tier, "Legitimacy: not assessed")


def _adjustments_table(adjustments: dict[str, float]) -> str:
    if not adjustments:
        return "_no rule adjustments applied_"
    lines = ["| Rule | Adjustment |", "|------|-----------:|"]
    for k, v in sorted(adjustments.items(), key=lambda x: -abs(x[1])):
        sign = "+" if v >= 0 else ""
        lines.append(f"| {k} | {sign}{v:.3f} |")
    return "\n".join(lines)


def render_report(
    payload: ScoredJob,
    dedup_id: str,
    extra: Optional[dict] = None,
) -> str:
    job = payload.job
    extra = extra or {}
    score_pct = int(round(payload.score * 100))
    posted = job.scraped_at or "unknown"

    md = [
        f"# {job.company} — {job.title}",
        "",
        f"**dedup_id:** `{dedup_id}`  ",
        f"**source:** {extra.get('source', 'streaming-pipeline')}  ",
        f"**posted/scraped:** {posted}  ",
        f"**reviewed:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Summary",
        "",
        f"- **Score:** {payload.score:.2f} ({score_pct}%)",
        f"- **Embedding score:** {payload.embedding_score:.2f}",
        f"- **{_legitimacy_tag(payload.legitimacy_tier)}**",
        f"- **Apply URL:** [{job.url}]({job.url})",
        f"- **Location:** {job.location or 'TBD'} (remote: {'yes' if job.remote else 'no'})",
        "",
        "## Why match",
        "",
        payload.reason or "_no reason provided_",
        "",
        "## Skills",
        "",
        f"- **You have:** {', '.join(payload.matched_skills) or '-'}",
        f"- **Gaps:** {', '.join(payload.missing_skills) or 'none'}",
        "",
        "## Score breakdown",
        "",
        _adjustments_table(payload.rule_adjustments),
        "",
        "## Posting legitimacy signals",
        "",
    ]
    if payload.legitimacy_signals:
        for s in payload.legitimacy_signals:
            md.append(f"- {s}")
    else:
        md.append("_no signals recorded_")
    md.append("")
    md.append("## Application status")
    md.append("")
    md.append("- [ ] resume tailored")
    md.append("- [ ] cover letter generated")
    md.append("- [ ] applied")
    md.append("- [ ] heard back")
    md.append("- [ ] interview")
    md.append("- [ ] offer")
    md.append("")
    md.append(f"_Update by editing this file or via the Telegram inline buttons "
              f"(dedup_id `{dedup_id}`)._")
    md.append("")
    return "\n".join(md)


def write_report(
    payload: ScoredJob,
    dedup_id: str,
    *,
    store: Optional[LocalArtifactStore] = None,
    extra: Optional[dict] = None,
) -> str:
    """Write the report to artifact store. Returns the canonical path string."""
    store = store or get_default_store()
    md = render_report(payload, dedup_id, extra=extra)
    path = store.put_text(dedup_id, "report.md", md)
    log.info("report written: %s -> %s", dedup_id, path)
    return path
