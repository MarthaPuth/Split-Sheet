"""The training coach — a Claude-backed chat that reasons from real numbers.

It never edits plan.json. It gives perspective: whether to trust a session as
written, what an ache might mean and what to do about it today, and rough
nutrition guidance. For anything that reads as a real injury it says so plainly
and points to a physio, the same way a good training partner would.

Uses Haiku — cheap and fast, which is what a "how do I feel today" chat needs.
A typical exchange costs a fraction of a cent; the whole season costs cents to
low dollars. See https://docs.claude.com/en/docs/about-claude/pricing for
current rates before relying on this number.
"""

import datetime as dt
import json
import urllib.request

import store

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 500
MAX_TURNS = 16          # kept short: cheap, fast, and a coach doesn't need your life story
HISTORY_KEY = "coach_history"


def _num(v, suffix="", none="not recorded"):
    return f"{v}{suffix}" if v is not None else none


def build_context(data, plan, today):
    """A compact brief a coach could actually read between reps."""
    daily = {r["day"]: r for r in (data or {}).get("daily", [])}
    stats = (data or {}).get("stats", {})

    days = sorted(d for d in daily if d <= today)
    last = daily.get(days[-1]) if days else {}
    recent = [daily[d] for d in days[-7:]]

    p = (plan or {}).get(today)
    if p:
        items = "; ".join(f"{i['tag']}: {i['name']}" + (f" ({i['det']})" if i.get("det") else "")
                          for i in p.get("items", []))
        plan_line = f"Today's plan — {items}. {p.get('mi', 0)} miles scheduled."
    else:
        plan_line = "No session logged in the plan for today."

    week_km = round(sum(r.get("km") or 0 for r in recent), 1)
    acwr = last.get("acwr")

    lines = [
        f"Today is {today}.",
        plan_line,
        f"Last night: {_num(last.get('tst'), ' h asleep')}, "
        f"efficiency {_num(last.get('eff'), '%')}, "
        f"HRV {_num(last.get('hrv'), ' ms')}, "
        f"resting heart rate {_num(last.get('rhr'), ' bpm')}, "
        f"readiness score {_num(last.get('readiness'))}.",
        f"Season averages: sleep {_num(stats.get('sleep_mean'), ' h')}, "
        f"HRV {_num(stats.get('hrv_mean'), ' ms')}, "
        f"resting heart rate {_num(stats.get('rhr_mean'), ' bpm')}, "
        f"sleep efficiency {_num(stats.get('eff_mean'), '%')}.",
        f"Last 7 days: {week_km} km logged. "
        f"Acute:chronic load ratio {_num(acwr)} "
        f"({'above 1.5, elevated injury risk' if isinstance(acwr, (int, float)) and acwr > 1.5 else 'in a reasonable range' if acwr else 'not enough data yet'}).",
        "Race calendar: Terre Haute NCAA DI Cross Country Championships, 21 November 2026. "
        "Regionals 13 November 2026.",
    ]
    return "\n".join(lines)


SYSTEM_TEMPLATE = """You are the training coach built into this NCAA Division I cross country \
runner's personal dashboard. You talk like an experienced, level-headed coach texting \
between sessions — not a chatbot, not a doctor.

What you do:
- Give perspective on today's session: whether to run it as written, ease off, or swap something, \
based on the numbers below. You never claim to have edited the written plan — you only advise; \
the athlete and their actual coach decide.
- When they mention an ache, pain, or niggle: ask what you need to (once, briefly) if it's unclear, \
then give practical guidance — what to do today, what to watch for, simple rehab or gym \
substitutions. If it sounds like more than a niggle (sharp pain, swelling, pain that changes their \
gait or doesn't ease into a warm-up, pain that's getting worse session over session), say plainly \
that this needs a physio or doctor, and still give the safe, conservative thing to do today.
- For nutrition questions, give practical, specific guidance sized to what's actually on the \
schedule today — not a generic diet plan.
- Ground every answer in the numbers you're given. Don't invent data you don't have, and say so \
if something isn't in the numbers below.

Keep it short — a few sentences, not a report. This is a phone screen between reps.

Athlete's current data:
{context}"""


def _post(body, api_key):
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST")
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode())


def history():
    return store.get(HISTORY_KEY, [])


def clear_history():
    store.drop(HISTORY_KEY)


VIEW_HINT = {
    "today": "the Today screen — today's session, readiness and fuelling",
    "sleep": "the Sleep screen — last night's stages, HRV, efficiency and trends",
    "runs": "the Runs screen — weekly volume, load ratio and individual runs",
    "gym": "the Gym screen — strength work, sauna, naps and recovery",
    "cal": "the Calendar — the month and week of training, past and upcoming",
}


def reply(user_message, api_key, data, plan, view=""):
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it in your host's environment settings.")

    today = dt.date.today().isoformat()
    context = build_context(data, plan, today)
    hint = VIEW_HINT.get(view)
    if hint:
        context += f"\n\nThe athlete is currently looking at {hint}, so a question with no other \ncontext is most likely about that."
    system = SYSTEM_TEMPLATE.format(context=context)

    past = history()[-MAX_TURNS:]
    messages = [{"role": m["role"], "content": m["content"]} for m in past]
    messages.append({"role": "user", "content": user_message})

    body = {"model": MODEL, "max_tokens": MAX_TOKENS, "system": system, "messages": messages}
    res = _post(body, api_key)

    if "error" in res:
        raise RuntimeError(res["error"].get("message", "the API rejected the request"))

    text = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")
    if not text:
        raise RuntimeError("empty response from the model")

    updated = past + [{"role": "user", "content": user_message},
                       {"role": "assistant", "content": text}]
    store.put(HISTORY_KEY, updated[-(MAX_TURNS * 2):])
    return text
