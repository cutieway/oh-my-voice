from __future__ import annotations

import datetime as dt
import hashlib
from abc import ABC, abstractmethod
from typing import Any

from . import CLASSIFIER_CONFIG_VERSION
from .cache import object_hash
from .config import DEFAULT_THRESHOLDS
from .jev import build_questions
from .mood import (
    aggregate_mood,
    build_mood_chunks,
    insufficient_volume,
    plan_chunk_count,
)


class ClassifierProvider(ABC):
    name = "base"
    model_id = "unknown"
    # True when the provider can be called once per dialogue window, which is
    # how a character with more lines than the context window is judged.
    supports_chunks = False

    @abstractmethod
    def classify(self, dossier: dict, questions: dict[str, dict]) -> dict:
        raise NotImplementedError

    def classify_chunk(self, dossier: dict, questions: dict[str, dict], chunk: dict) -> dict:
        raise NotImplementedError(f"{self.name} provider does not support dialogue chunks")


class RulesProvider(ClassifierProvider):
    name = "rules"
    model_id = "rules-1.0"

    def __init__(self, vocabulary: dict):
        self.vocabulary = vocabulary

    def classify(self, dossier: dict, questions: dict[str, dict]) -> dict:
        identity = dossier.get("identity") or {}
        analysis = (dossier.get("dialogue_analysis") or {}).get("interpretation") or {}
        answers = {}
        for question_id, question in questions.items():
            kind = question["type"]
            if kind == "noul":
                if question_id == "identity_resolved":
                    yes = (
                        identity.get("resolution_status") == "eligible"
                        and identity.get("npc_id") is not None
                        and not identity.get("identity_ambiguous")
                        and not identity.get("has_se_voice_reference")
                    )
                elif question_id == "voice_evidence_present":
                    yes = bool(dossier.get("voice_evidence"))
                else:
                    yes = False
                p_yes = 0.99 if yes else 0.01
                answers[question_id] = {"type": "noul", "noul": p_yes}
            elif kind == "score":
                if question_id == "individual_design":
                    count = int((dossier.get("statistics") or {}).get("dialogue_count", 0))
                    has_voice_evidence = bool(dossier.get("voice_evidence"))
                    score = 4 if has_voice_evidence else 2 if count >= 50 else 1
                else:
                    score = 0
                score = max(0, min(len(question["criteria"]) - 1, score))
                probabilities = {
                    str(index): 1.0 if index == score else 0.0
                    for index in range(len(question["criteria"]))
                }
                answers[question_id] = {
                    "type": "score",
                    "score": float(score),
                    "legend": {str(index): level for index, level in enumerate(question["criteria"])},
                    "probabilities": probabilities,
                    "confidence": 1.0,
                }
            elif kind == "choice":
                preferred = self._preferred(question_id, identity, analysis, question)
                criteria = question["criteria"]
                if preferred not in criteria:
                    preferred = next(iter(criteria))
                probabilities = {key: 1.0 if key == preferred else 0.0 for key in criteria}
                answers[question_id] = {
                    "type": "choice",
                    "choice": preferred,
                    "probabilities": probabilities,
                    "confidence": 1.0,
                }
            else:
                raise ValueError(f"unsupported classification question type: {kind}")
        return {
            "model": self.model_id,
            "answers": answers,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    def _preferred(self, question_id: str, identity: dict, analysis: dict, question: dict) -> str:
        if question_id.startswith("speech_style."):
            field = question_id.split(".", 1)[1]
            value = analysis.get(field)
            if field == "speech_habits":
                value = "none_observed"
            return value
        field = question_id.removeprefix("identity.")
        defaults = self.vocabulary.get("classification", {}).get("defaults", {})
        return defaults.get(field) or next(iter(question["criteria"]))


def utc_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _answer_for(decisions: dict, question_id: str) -> dict:
    try:
        return decisions["answers"][question_id]
    except KeyError as exc:
        raise ValueError(f"classification response is missing {question_id}") from exc


def _choice(answer: dict, thresholds: dict) -> tuple[str | None, dict]:
    distribution = answer["probabilities"]
    selected = answer["choice"]
    probability = distribution[selected]
    confidence = float(answer["confidence"])
    accepted = (
        confidence >= thresholds["choice_confidence"]
        and probability >= thresholds["choice_probability"]
    )
    return (selected if accepted else None), {
        "confidence": confidence,
        "selected_probability": probability,
        "accepted": accepted,
    }


def _noul(answer: dict, thresholds: dict) -> dict:
    probability = float(answer["noul"])
    if probability >= thresholds["noul_yes"]:
        state = "yes"
    elif probability <= thresholds["noul_no"]:
        state = "no"
    else:
        state = "uncertain"
    return {"yes_probability": probability, "decision": state}


def _score(answer: dict, thresholds: dict) -> dict:
    confidence = float(answer["confidence"])
    return {
        "score": float(answer["score"]),
        "confidence": confidence,
        "accepted": confidence >= thresholds["choice_confidence"],
    }


def _mood_decision(
    selected: str | None,
    choice_check: dict,
    justified_check: dict,
    answer: dict,
    policy: dict,
    line_count: int,
    provider: str,
) -> dict:
    """Decide the one archon a pack uses, defaulting to neutral.

    Three independent gates must all pass before a non-neutral preset is
    proposed: the choice cleared the mood thresholds, `mood.justified` is yes,
    and the character has enough dialogue for a baseline register to exist.
    """
    floor = int(policy["min_dialogue_lines_for_mood"])
    justified = justified_check.get("decision") == "yes"
    preset = selected if selected not in (None, "none", "neutral") else None
    blocked_by = []
    if selected is None:
        blocked_by.append("mood choice fell below the mood confidence and probability thresholds")
    elif preset is None:
        blocked_by.append("answer was none/neutral: no consistent default register")
    if not justified:
        blocked_by.append("mood.justified is not yes")
    if line_count < floor:
        blocked_by.append(f"{line_count} unique lines is below the mood floor {floor}")
    accepted = not blocked_by
    return {
        "preset": preset if accepted else "none",
        "archon": preset if accepted else "neutral",
        "accepted": accepted,
        "justified": justified,
        "gate": {
            "selected": selected,
            "confidence": choice_check.get("confidence"),
            "selected_probability": choice_check.get("selected_probability"),
            "choice_accepted": bool(choice_check.get("accepted")),
            "yes_probability": justified_check.get("yes_probability"),
            "line_count": line_count,
            "min_dialogue_lines": floor,
            "thresholds": {
                "confidence": policy["mood_confidence"],
                "probability": policy["mood_probability"],
            },
        },
        "blocked_by": blocked_by,
        "probabilities": answer.get("probabilities"),
        "method": f"{provider}_choice" if accepted else "default",
    }


def classify_dossier(
    dossier: dict,
    provider: ClassifierProvider,
    vocabulary: dict,
    *,
    model_id: str | None = None,
    thresholds: dict | None = None,
    min_confidence: float | None = None,
) -> dict:
    policy = dict(DEFAULT_THRESHOLDS)
    policy.update(vocabulary.get("classification", {}).get("thresholds", {}))
    if thresholds:
        policy.update(thresholds)
    if min_confidence is not None:
        policy["choice_confidence"] = float(min_confidence)
    identity = dossier.get("identity") or {}
    canonical_id = str(identity.get("canonical_id") or "")
    if (
        identity.get("resolution_status") != "eligible"
        or identity.get("npc_id") is None
        or identity.get("has_se_voice_reference")
        or identity.get("identity_ambiguous")
    ):
        return {
            "schema_version": "1.0",
            "canonical_id": canonical_id,
            "dossier_hash": dossier.get("input_hash"),
            "provider": provider.name,
            "model_id": model_id or provider.model_id,
            "status": "needs_review",
            "assignment": None,
            "review_reasons": list(identity.get("review_reasons") or []) + [
                "identity failed the conservative synthetic-candidate gate"
            ],
            "decisions": {},
        }

    questions = build_questions(vocabulary)
    statistics = dossier.get("statistics") or {}
    line_count = int(statistics.get("unique_line_count") or statistics.get("dialogue_count") or 0)
    mood_questions = {
        question_id: question
        for question_id, question in questions.items()
        if question_id.startswith("mood.")
    }
    base_questions = {
        question_id: question
        for question_id, question in questions.items()
        if not question_id.startswith("mood.")
    }
    chunked = bool(getattr(provider, "supports_chunks", False))
    chunk_plan: list[dict] = []
    chunk_responses: list[dict] = []
    if chunked:
        # Mood is asked per chunk, never alongside the style questions: the
        # style call sees the 24-line sample, and a mood answer drawn from it
        # would be the single-scene guess the chunking exists to avoid.
        response = (
            provider.classify(dossier, base_questions)
            if base_questions
            else {
                "model": model_id or provider.model_id,
                "answers": {},
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        )
        pool = dossier.get("dialogue_pool") or dossier.get("dialogue_samples") or []
        chunk_count = plan_chunk_count(line_count, policy)
        if chunk_count and mood_questions and chunk_count > 1 and not dossier.get("dialogue_pool"):
            # A dossier built before the mood pool carries only the 24-line style
            # sample. Chunking that would present one window as if it were the
            # corpus, which is exactly the single-scene guess this path exists to
            # prevent, so the refusal is explicit instead of silent.
            mood: dict | None = insufficient_volume(
                line_count, policy, provider.name, "insufficient_pool"
            )
        elif chunk_count and pool and mood_questions:
            chunk_plan = build_mood_chunks(pool, chunk_count, policy)
            for chunk in chunk_plan:
                chunk_responses.append(
                    provider.classify_chunk(
                        dossier, mood_questions, {**chunk, "count": len(chunk_plan)}
                    )
                )
            mood = aggregate_mood(
                [chunk_response["answers"] for chunk_response in chunk_responses],
                policy,
                line_count=line_count,
                provider=provider.name,
                chunk_lines=[len(chunk["lines"]) for chunk in chunk_plan],
            )
        else:
            mood = insufficient_volume(line_count, policy, provider.name)
    else:
        response = provider.classify(dossier, questions)
        mood = None
    answers = dict(response["answers"])
    if chunk_responses:
        # The aggregate is exposed under the same question IDs, so every
        # downstream reader sees one mean distribution instead of N answers.
        answers["mood.preset"] = {
            "type": "choice",
            "choice": mood["gate"]["selected"],
            "probabilities": mood["probabilities"],
            "confidence": mood["gate"]["confidence"],
        }
        answers["mood.justified"] = {"type": "noul", "noul": mood["gate"]["yes_probability"]}
    decision_checks = {}
    accepted_choices = {}
    # Mood is held to its own, stricter bar: a wrong default archon changes how
    # every line of a pack sounds, while a wrong style word only changes wording.
    mood_policy = {
        **policy,
        "choice_confidence": policy["mood_confidence"],
        "choice_probability": policy["mood_probability"],
    }
    for question_id, answer in answers.items():
        if answer["type"] == "choice":
            selected, check = _choice(
                answer, mood_policy if question_id == "mood.preset" else policy
            )
            decision_checks[question_id] = check
            if selected is not None:
                accepted_choices[question_id] = selected
        elif answer["type"] == "noul":
            decision_checks[question_id] = _noul(answer, policy)
        elif answer["type"] == "score":
            decision_checks[question_id] = _score(answer, policy)

    usage = {
        "input_tokens": int((response.get("usage") or {}).get("input_tokens") or 0),
        "output_tokens": int((response.get("usage") or {}).get("output_tokens") or 0),
    }
    for chunk_response in chunk_responses:
        chunk_usage = chunk_response.get("usage") or {}
        usage["input_tokens"] += int(chunk_usage.get("input_tokens") or 0)
        usage["output_tokens"] += int(chunk_usage.get("output_tokens") or 0)

    identity_resolved = decision_checks.get("identity_resolved", {}).get("decision") == "yes"
    if not identity_resolved:
        return {
            "schema_version": "1.0",
            "canonical_id": canonical_id,
            "npc_id": identity.get("npc_id"),
            "name": identity.get("name"),
            "dossier_hash": dossier.get("input_hash"),
            "provider": provider.name,
            "model_id": response.get("model") or model_id or provider.model_id,
            "usage": usage,
            "questions": questions,
            "answers": answers,
            "decision_checks": decision_checks,
            "status": "needs_review",
            "assignment": None,
            "review_reasons": ["Jev/rules provider did not accept canonical identity resolution"],
        }

    score = decision_checks.get("individual_design", {})
    voice_present = decision_checks.get("voice_evidence_present", {}).get("decision") == "yes"
    distinctive = (
        score.get("accepted", False)
        and score.get("score", 0) >= policy["distinctiveness_score"]
        and (
            voice_present
            or int((dossier.get("statistics") or {}).get("dialogue_count", 0))
            >= policy["min_dialogue_lines_for_individual"]
        )
    )
    speech_fields = (
        "register", "vocabulary_complexity", "sentence_complexity", "colloquialism",
        "rhetorical_style",
    )
    speech_style = {}
    analysis = (dossier.get("dialogue_analysis") or {}).get("interpretation") or {}
    style_floor = policy["min_dialogue_lines_for_style"]
    style_sufficient = line_count >= style_floor
    for field in speech_fields:
        question_id = f"speech_style.{field}"
        if not style_sufficient:
            # Too few lines to describe a register. The deterministic estimate is
            # kept for the audit, but it never reaches a profile: a six-line NPC
            # renders from identity alone rather than a confident guess.
            speech_style[field] = {
                "value": None,
                "method": "insufficient_dialogue_volume",
                "deterministic_estimate": analysis.get(field),
                "line_count": line_count,
                "confidence": None,
                "probabilities": None,
            }
            continue
        value = accepted_choices.get(question_id)
        if value is None:
            value = analysis.get(field)
        if value is not None:
            speech_style[field] = {
                "value": value,
                "method": (
                    f"{provider.name}_choice" if question_id in accepted_choices
                    else "deterministic_dialogue_analysis"
                ),
                "confidence": decision_checks.get(question_id, {}).get("confidence"),
                "probabilities": (answers.get(question_id) or {}).get("probabilities"),
            }
    speech_style["speech_habits"] = {
        "value": analysis.get("speech_habits", ["none_observed"]),
        "method": "deterministic_dialogue_analysis",
        "confidence": None,
    }

    if mood is None:
        mood = _mood_decision(
            accepted_choices.get("mood.preset"),
            decision_checks.get("mood.preset") or {},
            decision_checks.get("mood.justified") or {},
            answers.get("mood.preset") or {},
            policy,
            line_count,
            provider.name,
        )
    else:
        mood = {
            **mood,
            "chunking": {
                "chunks": len(chunk_plan),
                "chunk_chars": int(policy["mood_chunk_chars"]),
                "overlap": float(policy["mood_chunk_overlap"]),
                "pool_lines": len(dossier.get("dialogue_pool") or dossier.get("dialogue_samples") or []),
                "chunk_line_counts": [len(chunk["lines"]) for chunk in chunk_plan],
            },
        }

    review_reasons = []
    if "insufficient_pool" in (mood.get("blocked_by") or []):
        review_reasons.append(
            "dossier has no mood pool; re-run `dossiers` so mood can be judged on chunks"
        )
    if not style_sufficient:
        review_reasons.append(
            f"dialogue volume {line_count} is below the style floor {style_floor}; no style recorded"
        )
    if not score.get("accepted"):
        review_reasons.append("distinctiveness score is below the confidence threshold")
    if score.get("score", 0) < policy["distinctiveness_score"]:
        review_reasons.append("shared archetype is preferred for the available evidence")
    if not voice_present:
        review_reasons.append("no source explicitly describes acoustic voice qualities")
    if any(not check.get("accepted", True) for key, check in decision_checks.items()
           if key.startswith("identity.") or key.startswith("speech_style.")):
        review_reasons.append("one or more closed-vocabulary choices fell below confidence thresholds")
    lines = int((dossier.get("statistics") or {}).get("dialogue_count", 0))
    if lines < policy["min_dialogue_lines_for_review"]:
        review_reasons.append("very little dialogue is available")

    status = "ready_for_audition"
    if review_reasons and (
        lines < policy["min_dialogue_lines_for_review"]
        or any("confidence threshold" in reason for reason in review_reasons)
    ):
        status = "needs_review"
    configuration = {
        "config_version": CLASSIFIER_CONFIG_VERSION,
        "thresholds": policy,
        "question_count": len(questions),
        "model_requested": model_id or provider.model_id,
        "vocabulary_hash": object_hash(vocabulary),
    }
    return {
        "schema_version": "1.0",
        "canonical_id": canonical_id,
        "npc_id": identity.get("npc_id"),
        "name": identity.get("name"),
        "speaker_ids": identity.get("speaker_ids", []),
        "aliases": identity.get("aliases", []),
        "dossier_hash": dossier.get("input_hash"),
        "configuration_hash": object_hash(configuration),
        "configuration": configuration,
        "provider": provider.name,
        "model_id": response.get("model") or model_id or provider.model_id,
        "timestamp": utc_timestamp(),
        "usage": usage,
        "questions": questions,
        "answers": answers,
        "decision_checks": decision_checks,
        "distinctive": distinctive,
        "mood": mood,
        "pack_archon": mood["archon"],
        "speech_style": speech_style,
        "voice_claims": [
            {
                "field": evidence.get("field"),
                "source": evidence.get("source"),
                "revision_id": evidence.get("revision_id"),
                "quote": evidence.get("quote"),
                "evidence_level": evidence.get("level", "explicit"),
            }
            for evidence in dossier.get("voice_evidence") or []
        ],
        "status": status,
        "review_reasons": sorted(set(review_reasons)),
        "human_approval": None,
    }
