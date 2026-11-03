# Anonymized JEV Style request and response

Generated from the production builders (`jev.compact_state`,
`jev.build_questions`). The NPC identity, source names, and sheet names are
removed before this point; opaque line IDs are stable hashes of the local line
key plus sampler version. This example uses only four lines, so the application
derives `insufficient_evidence`; a response that claims support cannot override
that.

## Request

```json
{
  "model": "jev-1.14.0",
  "state": {
    "task": "Classify the character's stable, recurring written-dialogue style from the supplied anonymous lines. Do not infer voice acoustics or identity.",
    "dialogue_samples": [
      {"id": "L1F7DF2445BAFF3B4", "text": "Good morrow, traveler. The gate opens at first bell."},
      {"id": "L2464CD6ECADD2863", "text": "I cannot say. Ask the steward."},
      {"id": "L4C06198B12D44119", "text": "Pray, present thy permit before entering the hall."},
      {"id": "L64F332F6445743B8", "text": "We shall speak anon, once the council is convened."}
    ],
    "dialogue_coverage": {
      "rows": 12,
      "unique_lines": 12,
      "sampled_lines": 4,
      "sampled_words": 32,
      "coverage_of_unique_lines": 0.3333,
      "sampled_source_count": 1,
      "sampled_context_count": 3,
      "warning": null
    },
    "evidence_sufficiency": {
      "tier": "insufficient",
      "field": null,
      "sampled_lines": 4,
      "sampled_words": 32,
      "source_count": 1,
      "context_count": 3,
      "unique_corpus_lines": 12,
      "thresholds": {
        "minimum": {"lines": 8, "words": 100, "contexts": 1},
        "supported": {"lines": 20, "words": 300, "contexts": 2},
        "strong": {"lines": 40, "words": 600, "contexts": 3}
      },
      "field_multiplier": 1.0
    },
    "sample_manifest_hash": "033bc6b3e70a8b8b6f46438c6287cf8d0d10baffe6a519b161d2bc51a43a2272",
    "taxonomy_version": "style-taxonomy-3.0",
    "prompt_version": "style-prompt-3.0",
    "sampler_version": "dialogue-sampler-3.0",
    "guardrails": [
      "Classify only the recurring written-dialogue style of one character.",
      "Do not infer race, gender, age, body, occupation, status, personality, or expected archetype.",
      "Dialogue text cannot establish pitch, timbre, resonance, accent, loudness, speed, pauses, or intonation.",
      "Judge recurring patterns across ordinary lines; a dramatic scene is not the default.",
      "Use the value `mixed` only when two or more patterns genuinely recur; list both patterns and their line IDs.",
      "Do not assign an evidence status. The application derives status from the submitted sample and your cited lines.",
      "Abstain with a null choice when the sample is too sparse.",
      "Cite only supplied line IDs and give a short rationale tied to those lines. Never invent an ID, quote, or source fact.",
      "Model self-reported confidence is diagnostic only and is not calibrated."
    ],
    "budget_reduction": {
      "applied": false,
      "original_samples": 4,
      "final_samples": 4,
      "reason": null,
      "strategy": "balanced_length_and_context",
      "length_buckets_retained": ["short"],
      "lost_length_buckets": [],
      "original_source_count": 1,
      "final_source_count": 1,
      "lost_source_count": 0,
      "original_context_count": 3,
      "final_context_count": 3,
      "lost_context_count": 0
    }
  },
  "questions": {
    "speech_style.register": {
      "type": "choice",
      "instructions": "Which register does the character habitually use across ordinary dialogue? Use mixed for recurring shifts and abstain when the sample is too sparse.",
      "criteria": {
        "formal": "Consistently ceremonial or official grammar and address forms; vocabulary complexity and contractions are judged separately.",
        "neutral": "Ordinary grammatical register with no sustained ceremonial or casual stance.",
        "conversational": "Consistently casual grammar and address forms; do not infer this from contractions or slang alone.",
        "mixed": "Formality shifts with a stable contextual rule, such as formal address in one setting and casual grammar in another."
      },
      "evidence_required": true,
      "max_supporting": 6,
      "max_counterexamples": 3,
      "min_supporting": 3,
      "min_mixed_patterns": 2,
      "min_pattern_support": 3,
      "rationale_required": true
    }
  }
}
```

The remaining four questions use the same envelope:
`speech_style.lexical_complexity`, `speech_style.sentence_complexity`,
`speech_style.colloquialism`, and `speech_style.discourse_style`.

## Response proposals

The endpoint returns exactly the five requested question IDs. It proposes a
value and evidence; it does not decide status. `status` is omitted here
deliberately. If an endpoint emits one, it is stored only as
`model_reported_status`.

Supported-looking field fragment (the actual response must contain all five
question IDs):

```json
{
  "model": "jev-1.14.0",
  "answers": {
    "speech_style.register": {
      "type": "choice",
      "choice": "formal",
      "supporting_line_ids": ["L1F7DF2445BAFF3B4", "L4C06198B12D44119", "L64F332F6445743B8"],
      "counterexample_line_ids": [],
      "rationale": "Ceremonial address and conventional request wording recur across contexts."
    }
  }
}
```

A `mixed` proposal must cite both recurring patterns separately. Each pattern
needs at least three unique request lines; patterns cannot reuse each other's
lines or the top-level citation lists. The six IDs below are illustrative from
a larger request, because the four-line request above cannot supply six unique
lines:

```json
{
  "choice": "mixed",
  "patterns": [
    {
      "label": "formal",
      "line_ids": ["L1F7DF2445BAFF3B4", "L4C06198B12D44119", "L64F332F6445743B8"],
      "rationale": "Ceremonial address in official settings."
    },
    {
      "label": "conversational",
      "line_ids": ["L2464CD6ECADD2863", "L18B75AC11E2F09C4", "L90C2A6E4B17D3F51"],
      "rationale": "Casual phrasing when speaking with companions."
    }
  ],
  "supporting_line_ids": [],
  "counterexample_line_ids": [],
  "rationale": "Two stable registers split by setting."
}
```

Abstention:

```json
{
  "choice": null,
  "supporting_line_ids": [],
  "counterexample_line_ids": [],
  "rationale": "The supplied sample does not establish a stable pattern."
}
```

Every cited ID must exist in the exact post-trim request sample. Unknown,
forged, duplicate, or cross-listed IDs reject the field; valid citations plus
the request-visible tier gate decide `supported`, `provisional`,
`insufficient_evidence`, or `unresolved`.
