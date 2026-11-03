"""Human-readable Style audit. JSON records stay the source of truth."""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

NOT_SUPPLIED = "not supplied"


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _pre(value) -> str:
    if value is None:
        return f'<span class="missing">{NOT_SUPPLIED}</span>'
    return f"<pre>{_e(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))}</pre>"


def _sample_links(ids, known_ids: set[str]) -> str:
    if not ids:
        return f'<span class="missing">{NOT_SUPPLIED}</span>'
    parts = []
    for line_id in ids:
        if line_id in known_ids:
            parts.append(f'<a href="#line-{_e(line_id)}"><code>{_e(line_id)}</code></a>')
        else:
            parts.append(f'<code class="bad">{_e(line_id)}</code>')
    return " ".join(parts)


def _diagnostics(decision: dict) -> str:
    diagnostics = decision.get("diagnostics") or {}
    rows = []
    probabilities = diagnostics.get("raw_probabilities")
    if probabilities:
        ordered = sorted(probabilities.items(), key=lambda item: (-item[1], item[0]))
        rows.append(
            "<dt>raw probability distribution</dt><dd>"
            + ", ".join(f"<code>{_e(label)}</code> {value:.3f}" for label, value in ordered)
            + "</dd>"
        )
    else:
        rows.append(f"<dt>raw probability distribution</dt><dd class=\"missing\">{NOT_SUPPLIED}</dd>")
    for key, label in (
        ("raw_confidence", "raw model confidence"),
        ("model_self_reported_confidence", "model self-reported confidence"),
    ):
        value = diagnostics.get(key)
        rows.append(
            f"<dt>{label}</dt><dd>"
            + (f"{value:.3f}" if isinstance(value, (int, float)) else f'<span class="missing">{NOT_SUPPLIED}</span>')
            + "</dd>"
        )
    if decision.get("citation_balance") is not None:
        rows.append(f"<dt>citation balance (count ratio, not confidence)</dt><dd>{decision['citation_balance']:.3f}</dd>")
    source = diagnostics.get("evidence_source")
    if source:
        rows.append(
            "<dt>citation source</dt><dd>"
            f"<code>{_e(source)}</code>: the model ranked the supplied line IDs in a "
            "separate evidence pass, because the value question returns no citations. "
            "The recorded IDs are model output, not application guesses.</dd>"
        )
        rows.append(
            f"<dt>evidence pass version</dt><dd><code>{_e(diagnostics.get('evidence_pass_version'))}</code></dd>"
        )
        ranks = diagnostics.get("evidence_questions") or {}
        for question_id, detail in ranks.items():
            top = detail.get("top_lines") or []
            rows.append(
                f"<dt>{_e(question_id)}</dt><dd>"
                + (", ".join(
                    f"<code>{_e(item.get('line_id'))}</code> {item.get('probability'):.3f}"
                    for item in top if isinstance(item.get("probability"), (int, float))
                ) or f'<span class="missing">{NOT_SUPPLIED}</span>')
                + "</dd>"
            )
        failures = diagnostics.get("evidence_failures") or {}
        for question_id, reason in failures.items():
            rows.append(
                f"<dt>{_e(question_id)}</dt><dd class=\"missing\">{_e(reason)}</dd>"
            )
    return "<dl>" + "".join(rows) + "</dl>"


def _tier(decision: dict) -> str:
    evidence = decision.get("evidence") or {}
    thresholds = evidence.get("thresholds") or {}
    counts = (
        f"{evidence.get('sampled_lines', 0)} lines / "
        f"{evidence.get('sampled_words', 0)} words / "
        f"{evidence.get('context_count', 0)} contexts"
    )
    rows = [f"<p><b>{_e(decision.get('evidence_tier'))}</b> at {_e(counts)}</p>"]
    if thresholds:
        rows.append(
            "<ul>"
            + "".join(
                f"<li>{_e(name)}: {_e(rule.get('lines'))} lines, {_e(rule.get('words'))} words, "
                f"{_e(rule.get('contexts'))} contexts</li>"
                for name, rule in thresholds.items()
            )
            + "</ul>"
        )
    if evidence.get("field_multiplier") is not None:
        rows.append(f"<p>field multiplier: {_e(evidence.get('field_multiplier'))}</p>")
    return "".join(rows)


