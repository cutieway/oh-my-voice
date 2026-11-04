"""Build a blind identity-only versus identity+Style listening kit.

The audit pages label the condition and always show identity-only first, so
they support exploration but not a blind judgment. This module copies matching
takes under neutral names, randomizes sides per judge, and keeps the unblinded
mapping in a separate key file that is never packaged for a judge.

Two things make the questions answerable without revealing who the speaker is:

* each pair carries a short *speaker brief* -- a few of that speaker's other
  lines, with proper nouns, placeholders and stage directions removed. A judge
  can judge "does this delivery suit the manner of these lines" without knowing
  the character;
* only questions that one clip can answer are asked. Cross-render identity
  stability is measured separately, on three clips of the same condition, which
  is the only setting where "same speaker" is observable.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import random
import re
import shutil
from typing import Any

CONDITIONS = ("identity_only", "identity_style")

# Each question states both anchors so a judge does not have to invent a scale.
QUESTIONS = (
    {
        "id": "fit_to_brief",
        "label": "Fit to the speaker's other lines",
        "hint": "Read the brief above. Would this be the same manner of speaker?",
        "low": "1 = sounds like a different kind of speaker",
        "high": "5 = plainly the same manner of speaker",
    },
    {
        "id": "naturalness",
        "label": "Naturalness",
        "hint": "Does it sound like a recorded performance rather than a synthetic artefact?",
        "low": "1 = obviously synthetic, wrong rhythm",
        "high": "5 = could pass as a real recording",
    },
    {
        "id": "overacting",
        "label": "Freedom from overacting",
        "hint": "Caricature is a defect even when it is expressive.",
        "low": "1 = caricature or overdone delivery",
        "high": "5 = restrained and believable",
    },
)
CONSISTENCY_QUESTION = {
    "id": "same_speaker",
    "label": "Same speaker across the three clips",
    "hint": "All three clips come from one voice. Do they sound like one person?",
    "low": "1 = three different people",
    "high": "5 = clearly one person",
}
PREFERENCE_OPTIONS = ("L", "R", "BOTH_EQUAL", "NEITHER")
MAX_BRIEF_LINES = 3
_BRIEF_LENGTH = (35, 190)
# Words that are capitalised in ordinary English and must not count as names.
_COMMON_CAPITALS = frozenset("""
A An And As At Be But By Can Come Could Did Do Does Downtown Each Even Every First For From Get Give Go
Had Has Have He Her Here His How I If In Is It Its Just Keep Last Left Let Like Listen Look Make Many May
More Most Much Must My Never New Next No Nobody Not Nothing Now Of Off Oh Okay On Once One Only Or Other
Our Out Over Perhaps Please Right Said Say See Shall She Should So Some Something Still Such Sure Take Tell
Than Thank That The Their Them Then There These They Thing This Those Through To Too Twice Two Under Until
Up Upon Us Very Was We Well Were What When Where Which While Who Why Will With Without Would Yes Yet You
Your Aye Eh Ha Hey Hm Hmm Huh Psst Shh Yo Ah Ooh Ow Whoa Wow Nah Yep Yup Hm
""".split())
_NAME = re.compile(r"^[A-Z][a-z]{2,}$")


def _brief_line_ok(text: str) -> bool:
    """Reject lines that would leak identity or read as broken text."""
    if not text or "_" in text:
        return False
    if "(-" in text or text.count("...") >= 2:
        return False
    if not _BRIEF_LENGTH[0] <= len(text) <= _BRIEF_LENGTH[1]:
        return False
    for token in re.findall(r"[A-Za-z][A-Za-z'-]*", text):
        if _NAME.match(token) and token not in _COMMON_CAPITALS:
            return False
    return text.rstrip().endswith((".", "!", "?", "…"))


def _speaker_brief(dossier_path: pathlib.Path | None, seed: int) -> list[str]:
    """Return a few of the speaker's other lines, scrubbed of names."""
    if dossier_path is None or not dossier_path.is_file():
        return []
    dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
    candidates = [
        sample["text"].strip()
        for sample in dossier.get("dialogue_samples") or []
        if _brief_line_ok(sample.get("text", "").strip())
    ]
    if not candidates:
        return []
    rng = random.Random(seed)
    picked = rng.sample(candidates, min(MAX_BRIEF_LINES, len(candidates)))
    return sorted(picked, key=len, reverse=True)


