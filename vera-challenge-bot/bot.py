"""
bot.py — Vera challenge bot. Implements the 5-endpoint HTTP contract from
challenge-testing-brief.md:

    POST /v1/context    receive a context push (category/merchant/customer/trigger)
    POST /v1/tick        periodic wake-up; bot may initiate proactive sends
    POST /v1/reply       receive a reply from the simulated merchant/customer
    GET  /v1/healthz     liveness probe
    GET  /v1/metadata    bot identity

Run locally:
    pip install -r requirements.txt
    uvicorn bot:app --host 0.0.0.0 --port 8080

Deploy: see README.md for Render/Railway/Fly instructions.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from composer import compose
from conversation_handlers import respond as reply_respond

app = FastAPI(title="Vera Challenge Bot")
START = time.time()

MAX_ACTIONS_PER_TICK = 20

# ---------------------------------------------------------------------------
# in-memory state (fine per the brief — no restarts during the test window)
# ---------------------------------------------------------------------------

# (scope, context_id) -> {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict[str, Any]] = {}

# conversation_id -> state dict (see conversation_handlers.py docstring)
conversations: dict[str, dict[str, Any]] = {}

# suppression_key -> last-sent unix ts, so the same nudge doesn't fire twice
sent_suppression_keys: dict[str, float] = {}

# suppression_key -> conversation_id already opened for it (avoid duplicate
# conversations for the same trigger across ticks)
suppression_to_conversation: dict[str, str] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get(scope: str, context_id: Optional[str]) -> Optional[dict]:
    if not context_id:
        return None
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


def _category_for_merchant(merchant: dict) -> Optional[dict]:
    slug = merchant.get("category_slug")
    return _get("category", slug)


# ---------------------------------------------------------------------------
# GET /v1/healthz
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


# ---------------------------------------------------------------------------
# GET /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.environ.get("TEAM_NAME", "Solo Builder"),
        "team_members": [os.environ.get("TEAM_MEMBER", "Builder")],
        "model": "rule-based-deterministic-composer-v1",
        "approach": (
            "Deterministic, non-LLM composer: per-trigger-kind templates ground every "
            "message in the specific category/merchant/customer facts present in context "
            "(digest items, signals, offers, performance deltas, peer stats). Reply handling "
            "is a small state machine covering auto-reply detection, explicit intent handoff, "
            "graceful exit, wait-backoff, and off-topic/hostile redirection. Deterministic by "
            "construction (no model sampling), fast (<50ms typical), and never fabricates a "
            "fact that isn't in the pushed context."
        ),
        "contact_email": os.environ.get("TEAM_EMAIL", "team@example.com"),
        "version": "1.0.0",
        "submitted_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in VALID_SCOPES:
        return {"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {sorted(VALID_SCOPES)}"}

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions: list[dict] = []

    for trg_id in body.available_triggers:
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            break

        trigger = _get("trigger", trg_id)
        if not trigger:
            continue

        merchant_id = trigger.get("merchant_id")
        merchant = _get("merchant", merchant_id)
        if not merchant:
            continue
        category = _category_for_merchant(merchant)
        if not category:
            continue

        customer_id = trigger.get("customer_id")
        customer = _get("customer", customer_id) if customer_id else None
        if trigger.get("scope") == "customer" and customer_id and not customer:
            # customer-scoped trigger but we don't have the customer context yet — skip, don't fabricate
            continue

        suppression_key = trigger.get("suppression_key") or f"{trigger.get('kind')}:{merchant_id}"

        # dedup: don't resend the same suppression key
        if suppression_key in sent_suppression_keys:
            continue

        composed = compose(category, merchant, trigger, customer)

        conversation_id = suppression_to_conversation.get(suppression_key) or f"conv_{uuid.uuid4().hex[:12]}"
        suppression_to_conversation[suppression_key] = conversation_id

        conversations[conversation_id] = {
            "turns": [{"from": composed["send_as"], "msg": composed["body"]}],
            "sent_bodies": [composed["body"]],
            "auto_reply_streak": 0,
            "category_slug": category.get("slug"),
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trg_id,
            "last_offer_summary": _extract_offer_summary(composed["body"]),
        }

        sent_suppression_keys[suppression_key] = time.time()

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": _template_params(merchant, customer),
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": suppression_key,
            "rationale": composed["rationale"],
        })

    return {"actions": actions}


def _template_params(merchant: dict, customer: Optional[dict]) -> list[str]:
    name = (customer or merchant).get("identity", {}).get("name", "")
    return [name]


def _extract_offer_summary(body: str) -> str:
    # very light heuristic just so /v1/reply's intent-handoff has *something*
    # concrete to reference ("locking in X now") without re-parsing full state
    if "?" in body:
        # take the clause right before the question as the thing being offered
        return body.split("?")[0].split(".")[-1].strip().lower() or "that"
    return "that"


# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = conversations.setdefault(body.conversation_id, {
        "turns": [], "sent_bodies": [], "auto_reply_streak": 0,
        "merchant_id": body.merchant_id, "customer_id": body.customer_id,
    })

    result = reply_respond(state, body.message)

    # record the incoming turn *after* computing the response, so
    # repeat-detection compares against prior turns, not against itself
    state["turns"].append({"from": body.from_role, "msg": body.message})

    if result.get("action") == "send":
        state["sent_bodies"].append(result.get("body", ""))
        state["turns"].append({"from": "vera", "msg": result.get("body", "")})

    return result