def _patterns(patterns, known_ids: set[str]) -> str:
    if not patterns:
        return f'<span class="missing">{NOT_SUPPLIED}</span>'
    return "<ul>" + "".join(
        f"<li><b>{_e(pattern.get('label'))}</b>: "
        f"{_sample_links(pattern.get('line_ids') or [], known_ids)}</li>"
        for pattern in patterns
    ) + "</ul>"


def render_style_audit(report: dict, auditions: list[dict] | None = None) -> str:
    identity = report.get("identity_facts") or {}
    request = report.get("jev_request") or {}
    sample = report.get("jev_sample") or []
    provenance = report.get("jev_sample_local_provenance") or {}
    known_ids = {str(row.get("id")) for row in sample if row.get("id")}
    classification = report.get("classification") or {}
    analysis = classification.get("speech_style_analysis") if classification else None
    decisions = (analysis or {}).get("dimensions") or {}
    style_fields = ("register", "lexical_complexity", "sentence_complexity", "colloquialism", "discourse_style")

    identity_rows = "".join(
        f"<tr><th>{_e(key)}</th><td>{_e(value)}</td></tr>"
        for key, value in sorted(identity.items())
        if value not in (None, "", [], {})
    )
    metadata_rows = "".join(
        f"<tr><th>{_e(key.replace('_', ' '))}</th><td>{_pre(value) if isinstance(value, (dict, list)) else _e(value) if value is not None else '<span class=\"missing\">' + NOT_SUPPLIED + '</span>'}</td></tr>"
        for key, value in request.items()
        if key not in {"available", "evidence"}
    )
    if request.get("evidence"):
        metadata_rows += f"<tr><th>request-visible evidence</th><td>{_pre(request['evidence'])}</td></tr>"

    sample_rows = []
    for row in sample:
        line_id = str(row.get("id"))
        local = provenance.get(line_id) or {}
        sample_rows.append(
            f'<tr id="line-{_e(line_id)}"><td><code>{_e(line_id)}</code></td>'
            f"<td>{_e(row.get('text'))}</td>"
            f"<td>{_e(local.get('source'))} / {_e(local.get('sheet'))} "
            f"row {_e(local.get('row_id'))}</td></tr>"
        )
    if not sample_rows:
        sample_rows.append(
            '<tr><td colspan="3" class="missing">Exact request sample was not recorded for this '
            "classification. The report does not reconstruct a different sample.</td></tr>"
        )

    style_rows = []
    for field in style_fields:
        decision = decisions.get(field) or {}
        style_rows.append(
            "<tr>"
            f"<th>{_e(field)}</th>"
            f"<td><b>{_e(decision.get('value'))}</b></td>"
            f"<td>{_e(decision.get('status'))}</td>"
            f"<td>{_tier(decision)}</td>"
            f"<td>{_sample_links(decision.get('supporting_line_ids') or [], known_ids)}</td>"
            f"<td>{_sample_links(decision.get('counterexample_line_ids') or [], known_ids)}</td>"
            f"<td>{_patterns(decision.get('patterns') or [], known_ids)}</td>"
            f"<td>{_e(decision.get('rationale'))}</td>"
            f"<td>{_diagnostics(decision)}</td>"
            "</tr>"
        )

    audit_features = report.get("jev_audit_features") or {}
    instructions = (
        ("Identity", report.get("identity_instruction")),
        ("Style", report.get("style_instruction")),
        ("Combined", report.get("combined_instruction")),
    )
    instruction_blocks = "".join(
        f"<h3>{label}</h3><pre>{_e(value) if value is not None else NOT_SUPPLIED}</pre>"
        for label, value in instructions
    )
    hash_rows = "".join(
        f"<tr><th>{_e(label)}</th><td><code>{_e(value) if value is not None else NOT_SUPPLIED}</code></td></tr>"
        for label, value in (
            ("identity prompt hash", report.get("identity_prompt_hash")),
            ("style profile hash", report.get("style_profile_hash")),
            ("combined instruct hash", report.get("combined_instruct_hash")),
            ("reference text", report.get("reference_text")),
            ("design model", report.get("design_model")),
            ("design seed", report.get("design_seed")),
        )
    )

    audio_blocks = ""
    if auditions:
        grouped: dict[Any, dict[str, dict]] = {}
        for item in auditions:
            grouped.setdefault(item.get("seed"), {})[item.get("condition")] = item

        def clip(item: dict) -> str:
            flags = []
            if item.get("in_range") is False:
                flags.append('<b class="bad">outside the 8-18 s acceptance range</b>')
            if not item.get("sha256"):
                flags.append('<b class="bad">no recorded hash</b>')
            if not item.get("path"):
                flags.append('<b class="bad">no recorded file</b>')
            return (
                "<div>"
                f"<audio controls preload=\"none\" src=\"{_e(item.get('path') or '')}\"></audio>"
                f"<p>{_e(item.get('condition'))} · seed {_e(item.get('seed'))} · "
                f"{_e(item.get('seconds'))} s · sha256 "
                f"<code>{_e(item.get('sha256'))}</code></p>"
                + (f"<p>{' · '.join(flags)}</p>" if flags else "")
                + f"<pre>{_e(item.get('instruct'))}</pre>"
                "</div>"
            )

        rows = []
        for seed in sorted(grouped, key=lambda value: (value is None, value)):
            pair = grouped[seed]
            rows.append(
                f"<section class=\"take\"><h3>seed {_e(seed)}</h3>"
                + "".join(clip(pair[condition]) for condition in
                          ("identity_only", "identity_style") if condition in pair)
                + "".join(clip(pair[condition]) for condition in sorted(pair)
                             if condition not in {"identity_only", "identity_style"})
                + "</section>"
            )
        audio_blocks = (
            "<h2>Reference audition</h2>"
            "<p>Matched pairs share the reference text, design model and seed. "
            "Rate character fit, naturalness, whether it still sounds like the intended "
            "speaker, and whether the Style clause improves the voice or caricatures it. "
            "Text-style verification and acoustic approval stay separate decisions.</p>"
            + "".join(rows)
        )
    else:
        audio_blocks = (
            '<h2>Reference audition</h2><p class="missing">No generated reference WAVs '
            "are linked to this audit yet.</p>"
        )

    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Style audit — {_e(report.get('name'))}</title>