def _dossier_index(dossiers: pathlib.Path | None) -> dict[str, pathlib.Path]:
    if dossiers is None or not dossiers.is_dir():
        return {}
    index: dict[str, pathlib.Path] = {}
    for path in dossiers.glob("*.json"):
        stem = path.name.split(".")[0]
        index.setdefault(stem, path)
    return index


def _load_blocks(audition_root: pathlib.Path) -> list[dict[str, Any]]:
    """Load same-voice blocks that live beside each NPC's references.

    Each block is several sentences cloned from ONE pinned reference, recorded
    in `<audition_root>/<npc>/same_voice/report.json`. A different reference is
    a different designed speaker, so a block is skipped unless every line shares
    one reference hash.
    """
    blocks = []
    for report_path in sorted(audition_root.glob("*/same_voice/report.json")):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        root = report_path.parent
        for voice in report.get("voices") or []:
            for take in voice.get("takes") or []:
                lines = [line for line in take.get("lines") or [] if line.get("file")]
                if len(lines) < 2:
                    continue
                paths = [root / line["file"] for line in lines]
                if not all(path.is_file() for path in paths):
                    continue
                blocks.append({
                    "profile_id": voice["profile_id"],
                    "condition": voice.get("condition"),
                    "ref_sha256": take.get("ref_sha256"),
                    "ref": take.get("ref"),
                    "seed": take.get("seed"),
                    "lines": [
                        {"path": path, "text": line.get("text"), "sha256": line.get("sha256")}
                        for path, line in zip(paths, lines)
                    ],
                })
    return blocks


def _load_pairs(
    audition_root: pathlib.Path,
    dossiers: pathlib.Path | None,
) -> list[dict[str, Any]]:
    index = _dossier_index(dossiers)
    pairs = []
    for report_path in sorted(audition_root.glob("*/report.json")):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        directory = report_path.parent
        by_seed: dict[int, dict[str, dict]] = {}
        for voice in report.get("voices") or []:
            condition = voice.get("condition")
            if condition not in CONDITIONS:
                continue
            for take in voice.get("takes") or []:
                ref = take.get("ref")
                if not ref:
                    continue
                source = directory / ref
                if not source.is_file():
                    continue
                by_seed.setdefault(int(voice["seed"]), {})[condition] = {
                    "path": source,
                    "seconds": take.get("ref_seconds"),
                    "sha256": take.get("ref_sha256"),
                    "instruct": take.get("instruct") or voice.get("instruct"),
                }
        brief = _speaker_brief(index.get(directory.name), seed=0)
        for seed, cells in sorted(by_seed.items()):
            if set(cells) != set(CONDITIONS):
                continue
            pairs.append({
                "profile_id": directory.name,
                "seed": seed,
                "text": (report.get("texts") or [None])[0],
                "reference_text": report.get("reference_text"),
                "brief": brief,
                "cells": cells,
            })
    return pairs


