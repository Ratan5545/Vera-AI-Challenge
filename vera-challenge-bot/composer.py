"""
composer.py — the deterministic message-composition engine for the Vera challenge bot.

compose(category, merchant, trigger, customer=None) -> dict
    Pure function. Same inputs -> same output, every time. No LLM call, no
    randomness, no network I/O — this keeps the bot fast (<30s easily), cheap,
    and fully reproducible for the judge.

Design principles (see challenge-brief.md §5, §10, §11):
  - Anchor every message on a *specific* fact pulled straight out of the
    context (a number, a date, a headline, a signal) — never invent one.
  - One primary CTA. Binary yes/no for action triggers, open-ended for
    "want me to draft X" offers, none for pure FYI.
  - Match category voice (clinical/peer for dentists, energetic for gyms,
    trustworthy-precise for pharmacies, etc.) and the merchant/customer's
    language preference (hi-en code-mix where indicated).
  - Never fabricate: if a needed field is missing from the context, fall
    back to a generic-but-still-grounded phrasing rather than making
    something up.
"""

from __future__ import annotations
from typing import Any, Optional


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _signals_map(merchant: dict) -> dict[str, Optional[str]]:
    """Turn ['stale_posts:22d', 'ctr_below_peer_median'] into
    {'stale_posts': '22d', 'ctr_below_peer_median': None}."""
    out = {}
    for s in merchant.get("signals", []) or []:
        if ":" in s:
            k, v = s.split(":", 1)
            out[k] = v
        else:
            out[s] = None
    return out