<style>
body{{font:14px system-ui,sans-serif;margin:24px;max-width:1400px;line-height:1.4}}
h1,h2,h3{{line-height:1.2}} h2{{margin-top:2rem;border-top:1px solid rgba(128,128,128,.3);padding-top:1rem}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0 1rem}}
th,td{{border-bottom:1px solid rgba(128,128,128,.25);padding:.35rem .6rem .35rem 0;text-align:left;vertical-align:top}}
th{{width:12rem}} .style th{{width:auto}}
pre{{white-space:pre-wrap;background:rgba(128,128,128,.08);padding:.6rem;border-radius:4px;margin:.3rem 0}}
code{{font-size:.9em}} .missing{{opacity:.6;font-style:italic}} .bad{{color:#c0392b}}
.notice{{background:rgba(128,128,128,.1);border-left:3px solid currentColor;padding:.6rem .8rem}}
.take{{margin:1rem 0}} audio{{width:100%}}
</style>
<h1>Style audit — {_e(report.get('name'))} <small>{_e(report.get('canonical_id'))}</small></h1>
<p class="notice">JEV received only the opaque line IDs, text and aggregate counts below.
Identity, lore, source and sheet fields are shown for the reviewer and were not part of the request.</p>

<h2>Identity facts</h2><table>{identity_rows}</table>
<h2>JEV request metadata</h2><table>{metadata_rows}</table>
<h2>Exact post-trim representative sample</h2>
<p>{len(sample)} lines. These are the exact lines recorded with the classification.</p>
<table><tr><th>opaque ID</th><th>text sent</th><th>local provenance (not sent)</th></tr>{''.join(sample_rows)}</table>
<h2>Style decisions</h2>
<p>Status and evidence tier are application evidence gates. Raw probabilities and
confidence values are model diagnostics, never calibrated confidence.</p>
<table class="style"><tr><th>field</th><th>JEV value</th><th>application status</th><th>evidence tier</th>
<th>support</th><th>counterexamples</th><th>mixed patterns</th><th>JEV rationale</th><th>model diagnostics</th></tr>
{''.join(style_rows)}</table>
<h2>Audit-only features</h2>
<p class="notice">Non-production. These measurements are not rendered into the voice instruction.</p>
{_pre(audit_features)}
<h2>Exact instructions</h2>{instruction_blocks}
<h2>Plan provenance</h2><table>{hash_rows}</table>
{audio_blocks}
</html>
"""


def write_style_audit(report: dict, path: Path | str, auditions: list[dict] | None = None) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_style_audit(report, auditions), encoding="utf-8")
    return target