def build_blind_listening_kit(
    *,
    audition_root: pathlib.Path | str = "data/blueprints/auditions",
    out: pathlib.Path | str = "data/blueprints/auditions/blind_ab",
    seed: int = 20261104,
    judges: tuple[str, ...] = ("judge_a", "judge_b"),
    dossiers: pathlib.Path | str | None = "data/blueprints/cache/dossiers",
) -> dict:
    """Copy matched takes under neutral names and emit one randomized HTML kit per judge.

    `dossiers` supplies the speaker brief: a few non-identifying lines from the
    same speaker so `fit_to_brief` is answerable without naming the character.
    """
    audition_root = pathlib.Path(audition_root)
    out = pathlib.Path(out)
    dossiers = pathlib.Path(dossiers) if dossiers else None
    pairs = _load_pairs(audition_root, dossiers)
    if not pairs:
        raise FileNotFoundError(f"no matched identity_only/identity_style pairs under {audition_root}")
    clips_dir = out / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    kit_pairs = []
    for index, pair in enumerate(pairs, start=1):
        pair_id = f"P{index:02d}"
        sides = {}
        for condition in CONDITIONS:
            source = pair["cells"][condition]
            digest = hashlib.sha256(source["path"].read_bytes()).hexdigest()
            name = f"{pair_id}_{digest[:10]}.wav"
            destination = clips_dir / name
            if not destination.is_file():
                shutil.copyfile(source["path"], destination)
            sides[condition] = {"clip": f"clips/{name}", "sha256": digest}
        kit_pairs.append({"pair_id": pair_id, **pair, "clips": sides})

    # Same-voice blocks come from one pinned reference with several sentences.
    # Different seeds are different designed speakers, so they cannot be used.
    blocks_source = _load_blocks(audition_root)
    block_cells: dict[tuple[str, str], dict] = {}
    for index, block in enumerate(blocks_source, start=1):
        digest = block["ref_sha256"] or ""
        clips = []
        # Neutral names: the condition must not be readable from a filename.
        for position, line in enumerate(block["lines"], start=1):
            # Hash the bytes we actually ship: short prefixes of recorded
            # hashes can collide across conditions and would be readable.
            line_hash = hashlib.sha256(line["path"].read_bytes()).hexdigest()
            # Opaque name: no cluster, condition, order or profile hint.
            name = f"{line_hash[:16]}.wav"
            destination = clips_dir / name
            if not destination.is_file():
                shutil.copyfile(line["path"], destination)
            clips.append(f"clips/{name}")
        block_cells[(block["profile_id"], block["condition"])] = {
            "clips": clips,
            "ref_sha256": digest,
            "seed": block["seed"],
        }

    rng = random.Random(seed)
    judges_out = {}
    for judge in judges:
        order = {pair["pair_id"]: rng.sample(list(CONDITIONS), 2) for pair in kit_pairs}
        pair_order = [p["pair_id"] for p in kit_pairs]
        rng.shuffle(pair_order)
        presentation = []
        key = []
        for pair_id in pair_order:
            pair = next(item for item in kit_pairs if item["pair_id"] == pair_id)
            left_condition, right_condition = order[pair_id]
            presentation.append({
                "pair_id": pair_id,
                "brief": pair["brief"],
                "left": {"clip": pair["clips"][left_condition]["clip"]},
                "right": {"clip": pair["clips"][right_condition]["clip"]},
            })
            key.append({
                "pair_id": pair_id,
                "profile_id": pair["profile_id"],
                "seed": pair["seed"],
                "left": left_condition,
                "right": right_condition,
            })
        block_order = sorted(block_cells, key=lambda item: (item[0], item[1]))
        rng.shuffle(block_order)
        blocks = []
        block_key = []
        for block_index, (profile_id, condition) in enumerate(block_order, start=1):
            block_id = f"S{block_index:02d}"
            cell = block_cells[(profile_id, condition)]
            blocks.append({"block_id": block_id, "clips": cell["clips"]})
            block_key.append({
                "block_id": block_id,
                "profile_id": profile_id,
                "condition": condition,
                "ref_sha256": cell["ref_sha256"],
                "seed": cell["seed"],
            })
        kit = {
            "judge": judge,
            "seed": seed,
            "pairs": presentation,
            "blocks": blocks,
            "questions": [dict(q) for q in QUESTIONS],
            "consistency": dict(CONSISTENCY_QUESTION),
            "preferences": list(PREFERENCE_OPTIONS),
        }
        html_path = out / f"{judge}_blind_ab.html"
        html_path.write_text(_render_html(kit), encoding="utf-8")
        key_path = out / f"{judge}_key.json"
        if not key_path.exists():
            key_path.write_text(
                json.dumps({
                    "judge": judge,
                    "seed": seed,
                    "note": "Unblinded mapping. Do not open this file before the judgments are submitted.",
                    "pairs": key,
                    "blocks": block_key,
                }, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        judges_out[judge] = {
            "html": str(html_path),
            "key": str(key_path),
            "pair_count": len(presentation),
            "block_count": len(blocks),
        }

    manifest = {
        "audition_root": str(audition_root),
        "pair_count": len(kit_pairs),
        "clip_count": len(kit_pairs) * len(CONDITIONS),
        "block_count": len(block_cells),
        "conditions": list(CONDITIONS),
        "questions": [q["id"] for q in QUESTIONS] + [CONSISTENCY_QUESTION["id"]],
        "judges": judges_out,
        "note": "Presentation order and side assignment are randomized per judge; condition labels appear only in the key files.",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return manifest


def package_judge_kits(
    out: pathlib.Path | str,
    dist: pathlib.Path | str,
    judges: tuple[str, ...],
) -> dict:
    """Zip one self-contained kit per judge: the HTML plus the shared clips.

    Keys are never packaged; a judge cannot unblind their own kit.
    """
    import zipfile

    out = pathlib.Path(out)
    dist = pathlib.Path(dist)
    dist.mkdir(parents=True, exist_ok=True)
    clips = sorted((out / "clips").glob("*.wav"))
    packages = {}
    for judge in judges:
        html = out / f"{judge}_blind_ab.html"
        if not html.is_file():
            raise FileNotFoundError(f"missing kit page: {html}")
        target = dist / f"{judge}.zip"
        if target.exists():
            raise FileExistsError(f"refusing to overwrite {target}")
        tmp = target.with_suffix(".tmp.zip")
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.write(html, arcname="listening.html")
            for clip in clips:
                archive.write(clip, arcname=f"clips/{clip.name}")
            archive.writestr("README.txt", _judge_readme(judge))
        tmp.replace(target)
        packages[judge] = {
            "zip": str(target),
            "bytes": target.stat().st_size,
            "html": "listening.html",
            "clips": len(clips),
        }
    return {
        "dist": str(dist),
        "judge_count": len(packages),
        "packages": packages,
        "note": "Each ZIP is independent and contains no key file. Judge order and side assignment differ per ZIP.",
    }


def _judge_readme(judge: str) -> str:
    return (
        f"Blind listening task ({judge})\n"
        "\n"
        "1. Unzip this folder anywhere. Keep listening.html and clips/ together.\n"
        "2. Open listening.html in a browser. Nothing is uploaded; no network is used.\n"
        "3. Part 1: for each pair, rate both clips and say which you prefer.\n"
        "   Part 2: each block has three clips of one voice. Say how far they sound\n"
        "   like one person.\n"
        "4. Press Download answers and send the JSON file back.\n"
        "\n"
        "Headphones are preferred. Do not compare notes with anyone else doing this\n"
        "before you finish. There are no correct answers; the order of clips is\n"
        "randomised per person, so a preference for the left side in one pair means\n"
        "nothing on its own.\n"
    )


def _render_html(kit: dict) -> str:
    payload = json.dumps(kit, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.replace("__PAYLOAD__", payload).replace("__JUDGE__", kit["judge"])


_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Blind listening</title>
<style>
:root{color-scheme:light dark}
body{font:15px/1.5 system-ui,sans-serif;max-width:56rem;margin-inline:auto;padding:1.5rem}
h1{font-size:1.3rem;margin:0 0 .25rem}h2{font-size:1.05rem;margin:1.6rem 0 .5rem}
.meta{color:#666;font-size:.85rem}
.brief{background:#8881;border-radius:6px;padding:.5rem .7rem;margin:.5rem 0;font-style:italic}
.pair,.block{border:1px solid #8884;border-radius:8px;padding:1rem;margin:1.2rem 0}
.pair.done,.block.done{border-color:#2a7}
.sides{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media(max-width:640px){.sides{grid-template-columns:1fr}}
.side{border:1px solid #8883;border-radius:6px;padding:.6rem}
audio{width:100%;margin:.4rem 0}
.q{font-weight:600;margin:.5rem 0 .1rem}
.anchors{display:flex;justify-content:space-between;font-size:.75rem;color:#666}
label{display:block;font-size:.85rem;color:#555;margin-top:.35rem}
select,textarea{font:inherit;padding:.15rem;border:1px solid #8886;border-radius:4px}
textarea{width:100%;min-height:2rem}
button{font:inherit;padding:.4rem 1rem;border-radius:6px;border:1px solid #8886;cursor:pointer}
#bar{position:sticky;top:0;background:Canvas;padding:.5rem 0;border-bottom:1px solid #8884;z-index:2}
</style></head><body>
<div id="bar"><strong id="progress"></strong> <span class="meta" id="status"></span></div>
<h1>Blind listening &mdash; __JUDGE__</h1>
<p class="meta">Each pair is the same spoken text, model and seed; one clip has a style clause added and
the other does not. Which side is which is randomized and hidden, and it differs for every person doing
this. Rate each side on its own, then say which you prefer. In Part 2 all three clips are one voice:
rate how far they sound like one person. Work alone, and do not compare notes with anyone else doing
this until you have sent your answers back.</p>
<h2>Part 1 &mdash; paired comparison</h2>
<div id="pairs"></div>
<h2>Part 2 &mdash; same-voice consistency</h2>
<p class="meta">Each block is three different sentences spoken from one and the same reference
recording of that voice. Nothing about a block tells you which variant produced it.</p>
<div id="blocks"></div>
<p><button id="export">Download answers</button> <span class="meta" id="blocked"></span></p>
<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
const K = JSON.parse(document.getElementById('payload').textContent);
const Q = K.questions, S = K.consistency;
const pairs = K.pairs.map(p => Object.assign({}, p, {ratings:{}, preference:'', notes:''}));
const blocks = K.blocks.map(b => Object.assign({}, b, {rating:'', notes:''}));
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const scale = () => '<option value="">&mdash;</option>' + [1,2,3,4,5].map(v => '<option value="' + v + '">' + v + '</option>').join('');
function qHtml(q) {
  return '<div class="q">' + esc(q.label) + '</div>' +
    '<div class="meta">' + esc(q.hint) + '</div>' +
    '<div class="anchors"><span>' + esc(q.low) + '</span><span>' + esc(q.high) + '</span></div>';
}
function missing() {
  const out = [];
  pairs.forEach(p => {
    Q.forEach(q => { if (!p.ratings[q.id + '|L'] || !p.ratings[q.id + '|R']) out.push(p.pair_id + ' ' + q.label); });
    if (!p.preference) out.push(p.pair_id + ' preference');
  });
  blocks.forEach(b => { if (!b.rating) out.push(b.block_id + ' same-speaker'); });
  return out;
}
function refresh() {
  const bad = missing();
  document.getElementById('progress').textContent =
    pairs.length + ' pairs + ' + blocks.length + ' blocks \u00b7 ' + (bad.length ? bad.length + ' left' : 'complete');
  document.querySelectorAll('.pair').forEach(el => {
    el.classList.toggle('done', !bad.some(x => x.indexOf(el.dataset.pair + ' ') === 0));
  });
  document.querySelectorAll('.block').forEach(el => {
    el.classList.toggle('done', !bad.some(x => x.indexOf(el.dataset.block + ' ') === 0));
  });
}
const rootP = document.getElementById('pairs');
pairs.forEach((p, idx) => {
  const card = document.createElement('section');
  card.className = 'pair';
  card.dataset.pair = p.pair_id;
  let brief = '';
  if (p.brief && p.brief.length) {
    brief = '<div class="brief"><div class="meta">Speaker&rsquo;s other lines (names removed) &mdash; same speaker as both clips:</div>' +
      p.brief.map(esc).join('<br>') + '</div>';
  }
  const sides = ['L','R'].map(side => {
    const clip = p[side === 'L' ? 'left' : 'right'].clip;
    return '<div class="side"><strong>Clip ' + side + '</strong>' +
      '<audio controls preload="none" src="' + esc(clip) + '"></audio>' +
      Q.map(q => qHtml(q) + '<select data-side="' + side + '" data-q="' + q.id + '">' + scale() + '</select>').join('') +
      '</div>';
  }).join('');
  card.innerHTML =
    '<div class="meta">Pair ' + (idx + 1) + ' of ' + pairs.length + ' \u00b7 ' + p.pair_id + '</div>' +
    brief + '<div class="sides">' + sides + '</div>' +
    '<div class="q">Which clip do you prefer overall?</div>' +
    '<select data-pref><option value="">&mdash;</option>' +
    K.preferences.map(o => '<option value="' + esc(o) + '">' + esc(o) + '</option>').join('') + '</select>' +
    '<label>Why? What differs?</label><textarea data-notes></textarea>';
  card.querySelectorAll('select[data-q]').forEach(el => el.addEventListener('input', () => {
    p.ratings[el.dataset.q + '|' + el.dataset.side] = el.value ? Number(el.value) : '';
    refresh();
  }));
  card.querySelector('select[data-pref]').addEventListener('input', e => { p.preference = e.target.value; refresh(); });
  card.querySelector('textarea').addEventListener('input', e => { p.notes = e.target.value; });
  rootP.appendChild(card);
});
const rootB = document.getElementById('blocks');
blocks.forEach((b, idx) => {
  const card = document.createElement('section');
  card.className = 'block';
  card.dataset.block = b.block_id;
  card.innerHTML =
    '<div class="meta">Block ' + (idx + 1) + ' of ' + blocks.length + ' \u00b7 ' + b.block_id + ' \u00b7 three clips</div>' +
    b.clips.map(c => '<audio controls preload="none" src="' + esc(c) + '"></audio>').join('') +
    qHtml(S) + '<select data-consist>' + scale() + '</select>' +
    '<label>Notes</label><textarea data-notes></textarea>';
  card.querySelector('select[data-consist]').addEventListener('input', e => {
    b.rating = e.target.value ? Number(e.target.value) : '';
    refresh();
  });
  card.querySelector('textarea').addEventListener('input', e => { b.notes = e.target.value; });
  rootB.appendChild(card);
});
document.getElementById('export').addEventListener('click', () => {
  const bad = missing();
  const warn = document.getElementById('blocked');
  if (bad.length) {
    warn.textContent = 'Missing: ' + bad.slice(0, 4).join('; ') + (bad.length > 4 ? ' \u2026 (+' + (bad.length - 4) + ')' : '');
    return;
  }
  warn.textContent = '';
  const out = {
    judge: K.judge, seed: K.seed,
    questions: Q.map(q => q.id), consistency_question: S.id,
    pairs: pairs.map(p => ({pair_id: p.pair_id, ratings: p.ratings, preference: p.preference, notes: p.notes})),
    blocks: blocks.map(b => ({block_id: b.block_id, rating: b.rating, notes: b.notes}))
  };
  const blob = new Blob([JSON.stringify(out, null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = K.judge + '_answers.json';
  a.click();
  URL.revokeObjectURL(a.href);
  document.getElementById('status').textContent = 'downloaded \u2014 send that file back';
});
refresh();
</script></body></html>
"""


def summarize_answers(
    answers_paths: list[pathlib.Path | str],
    key_paths: list[pathlib.Path | str],
) -> dict:
    """Unblind judge answers and aggregate identity_only versus identity_style."""
    keys: dict[str, dict] = {}
    for path in key_paths:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        keys[data["judge"]] = {
            "pairs": {row["pair_id"]: row for row in data["pairs"]},
            "blocks": {row["block_id"]: row for row in data.get("blocks") or []},
        }
    by_condition: dict[str, dict[str, list[float]]] = {}
    preferences = {"identity_style": 0, "identity_only": 0, "BOTH_EQUAL": 0, "NEITHER": 0}
    consistency: dict[str, dict[str, list[float]]] = {}
    per_judge = {}
    seen_judges: set[str] = set()
    for path in answers_paths:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        judge = data["judge"]
        if judge in seen_judges:
            # Two people cannot share a judge label: the per-judge totals would
            # silently keep only the last file.
            raise ValueError(
                f"duplicate answers for judge {judge!r}; give each person their own kit"
            )
        seen_judges.add(judge)
        key = keys.get(judge)
        if key is None:
            raise ValueError(f"no key for judge {judge!r}")
        judge_pref = {"identity_style": 0, "identity_only": 0, "BOTH_EQUAL": 0, "NEITHER": 0}
        for row in data["pairs"]:
            mapping = key["pairs"][row["pair_id"]]
            for side, label in (("left", "L"), ("right", "R")):
                condition = mapping[side]
                for name, value in (row.get("ratings") or {}).items():
                    question, _, rated_side = name.partition("|")
                    if rated_side == label and value:
                        by_condition.setdefault(question, {}).setdefault(condition, []).append(float(value))
            preference = row.get("preference") or ""
            if preference in ("BOTH_EQUAL", "NEITHER"):
                mapped = preference
            elif preference == "L":
                mapped = mapping["left"]
            elif preference == "R":
                mapped = mapping["right"]
            else:
                mapped = None
            if mapped:
                judge_pref[mapped] += 1
                preferences[mapped] += 1
        for row in data.get("blocks") or []:
            if not row.get("rating"):
                continue
            mapping = key["blocks"].get(row["block_id"])
            if mapping is None:
                continue
            question = data.get("consistency_question") or "same_speaker"
            consistency.setdefault(question, {}).setdefault(mapping["condition"], []).append(float(row["rating"]))
        per_judge[judge] = judge_pref

    def _agg(groups: dict[str, list[float]]) -> dict:
        return {
            condition: {"n": len(values), "mean": round(sum(values) / len(values), 3) if values else None}
            for condition, values in groups.items()
        }

    return {
        "questions": {question: _agg(groups) for question, groups in by_condition.items()},
        "consistency": {question: _agg(groups) for question, groups in consistency.items()},
        "preferences": preferences,
        "preferences_by_judge": per_judge,
        "note": "Preference and consistency counts come from the unblinded key, never from side labels.",
    }