def _digest_item(category: dict, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for item in category.get("digest", []) or []:
        if item.get("id") == item_id:
            return item
    return None


def _offer_by_keyword(merchant: dict, *keywords: str) -> Optional[dict]:
    keywords = [k.lower() for k in keywords]
    for o in merchant.get("offers", []) or []:
        title = (o.get("title") or "").lower()
        if o.get("status") == "active" and any(k in title for k in keywords):
            return o
    for o in merchant.get("offers", []) or []:
        if o.get("status") == "active":
            return o
    return None


def _first_name(name: str) -> str:
    if not name:
        return "there"
    n = name.strip()
    for prefix in ("Dr. ", "Dr ", "Mr. ", "Mr ", "Mrs. ", "Mrs "):
        if n.startswith(prefix):
            n = n[len(prefix):]
            break
    first = n.split()[0] if n.split() else n
    # strip trailing possessive ("Meera's" -> "Meera")
    if first.endswith("'s"):
        first = first[:-2]
    elif first.endswith("\u2019s"):
        first = first[:-2]
    return first or n


def _uses_hindi_mix(languages: list[str] | None, lang_pref: str | None) -> bool:
    if lang_pref:
        lp = lang_pref.lower()
        if "hi" in lp and "en" in lp:
            return True
        if lp.strip() == "hi":
            return True
    if languages and "hi" in [l.lower() for l in languages]:
        return True
    return False


def _connector(hindi_mix: bool, kind: str = "interest") -> str:
    """Small natural Hindi-English connector phrases, used sparingly."""
    if not hindi_mix:
        return {
            "interest": "Want me to",
            "confirm": "Shall I go ahead?",
            "check": "Worth a look?",
        }[kind]
    return {
        "interest": "Chahiye toh main",
        "confirm": "Chalega?",
        "check": "Ek baar dekh lengi?",
    }[kind]


def _salutation_for_merchant(category: dict, merchant: dict) -> str:
    """Prefer the merchant's own owner_first_name (a real identity field);
    fall back to parsing the business display name only if that's missing."""
    identity = merchant.get("identity", {}) or {}
    display_name = identity.get("name", "")
    first = identity.get("owner_first_name") or _first_name(display_name)
    examples = (category.get("voice") or {}).get("salutation_examples") or []
    if examples and "Dr." in examples[0] and display_name.lower().startswith("dr"):
        return f"Dr. {first}"
    return first


def _salutation(category: dict, display_name: str) -> str:
    """Back-compat helper for spots that only have the display name (no
    full merchant dict) — e.g. customer-facing copy referencing the
    merchant's business name rather than a salutation."""
    return _first_name(display_name)


def _pct(x: float) -> str:
    return f"{abs(x) * 100:.0f}%"


def _fmt_money(v) -> str:
    try:
        return f"₹{int(v):,}"
    except Exception:
        return str(v)


# --------------------------------------------------------------------------
# per-trigger-kind hook builders
# Each returns: (hook_text, ask_text, cta_type, rationale)
# cta_type ∈ {"binary_yes_stop", "open_ended", "none"}
# --------------------------------------------------------------------------

def _hook_research_digest(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    item = _digest_item(category, payload.get("top_item_id"))
    name = _salutation_for_merchant(category, merchant)
    sig = _signals_map(merchant)
    if not item:
        return (
            f"{name}, this week's {category.get('display_name', category.get('slug'))} "
            f"digest has a new item worth a look.",
            f"{_connector(hindi)} send you the summary?",
            "open_ended",
            "Research digest fired but no matching digest item was found in context; kept the ask generic rather than inventing a headline.",
        )
    seg_note = ""
    if item.get("patient_segment") == "high_risk_adults" and "high_risk_adult_cohort" in sig:
        seg_note = " — one item relevant to your high-risk adult patients"
    n = item.get("trial_n")
    trial_note = f" ({n:,}-patient trial)" if n else ""
    hook = (
        f"{name}, {item.get('source', 'this week\u2019s digest')} landed{seg_note}. "
        f"{item.get('title')}{trial_note}."
    )
    ask = f"{_connector(hindi)} pull the full item + draft a patient-ed WhatsApp you can share? — {item.get('source')}"
    return (
        hook, ask, "open_ended",
        f"External research digest ({item.get('id')}) matched to merchant's own signal(s): {list(sig)[:3]}",
    )


def _hook_regulation_change(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    item = _digest_item(category, payload.get("top_item_id"))
    name = _salutation_for_merchant(category, merchant)
    deadline = payload.get("deadline_iso", "").split("T")[0]
    if item:
        hook = f"{name}, heads up — {item.get('title')} ({item.get('source')})."
        detail = item.get("summary", "")
    else:
        hook = f"{name}, a regulation change affecting your category is coming."
        detail = ""
    ask = f"Deadline {deadline}. {_connector(hindi)} check your setup against it before then?" if deadline else f"{_connector(hindi)} check your setup against it?"
    return (f"{hook} {detail}".strip(), ask, "open_ended",
            f"Compliance trigger, deadline {deadline or 'unspecified'} — urgency {trigger.get('urgency')}")


def _hook_recall_due(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    cust_name = customer["identity"]["name"] if customer else "there"
    service = (payload.get("service_due") or "your recall visit").replace("_", " ")
    slots = payload.get("available_slots") or []
    offer = _offer_by_keyword(merchant, "cleaning", "checkup", "consult")
    merchant_name = merchant["identity"]["name"]
    price_bit = f" {offer['title']}" if offer else ""
    if slots:
        slot_bits = " ya ".join(s["label"] for s in slots[:2]) if hindi else " or ".join(s["label"] for s in slots[:2])
        slot_sentence = f"2 slots ready hain: {slot_bits}." if hindi else f"2 slots open: {slot_bits}."
        cta_sentence = "Reply 1 for the first, 2 for the second, or tell us a time that works."
    else:
        slot_sentence = ""
        cta_sentence = "Reply YES to book, or tell us a time that works."
    last_visit = (customer or {}).get("relationship", {}).get("last_visit", "")
    emoji = " 🦷" if category.get("slug") == "dentists" else ""
    hook = (
        f"Hi {cust_name}, {merchant_name} here{emoji} It's time for your {service}"
        + (f" (last visit {last_visit})" if last_visit else "") + f". {slot_sentence}"
    ).strip()
    if price_bit:
        hook += f" {price_bit.strip()} + we'll take care of the rest."
    return (hook, cta_sentence, "binary_yes_stop",
            f"Customer-scoped recall_due trigger; anchored on service='{service}', real open slots, merchant's own offer catalog")


def _hook_perf_dip(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    metric = payload.get("metric", "performance")
    delta = payload.get("delta_pct")
    window = payload.get("window", "7d")
    baseline = payload.get("vs_baseline")
    name = _salutation_for_merchant(category, merchant)
    delta_txt = f"down {_pct(delta)}" if isinstance(delta, (int, float)) else "down"
    baseline_txt = f" (usual ~{baseline}/day)" if baseline else ""
    hook = f"{name}, your {metric} are {delta_txt} over the last {window}{baseline_txt}."
    ask = f"{_connector(hindi)} take a look and suggest a fix?"
    return (hook, ask, "open_ended",
            f"Internal perf_dip on '{metric}' ({delta_txt} over {window}) — loss-aversion framing")


def _hook_perf_spike(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    metric = payload.get("metric", "performance")
    delta = payload.get("delta_pct")
    driver = payload.get("likely_driver")
    name = _salutation_for_merchant(category, merchant)
    delta_txt = f"up {_pct(delta)}" if isinstance(delta, (int, float)) else "up"
    driver_txt = f" — likely from {driver.replace('_', ' ')}" if driver else ""
    hook = f"{name}, nice one — your {metric} are {delta_txt} this week{driver_txt}."
    ask = "Want to double down on whatever's working, or should I ask what changed?"
    return (hook, ask, "open_ended",
            f"Internal perf_spike on '{metric}' ({delta_txt}) — reinforce + curious-ask lever")


def _hook_renewal_due(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    days = payload.get("days_remaining", merchant.get("subscription", {}).get("days_remaining"))
    plan = payload.get("plan", merchant.get("subscription", {}).get("plan"))
    amount = payload.get("renewal_amount")
    name = _salutation_for_merchant(category, merchant)
    amt_txt = f" ({_fmt_money(amount)})" if amount else ""
    hook = f"{name}, your {plan} plan renews in {days} days{amt_txt}."
    ask = "Reply YES to renew now, or STOP if you'd rather I not remind you again."
    return (hook, ask, "binary_yes_stop",
            f"renewal_due, {days} days remaining — urgency {trigger.get('urgency')}")


def _hook_milestone_reached(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    metric = payload.get("metric", "a milestone").replace("_", " ")
    now_v = payload.get("value_now")
    target = payload.get("milestone_value")
    name = _salutation_for_merchant(category, merchant)
    hook = f"{name}, you're at {now_v} {metric} — {target - now_v if isinstance(now_v, (int, float)) and isinstance(target, (int, float)) else 'almost'} away from {target}!"
    ask = f"{_connector(hindi)} post about it on your profile once you hit it?"
    return (hook, ask, "open_ended", f"milestone_reached: {metric} {now_v}->{target}")


def _hook_dormant_with_vera(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    days = payload.get("days_since_last_merchant_message")
    last_topic = (payload.get("last_topic") or "").replace("_", " ")
    name = _salutation_for_merchant(category, merchant)
    topic_bit = f" We were last talking about {last_topic}." if last_topic else ""
    hook = f"{name}, haven't heard from you in {days} days.{topic_bit}"
    ask = "Still want help with that, or is there something else on your mind this week?"
    return (hook, ask, "open_ended", f"dormant_with_vera:{days}d, re-opening on last_topic='{last_topic}'")


def _hook_review_theme_emerged(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    theme = (payload.get("theme") or "").replace("_", " ")
    occ = payload.get("occurrences_30d")
    quote = payload.get("common_quote")
    name = _salutation_for_merchant(category, merchant)
    quote_bit = f" One review says: \"{quote}\"." if quote else ""
    hook = f"{name}, {occ} reviews this month mention '{theme}'.{quote_bit}"
    ask = f"{_connector(hindi)} draft a quick fix or a reply template for this?"
    return (hook, ask, "open_ended", f"review_theme_emerged: '{theme}' x{occ} in 30d")


def _hook_competitor_opened(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    comp = payload.get("competitor_name")
    dist = payload.get("distance_km")
    their_offer = payload.get("their_offer")
    name = _salutation_for_merchant(category, merchant)
    hook = f"{name}, {comp} opened {dist}km from you"
    if their_offer:
        hook += f", running \"{their_offer}\"."
    else:
        hook += "."
    ask = f"{_connector(hindi)} draft a counter-offer from your own catalog?"
    return (hook, ask, "open_ended", f"competitor_opened: {comp} at {dist}km")


def _hook_festival_upcoming(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    fest = payload.get("festival", "the festival")
    days = payload.get("days_until")
    name = _salutation_for_merchant(category, merchant)
    hook = f"{name}, {fest} is {days} days away."
    ask = f"{_connector(hindi)} draft a {fest} offer post from your catalog?"
    return (hook, ask, "open_ended", f"festival_upcoming: {fest} in {days}d")


def _hook_winback_eligible(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    days = payload.get("days_since_expiry")
    dip = payload.get("perf_dip_pct")
    lapsed = payload.get("lapsed_customers_added_since_expiry")
    name = _salutation_for_merchant(category, merchant)
    dip_txt = f", views down {_pct(dip)}" if isinstance(dip, (int, float)) else ""
    lapsed_txt = f" {lapsed} more customers have gone quiet since." if lapsed else ""
    hook = f"{name}, it's been {days} days since your plan expired{dip_txt}.{lapsed_txt}"
    ask = "Reply YES to reactivate today, or STOP to not hear about this again."
    return (hook, ask, "binary_yes_stop", f"winback_eligible: expired {days}d ago")


def _hook_ipl_match_today(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    match = payload.get("match")
    t = (payload.get("match_time_iso") or "").split("T")
    time_bit = t[1][:5] if len(t) > 1 else ""
    name = _salutation_for_merchant(category, merchant)
    offer = _offer_by_keyword(merchant, "combo", "match")
    hook = f"{name}, {match} tonight{f' at {time_bit}' if time_bit else ''} — weeknight matches usually push covers up."
    if offer:
        ask = f"Want me to push \"{offer['title']}\" as tonight's GBP post?"
    else:
        ask = f"{_connector(hindi)} draft a quick match-night post?"
    return (hook, ask, "open_ended", f"ipl_match_today: {match}")


def _hook_active_planning_intent(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    topic = (payload.get("intent_topic") or "").replace("_", " ")
    last_msg = payload.get("merchant_last_message", "")
    name = _salutation_for_merchant(category, merchant)
    hook = f"{name}, on the {topic} — picking this up where we left off."
    ask = "Want me to draft the concrete plan (pricing + schedule) right now, or do you want to tweak the idea first?"
    return (hook, ask, "open_ended", f"active_planning_intent: '{topic}', last merchant msg='{last_msg[:60]}'")


def _hook_seasonal_perf_dip(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    metric = payload.get("metric", "performance")
    note = payload.get("season_note", "").replace("_", " ")
    name = _salutation_for_merchant(category, merchant)
    hook = f"{name}, your {metric} dip right now is normal — {note or 'a seasonal pattern'}, not something specific to your listing."
    ask = "Want me to hold off on any changes and just flag when it turns around?"
    return (hook, ask, "open_ended", f"seasonal_perf_dip on {metric} — reassurance framing, no action forced")


def _hook_customer_lapsed(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    days = payload.get("days_since_last_visit")
    focus = (payload.get("previous_focus") or "").replace("_", " ")
    cust_name = customer["identity"]["name"] if customer else "there"
    merchant_name = merchant["identity"]["name"]
    offer = _offer_by_keyword(merchant, focus.split()[0] if focus else "")
    hook = f"Hi {cust_name}, {merchant_name} here — it's been {days} days since your last visit"
    hook += f" (you were working on {focus})." if focus else "."
    if offer:
        ask = f"We've got \"{offer['title']}\" running right now — want to grab a slot?"
    else:
        ask = "Want to pick a slot to get back on track?"
    return (hook, ask, "binary_yes_stop", f"customer lapse ({days}d) winback, previous focus='{focus}'")


def _hook_appointment_tomorrow(category, merchant, trigger, customer, hindi):
    cust_name = customer["identity"]["name"] if customer else "there"
    merchant_name = merchant["identity"]["name"]
    hook = f"Hi {cust_name}, quick reminder from {merchant_name} — your appointment is tomorrow."
    ask = "Reply YES to confirm, or let us know if you need to reschedule."
    return (hook, ask, "binary_yes_stop", "appointment_tomorrow reminder")


def _hook_trial_followup(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    trial_date = payload.get("trial_date")
    options = payload.get("next_session_options") or []
    cust_name = customer["identity"]["name"] if customer else "there"
    merchant_name = merchant["identity"]["name"]
    hook = f"Hi {cust_name}, hope you enjoyed the trial at {merchant_name} on {trial_date}!"
    if options:
        ask = f"Next slot open: {options[0]['label']} — want to lock it in?"
    else:
        ask = "Want to lock in your next session?"
    return (hook, ask, "binary_yes_stop", f"trial_followup after {trial_date}")


def _hook_chronic_refill_due(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    molecules = payload.get("molecule_list") or []
    runs_out = (payload.get("stock_runs_out_iso") or "").split("T")[0]
    delivery = payload.get("delivery_address_saved")
    cust_name = customer["identity"]["name"] if customer else "there"
    merchant_name = merchant["identity"]["name"]
    mol_txt = ", ".join(molecules[:3]) if molecules else "your regular medicines"
    hook = f"Hi {cust_name}, {merchant_name} here — your {mol_txt} stock runs out around {runs_out}."
    if delivery:
        ask = "Reply YES and we'll deliver to your saved address, or STOP to skip this time."
    else:
        ask = "Reply YES to place the refill, or STOP to skip this time."
    return (hook, ask, "binary_yes_stop", f"chronic_refill_due: {mol_txt}, runs out {runs_out}")


def _hook_category_seasonal(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    season = payload.get("season", "this season").replace("_", " ")
    trends = payload.get("trends") or []
    name = _salutation_for_merchant(category, merchant)
    trend_txt = ", ".join(t.replace("_", " ") for t in trends[:3])
    hook = f"{name}, {season} shelf shift is starting: {trend_txt}."
    ask = f"{_connector(hindi)} send a shelf-rearrange checklist?"
    return (hook, ask, "open_ended", f"category_seasonal({season}): {trend_txt}")


def _hook_gbp_unverified(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    uplift = payload.get("estimated_uplift_pct")
    name = _salutation_for_merchant(category, merchant)
    uplift_txt = f" — verified profiles see roughly {_pct(uplift)} more views" if isinstance(uplift, (int, float)) else ""
    hook = f"{name}, your Google profile isn't verified yet{uplift_txt}."
    ask = "Reply YES and I'll walk you through the postcard/phone verification, 5 min setup."
    return (hook, ask, "binary_yes_stop", "gbp_unverified — verification uplift framing")


def _hook_cde_opportunity(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    item = _digest_item(category, payload.get("digest_item_id"))
    name = _salutation_for_merchant(category, merchant)
    if item:
        date = (item.get("date") or "").split("T")[0]
        hook = f"{name}, {item.get('title')} — {date}, {item.get('credits', 0)} credits, {item.get('summary', '')}"
    else:
        hook = f"{name}, there's a CDE opportunity coming up in your area."
    ask = "Want the registration link?"
    return (hook, ask, "open_ended", f"cde_opportunity: {payload.get('digest_item_id')}")


def _hook_wedding_package_followup(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    days = payload.get("days_to_wedding")
    next_step = (payload.get("next_step_window_open") or "").replace("_", " ")
    cust_name = customer["identity"]["name"] if customer else "there"
    merchant_name = merchant["identity"]["name"]
    hook = f"Hi {cust_name}, {days} days to go! Loved having you for the trial at {merchant_name}."
    ask = f"Your {next_step} window is open now — want to book it before the pre-wedding rush?"
    return (hook, ask, "binary_yes_stop", f"wedding_package_followup: {days}d to wedding, next_step='{next_step}'")


def _hook_curious_ask_due(category, merchant, trigger, customer, hindi):
    name = _salutation_for_merchant(category, merchant)
    catalog = category.get("offer_catalog") or []
    example = catalog[0]["title"] if catalog else "a service"
    hook = f"{name}, quick one from me this week —"
    ask = f"what's been your most-asked-for service lately? Curious if it's still {example} or something new."
    return (hook, ask, "open_ended", "curious_ask_due — social-proof/ask-the-merchant compulsion lever")


def _hook_supply_alert(category, merchant, trigger, customer, hindi):
    payload = trigger.get("payload", {}) or {}
    molecule = payload.get("molecule")
    batches = payload.get("affected_batches") or []
    mfr = payload.get("manufacturer")
    name = _salutation_for_merchant(category, merchant)
    batch_txt = ", ".join(batches) if batches else "specific batches"
    hook = f"{name}, voluntary recall on {molecule} — {batch_txt} by {mfr}."
    ask = "Reply YES and I'll pull the customer list on this molecule so you can inform them."
    return (hook, ask, "binary_yes_stop", f"supply_alert: {molecule}, batches={batches}")


HOOK_BUILDERS = {
    "research_digest": _hook_research_digest,
    "regulation_change": _hook_regulation_change,
    "recall_due": _hook_recall_due,
    "perf_dip": _hook_perf_dip,
    "perf_spike": _hook_perf_spike,
    "renewal_due": _hook_renewal_due,
    "milestone_reached": _hook_milestone_reached,
    "dormant_with_vera": _hook_dormant_with_vera,
    "review_theme_emerged": _hook_review_theme_emerged,
    "competitor_opened": _hook_competitor_opened,
    "festival_upcoming": _hook_festival_upcoming,
    "winback_eligible": _hook_winback_eligible,
    "ipl_match_today": _hook_ipl_match_today,
    "active_planning_intent": _hook_active_planning_intent,
    "seasonal_perf_dip": _hook_seasonal_perf_dip,
    "customer_lapsed_soft": _hook_customer_lapsed,
    "customer_lapsed_hard": _hook_customer_lapsed,
    "appointment_tomorrow": _hook_appointment_tomorrow,
    "trial_followup": _hook_trial_followup,
    "chronic_refill_due": _hook_chronic_refill_due,
    "category_seasonal": _hook_category_seasonal,
    "gbp_unverified": _hook_gbp_unverified,
    "cde_opportunity": _hook_cde_opportunity,
    "wedding_package_followup": _hook_wedding_package_followup,
    "curious_ask_due": _hook_curious_ask_due,
    "supply_alert": _hook_supply_alert,
}


def _hook_placeholder(category, merchant, trigger, customer, hindi):
    """The dataset generator emits some auto-expanded triggers with a bare
    {"placeholder": True, ...} payload — no real number/date/headline to
    anchor on. Rather than let a kind-specific builder read a missing field
    and print 'None', ground the message on the one thing that IS always
    real: the merchant's own performance snapshot / identity (and, for
    customer-scoped triggers, the customer's own relationship state)."""
    kind_txt = (trigger.get("kind") or "an update").replace("_", " ")
    perf = merchant.get("performance", {}) or {}
    views, calls = perf.get("views"), perf.get("calls")
    window = perf.get("window_days", 30)
    perf_bit = f"{views} views / {calls} calls in the last {window} days" if views is not None and calls is not None else ""

    if customer is not None:
        cust_name = customer.get("identity", {}).get("name", "there")
        merchant_name = merchant["identity"]["name"]
        rel = customer.get("relationship", {}) or {}
        visits = rel.get("visits_total")
        last_visit = rel.get("last_visit")
        state = customer.get("state", "")
        detail = f" You've visited {visits}x, last on {last_visit}." if visits and last_visit else ""
        hook = f"Hi {cust_name}, {merchant_name} here — checking in re: {kind_txt}.{detail}"
        ask = "Want to hear more, or should I leave it for now?"
        return (hook, ask, "open_ended",
                f"Trigger '{trigger.get('kind')}' had a placeholder payload (no real fact attached); "
                f"grounded on the customer's own relationship state (visits={visits}, state='{state}') "
                f"instead of inventing trigger detail.")

    name = _salutation_for_merchant(category, merchant)
    perf_clause = f" Last 30 days: {perf_bit}." if perf_bit else ""
    hook = f"{name}, quick note related to {kind_txt}.{perf_clause}"
    ask = f"{_connector(hindi)} take a closer look together?"
    return (hook, ask, "open_ended",
            f"Trigger '{trigger.get('kind')}' had a placeholder payload; fell back to the merchant's real "
            f"performance snapshot rather than inventing a trigger-specific number.")


def _hook_generic_fallback(category, merchant, trigger, customer, hindi):
    """Used only for a trigger 'kind' we've never seen (forward-compat with
    judge-injected trigger kinds). Grounds on whatever real signal + payload
    keys are present rather than inventing anything."""
    name = _salutation_for_merchant(category, merchant)
    payload = trigger.get("payload", {}) or {}
    kind_txt = (trigger.get("kind") or "an update").replace("_", " ")
    facts = [f"{k}={v}" for k, v in payload.items() if not isinstance(v, (dict, list))][:2]
    facts_txt = f" ({', '.join(facts)})" if facts else ""
    hook = f"{name}, quick note on {kind_txt}{facts_txt}."
    ask = f"{_connector(hindi)} look into this together?"
    return (hook, ask, "open_ended", f"unrecognized trigger kind '{trigger.get('kind')}'; generic grounded fallback used")


# --------------------------------------------------------------------------
# main entrypoint
# --------------------------------------------------------------------------

def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """Deterministically compose the next Vera message.

    category / merchant / trigger / customer are plain dicts in the shapes
    defined in challenge-brief.md §4 / challenge-testing-brief.md §3.
    """
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    if payload.get("placeholder"):
        builder = _hook_placeholder
    else:
        builder = HOOK_BUILDERS.get(kind, _hook_generic_fallback)

    if customer is not None:
        hindi = _uses_hindi_mix(None, customer.get("identity", {}).get("language_pref"))
    else:
        hindi = _uses_hindi_mix(merchant.get("identity", {}).get("languages"), None)

    hook, ask, cta_type, rationale = builder(category, merchant, trigger, customer, hindi)

    body = f"{hook.strip()} {ask.strip()}".strip()
    body = " ".join(body.split())  # normalize whitespace

    send_as = "merchant_on_behalf" if customer is not None else "vera"
    suppression_key = trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}"

    return {
        "body": body,
        "cta": cta_type,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": rationale,
    }
