# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Fast Reward as the Reward stage: its result converted to ``attribution.reward.v1``.

Fast Reward (:mod:`openjiuwen.agent_evolving.fast_reward`) writes checkable criteria from the user's own
request (what was asked, plus what the best work of the kind does), gathers evidence by running the
deliverable, judges every claim and computes the scores in code.  This module feeds it the stage-one
result and turns its claims into reward dimensions, so the rest of the pipeline (signals, attribution,
the host's display) is unchanged.

The tier of each dimension comes from the claim's status, never from a score: a met claim is perfect
(attribution skips it); a requirement partly met is fair and one not met is weak; a proactive challenge
not (fully) met is fair (room to improve, not a defect).  The dimension score is Fast Reward's own claim
score (met 100, partial 50, unmet 0), shown only.  A claim the evidence could not settle ("unknown") is no
dimension; the overall comment counts it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from openjiuwen.agent_evolving.attribution.model import TaskParseResult
from openjiuwen.agent_evolving.attribution.nodes import TrajectoryNode

#: ``ATTRIBUTION_REWARD=agent`` switches the Reward stage back to the Reward deep agent.
REWARD_ENGINE_ENV = "ATTRIBUTION_REWARD"
#: Seconds Fast Reward may take for one diagnosis (``FAST_REWARD_DEADLINE`` overrides).
DEFAULT_DEADLINE_SECONDS = 600

_REQUIREMENT_TIER = {"met": "perfect", "partial": "fair", "unmet": "weak"}
_CHALLENGE_TIER = {"met": "perfect", "partial": "fair", "unmet": "fair"}
_CLAIM_SCORE = {"met": 100, "partial": 50, "unmet": 0}
_LABELS = {
    "zh": {"floor": "要求", "challenge": "挑战", "unknown": "另有 {count} 项因证据不足未能判定。"},
    "en": {"floor": "Requirement", "challenge": "Challenge", "unknown": "{count} more claims could not be settled."},
}


def fast_reward_enabled() -> bool:
    """Fast Reward is the Reward stage unless ``ATTRIBUTION_REWARD=agent``."""
    return os.environ.get(REWARD_ENGINE_ENV, "fast").strip().lower() != "agent"


def route_from_model(model: Any) -> dict[str, Any]:
    """An OpenAI-compatible route (``model``, ``base_url``, ``api_key``) read from a core ``Model``.

    Raises:
        ValueError: The model carries no name or endpoint (e.g. a model group); set ``FAST_REWARD_CONFIG``.
    """
    client = getattr(model, "model_client_config", None)
    request = getattr(model, "model_config", None)
    name = str(getattr(request, "model_name", "") or "")
    base_url = str(getattr(client, "api_base", "") or "")
    if not name or not base_url:
        raise ValueError("the diagnosis model has no model name or endpoint; set FAST_REWARD_CONFIG to a config file")
    route: dict[str, Any] = {
        "model": name,
        "base_url": base_url,
        "api_key": str(getattr(client, "api_key", "") or ""),
        "provider": str(getattr(client, "client_provider", "") or "OpenAI"),
        "timeout": float(getattr(client, "timeout", 360) or 360),
    }
    extra = getattr(request, "extra_body", None)
    if isinstance(extra, Mapping):
        route["extra_body"] = dict(extra)
    return route


def fast_reward_config(model: Any) -> dict[str, Any]:
    """The Fast Reward config: ``$FAST_REWARD_CONFIG`` or ``~/.fast_reward/config.json`` when present,
    else every model role on the diagnosis model."""
    from openjiuwen.agent_evolving.fast_reward.config import config_path, load_config

    if os.environ.get("FAST_REWARD_CONFIG") or config_path().is_file():
        config = dict(load_config())
    else:
        route = route_from_model(model)
        config = {"producer": route, "judge": route, "evidence_model": route, "criteria_budget": 3}
    config.setdefault("deadline_seconds", float(os.environ.get("FAST_REWARD_DEADLINE") or DEFAULT_DEADLINE_SECONDS))
    return config


def existing_paths(paths: Sequence[str]) -> list[str]:
    """The paths that exist (a deliverable deleted since the run cannot be scored)."""
    return [path for path in paths if Path(path).exists()]


def final_reply(task_parse: TaskParseResult, nodes_by_subagent: Mapping[str, Sequence[TrajectoryNode]]) -> str:
    """The text the user got last among the message artifacts.

    Messages a team member sent inside the team are skipped while a message to the user exists.
    """
    nodes = {node.node_id: node for rows in nodes_by_subagent.values() for node in rows}
    candidates = [(node_id, text) for node_id, text in task_parse.message_artifacts if text]
    to_user = [item for item in candidates if getattr(nodes.get(item[0]), "kind", "") != "team_member"]
    pool = to_user or candidates
    if not pool:
        return ""
    return max(pool, key=lambda item: getattr(nodes.get(item[0]), "start_ts_ns", 0))[1]


def to_reward_payload(result: Mapping[str, Any], language: str = "zh") -> dict[str, Any]:
    """A Fast Reward result as an ``attribution.reward.v1`` payload.

    Each dimension also carries ``tier`` (from the claim's status); ``run_diagnosis`` applies it to the
    signal instead of the score band.

    Raises:
        ValueError: The result has no total score (no evidence) or no claim it could settle.
    """
    labels = _LABELS["en" if str(language).lower().startswith("en") else "zh"]
    total = (result.get("total") or {}).get("score")
    if total is None:
        raise ValueError("Fast Reward found no evidence to score the work")
    dimensions = []
    unknown = 0
    for item in result.get("sub_category_items") or ():
        status = item.get("status")
        challenge = item.get("type") == "proactive_challenge"
        tier = (_CHALLENGE_TIER if challenge else _REQUIREMENT_TIER).get(status)
        if tier is None:
            unknown += 1
            continue
        dimensions.append(
            {
                "category": str(item.get("category_name") or item.get("category_id") or "-"),
                "requirement": f"[{labels['challenge' if challenge else 'floor']}] {item.get('rubric', '')}",
                "comment": str(item.get("comment") or item.get("rubric") or "-"),
                "score": _CLAIM_SCORE[status],
                "tier": tier,
                "related_signal_ids": [],
            }
        )
    if not dimensions:
        raise ValueError("Fast Reward settled no claim")
    comment = str((result.get("total") or {}).get("summary") or "-")
    if unknown:
        comment = f"{comment} {labels['unknown'].format(count=unknown)}"
    categories = [
        {
            "name": str(c.get("category_name") or c.get("category_id") or "-"),
            "comment": str(c.get("comment") or ""),
            **({"score": c["score"]} if isinstance(c.get("score"), (int, float)) else {}),
        }
        for c in result.get("categories") or ()
    ]
    return {
        "schema_version": "attribution.reward.v1",
        "overall": {"comment": comment, "total_score": int(round(float(total)))},
        "categories": categories,
        "dimensions": dimensions,
    }


async def run_fast_reward(
    *,
    task_parse: TaskParseResult,
    nodes_by_subagent: Mapping[str, Sequence[TrajectoryNode]],
    user_attribution: str,
    model: Any,
    language: str,
    status_sink: Any = None,
    model_name: str = "",
    status_counter: list[int] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Score the stage-one deliverables with Fast Reward.

    Returns:
        The ``attribution.reward.v1`` payload and run meta for ``run_meta["reward"]``.

    Raises:
        Exception: Any failure (no route, no evidence, model errors); the caller falls back as for a
            failed Reward run.
    """
    from openjiuwen.agent_evolving.attribution.agent import emit_single_llm_event
    from openjiuwen.agent_evolving.fast_reward import pipeline

    counter = status_counter if status_counter is not None else [0]
    task = task_parse.user_request or task_parse.task_description
    artifacts = existing_paths(task_parse.file_artifacts)
    reply = final_reply(task_parse, nodes_by_subagent)
    config = fast_reward_config(model)
    started = time.time_ns()
    try:
        result = await pipeline.reward(task, artifacts, config, txt_response=reply, user_note=user_attribution or "")
        payload = to_reward_payload(result, language)
    except Exception as exc:
        emit_single_llm_event(
            status_sink,
            stage="reward",
            model=model_name,
            counter=counter,
            started_at_ns=started,
            ended_at_ns=time.time_ns(),
            error=str(exc)[:200] or type(exc).__name__,
        )
        raise
    emit_single_llm_event(
        status_sink,
        stage="reward",
        model=model_name,
        counter=counter,
        started_at_ns=started,
        ended_at_ns=time.time_ns(),
        preview=f"Fast Reward {payload['overall']['total_score']}",
    )
    meta = {
        "engine": "fast_reward",
        "artifacts": artifacts,
        "reply_chars": len(reply),
        "task_from_user": bool(task_parse.user_request),
        "claims": len(result.get("sub_category_items") or ()),
        "warnings": list((result.get("meta") or {}).get("warnings") or ()),
    }
    return payload, meta


__all__ = [
    "REWARD_ENGINE_ENV",
    "fast_reward_config",
    "fast_reward_enabled",
    "existing_paths",
    "final_reply",
    "route_from_model",
    "run_fast_reward",
    "to_reward_payload",
]
