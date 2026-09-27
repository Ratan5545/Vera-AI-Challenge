"""
conversation_handlers.py — multi-turn reply logic.

respond(state, merchant_message) -> dict with keys: action, body?, cta?,
wait_seconds?, rationale.

Addresses the three things the brief explicitly calls out as differentiators
(challenge-brief.md §9-§12):
  1. Auto-reply detection (same canned text repeating) -> try once, then exit.
  2. Intent-handoff -> explicit "yes/let's do it" skips straight to action,
     never back to a qualifying question.
  3. Graceful exit -> hard "not interested" / repeated silence ends politely.
  4. Off-topic / hostile -> stay on-mission, polite, redirect once.

`state` is a plain dict the bot server maintains per conversation_id:
{
  "turns": [{"from": "...", "msg": "..."}, ...],
  "sent_bodies": [...],
  "auto_reply_streak": 0,
  "category": {...}, "merchant": {...}, "trigger": {...}, "customer": {...},
  "last_offer_summary": "...",
}
"""

from __future__ import annotations
import re
from typing import Any


AUTO_REPLY_PHRASES = [
    "thank you for contacting", "we will get back to you", "automated assistant",
    "auto reply", "auto-reply", "team tak pahuncha", "shukriya", "currently unavailable",
    "we have received your message", "out of office",
]

INTENT_YES_PATTERNS = [
    r"\byes\b", r"\bsure\b", r"\bgo ahead\b", r"\blet'?s do it\b", r"\bok(ay)? send\b",
    r"\bplease do\b", r"\bdo it\b", r"\bhaan\b", r"\bkaro\b", r"\bbhej do\b",
    r"\bconfirm\b", r"\bbook (it|1|2)\b", r"^\s*1\s*$", r"^\s*2\s*$", r"\bi want to join\b",
]

INTENT_NO_PATTERNS = [
    r"\bnot interested\b", r"\bno thanks\b", r"\bstop\b", r"\bnahi chahiye\b",
    r"\bband karo\b", r"\bunsubscribe\b", r"\bleave me alone\b", r"\bdon'?t (contact|message) me\b",
]

WAIT_PATTERNS = [
    r"\bcall you (later|back)\b", r"\bbusy right now\b", r"\bkal batata\b", r"\blater\b",
    r"\bwill (check|revert|get back)\b", r"\bgive me (a|some) time\b", r"\bnot now\b",
]

ABUSE_PATTERNS = [r"\bidiot\b", r"\bstupid\b", r"\bshut up\b", r"\bnonsense\b", r"\bbekar\b"]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _matches_any(patterns: list[str], text: str) -> bool:
    t = _norm(text)
    return any(re.search(p, t) for p in patterns)


def _is_auto_reply(state: dict, message: str) -> bool:
    if _matches_any(AUTO_REPLY_PHRASES, message):
        return True
    prior_incoming = [t["msg"] for t in state.get("turns", []) if t.get("from") in ("merchant", "customer")]
    same_as_before = sum(1 for m in prior_incoming if _norm(m) == _norm(message))
    return same_as_before >= 1 and len(_norm(message)) > 0 and _norm(message) in [_norm(m) for m in prior_incoming]


def respond(state: dict, merchant_message: str) -> dict:
    state.setdefault("turns", [])
    state.setdefault("auto_reply_streak", 0)
    state.setdefault("sent_bodies", [])

    msg = merchant_message or ""

    # 1) auto-reply detection — try exactly once, then exit
    if _is_auto_reply(state, msg):
        state["auto_reply_streak"] = state.get("auto_reply_streak", 0) + 1
        if state["auto_reply_streak"] == 1:
            body = ("Samajh gayi — before this goes to the team, want to take 2 minutes "
                     "yourself to see exactly what's pending? Chalega?")
            return {"action": "send", "body": body, "cta": "binary_yes_stop",
                    "rationale": "First auto-reply detected; one gentle nudge before disengaging"}
        return {"action": "end",
                "rationale": "Second consecutive auto-reply detected — this is a canned WhatsApp Business "
                              "response, not the owner. Exiting to avoid wasting turns."}

    # 2) hard decline -> graceful exit
    if _matches_any(INTENT_NO_PATTERNS, msg):
        return {"action": "end",
                "rationale": "Merchant signaled not interested / opt-out; exiting immediately and politely."}

    # 3) explicit yes / intent-to-act -> skip straight to action, never re-qualify
    if _matches_any(INTENT_YES_PATTERNS, msg):
        offer_bit = state.get("last_offer_summary") or "the next step"
        body = f"On it — locking in {offer_bit} now. I'll confirm here as soon as it's done."
        return {"action": "send", "body": body, "cta": "none",
                "rationale": "Detected explicit affirmative intent; routed directly to action instead of "
                              "re-asking a qualifying question."}

    # 4) asked for time -> back off
    if _matches_any(WAIT_PATTERNS, msg):
        return {"action": "wait", "wait_seconds": 1800,
                "rationale": "Merchant asked for time; backing off 30 minutes rather than pushing again."}

    # 5) abusive -> stay calm, don't escalate, one polite redirect
    if _matches_any(ABUSE_PATTERNS, msg):
        body = "No worries, I'll leave it here for now. If you want help with your listing later, just say so."
        return {"action": "send", "body": body, "cta": "none",
                "rationale": "Message read as hostile; de-escalating politely, not matching tone, staying on-mission."}

    # 6) off-topic question -> answer briefly that it's outside scope, redirect once
    off_topic_markers = ["gst", "income tax", "loan", "visa", "insurance claim"]
    if any(m in _norm(msg) for m in off_topic_markers):
        body = ("That's outside what I can help with directly — I'm focused on your growth/marketing side. "
                "Should we get back to what we were discussing?")
        return {"action": "send", "body": body, "cta": "open_ended",
                "rationale": "Off-topic request detected; politely declined and redirected back to mission."}

    # 7) default — engaged, ambiguous reply: acknowledge + advance one concrete step
    turn_count = len([t for t in state.get("turns", []) if t.get("from") in ("merchant", "customer")])
    if turn_count >= 3:
        return {"action": "end",
                "rationale": "3+ unanswered/ambiguous turns with no clear signal; disengaging gracefully per "
                              "'knowing when to stop' guidance rather than continuing to nudge."}
    body = "Got it — want me to go ahead and put that together, or is there something specific you'd like changed first?"
    return {"action": "send", "body": body, "cta": "open_ended",
            "rationale": "Ambiguous/engaged reply; advancing the conversation with one concrete low-friction ask."}
