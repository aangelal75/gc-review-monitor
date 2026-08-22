import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText

import requests

# ── Configuration ──────────────────────────────────────────────────────────────
API_KEY           = os.environ["GOOGLE_PLACES_API_KEY"]
EMAIL_FROM        = os.environ["EMAIL_FROM"]
EMAIL_APP_PASSWORD = os.environ["EMAIL_APP_PASSWORD"]
SEND_ALWAYS       = os.environ.get("SEND_ALWAYS", "true").lower() == "true"
SALON_ID          = os.environ.get("SALON_ID", "")   # passed by workflow per-salon


def load_salons():
    with open("salons.json") as f:
        return json.load(f)


def state_file(salon_id):
    return f"state_{salon_id}.json"


def load_state(salon_id):
    path = state_file(salon_id)
    if not os.path.exists(path):
        return {"total": 0, "sum": 0, "breakdown": {"1":0,"2":0,"3":0,"4":0,"5":0},
                "seen_review_ids": [], "google_rating": 0, "bootstrapped": False}
    with open(path) as f:
        return json.load(f)


def save_state(salon_id, state):
    with open(state_file(salon_id), "w") as f:
        json.dump(state, f, indent=2)


# ── Google API ─────────────────────────────────────────────────────────────────
def fetch_place_details(place_id):
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {"place_id": place_id, "fields": "rating,user_ratings_total,reviews",
              "reviews_sort": "newest", "key": API_KEY}
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "OK":
        raise RuntimeError(f"Places API error: {data.get('status')} — {data.get('error_message','')}")
    return data["result"]


# ── Math ───────────────────────────────────────────────────────────────────────
def stars_needed(total, rating_sum, target):
    target_x10 = round(target * 10)
    numerator   = target_x10 * total - 10 * rating_sum
    denominator = 50 - target_x10
    if numerator <= 0:
        return 0
    return -(-numerator // denominator)


# ── State update ───────────────────────────────────────────────────────────────
def update_breakdown(state, result):
    new_google_total  = result.get("user_ratings_total")
    new_google_rating = result.get("rating")
    reviews           = result.get("reviews", [])

    if not state.get("bootstrapped"):
        total      = state["total"]
        rating_sum = state["sum"]
        breakdown  = state["breakdown"]
        diff = (new_google_total - total) if new_google_total is not None else 0
        note = ""
        if diff != 0 and new_google_rating:
            assumed = max(1, min(5, round(new_google_rating)))
            rating_sum += diff * assumed
            breakdown[str(assumed)] = breakdown.get(str(assumed), 0) + diff
            note  = f"(Baseline reconciled: Google total moved by {diff}; estimated {assumed}★.)"
            total = new_google_total
        new_ids = [f"{r.get('author_name')}|{r.get('time')}" for r in reviews]
        state.update({"total": total, "sum": rating_sum, "breakdown": breakdown,
                      "seen_review_ids": new_ids, "google_rating": new_google_rating,
                      "bootstrapped": True})
        return state, [], note

    seen_ids   = set(state.get("seen_review_ids", []))
    new_reviews = [r for r in reviews if f"{r.get('author_name')}|{r.get('time')}" not in seen_ids]

    total      = state["total"]
    rating_sum = state["sum"]
    breakdown  = state["breakdown"]

    for r in new_reviews:
        try:
            rating = int(round(r.get("rating", 0)))
        except (TypeError, ValueError):
            continue
        if 1 <= rating <= 5:
            breakdown[str(rating)] = breakdown.get(str(rating), 0) + 1
            rating_sum += rating

    accounted = total + len(new_reviews)
    diff      = (new_google_total - accounted) if new_google_total is not None else 0
    note      = ""
    if diff != 0 and new_google_rating:
        assumed = max(1, min(5, round(new_google_rating)))
        rating_sum += diff * assumed
        breakdown[str(assumed)] = breakdown.get(str(assumed), 0) + diff
        note = (f"({diff} review(s) added by Google weren't individually visible; estimated {assumed}★.)"
                if diff > 0 else
                f"({-diff} review(s) removed by Google; assumed {assumed}★ removed.)")

    total   = new_google_total if new_google_total is not None else accounted
    new_ids = [f"{r.get('author_name')}|{r.get('time')}" for r in reviews]
    state.update({"total": total, "sum": rating_sum, "breakdown": breakdown,
                  "seen_review_ids": new_ids, "google_rating": new_google_rating})
    return state, new_reviews, note


# ── Email builders ─────────────────────────────────────────────────────────────
def build_daily_email(salon, baseline, new_state, new_reviews, note):
    name     = salon["name"]
    target   = salon.get("target_rating", 4.9)
    total    = new_state["total"]
    s        = new_state["sum"]
    avg      = round(s / total, 4) if total else 0
    base_avg = round(baseline["sum"] / baseline["total"], 4) if baseline.get("total") else None
    delta    = total - baseline.get("total", total)
    needed   = stars_needed(total, s, target)
    bd       = new_state["breakdown"]

    lines = [f"{name} — Weekly Review Check  ({datetime.now().strftime('%B %-d, %Y')})", ""]

    if delta == 0 and not new_reviews:
        lines += [f"No new reviews this week.",
                  f"Total: {total}  |  Average: {avg}  (Google shows {new_state.get('google_rating')})"]
    else:
        if delta > 0:
            lines.append(f"{delta} new review(s) this week.")
        lines.append(f"Average: {avg}  (was {base_avg})  |  Total: {total}  (was {baseline.get('total')})")
        if new_reviews:
            lines += ["", "New review(s):"]
            for r in new_reviews:
                rt    = int(round(r.get("rating", 0)))
                stars = "*" * rt + "-" * (5 - rt)
                text  = (r.get("text") or "").strip()[:250]
                lines.append(f"  [{stars}]  {r.get('author_name','Customer')}: {text}")
        if note:
            lines += ["", note]

    lines += ["",
              f"Progress to {target}: {'✓ Target reached!' if needed == 0 else f'{needed} more 5★ reviews needed'}",
              f"Breakdown:  1★={bd.get('1',0)}  2★={bd.get('2',0)}  3★={bd.get('3',0)}  4★={bd.get('4',0)}  5★={bd.get('5',0)}"]
    return "\n".join(lines)


def build_alert_email(salon, new_state, new_reviews):
    name   = salon["name"]
    target = salon.get("target_rating", 4.9)
    total  = new_state["total"]
    s      = new_state["sum"]
    avg    = round(s / total, 4) if total else 0
    needed = stars_needed(total, s, target)
    has_low = any(int(round(r.get("rating", 5))) < 3 for r in new_reviews)

    lines = [f"{name} — {'⚠ Low Rating Alert' if has_low else 'New Review'} ({datetime.now().strftime('%B %-d, %Y  %I:%M %p')})", "",
             f"{len(new_reviews)} new review(s) just appeared on Google.",
             f"Current average: {avg}  |  Total: {total}", ""]
    for r in new_reviews:
        rt    = int(round(r.get("rating", 0)))
        stars = "*" * rt + "-" * (5 - rt)
        text  = (r.get("text") or "").strip()[:250]
        lines.append(f"  [{stars}]  {r.get('author_name','Customer')}: {text}")
    lines += ["",
              f"Progress to {target}: {'✓ Target reached!' if needed == 0 else f'{needed} more 5★ reviews needed'}"]
    return "\n".join(lines)


def alert_subject(salon, new_reviews):
    name    = salon["name"]
    has_low = any(int(round(r.get("rating", 5))) < 3 for r in new_reviews)
    if has_low:
        return f"⚠ {name} — Low Rating Alert"
    return f"★ {name} — {len(new_reviews)} New Review{'s' if len(new_reviews)>1 else ''}"


# ── Email send ─────────────────────────────────────────────────────────────────
def send_email(to_list, subject, body):
    msg            = MIMEText(body)
    msg["Subject"] = subject
    msg["From"]    = f"GC Review Monitor <{EMAIL_FROM}>"
    msg["To"]      = ", ".join(to_list)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
        server.sendmail(EMAIL_FROM, to_list, msg.as_string())


# ── Main ───────────────────────────────────────────────────────────────────────
def run_salon(salon):
    sid      = salon["id"]
    to_list  = [e.strip() for e in salon["email_to"].split(",")]
    result   = fetch_place_details(salon["place_id"])

    old_state         = load_state(sid)
    new_state         = json.loads(json.dumps(old_state))
    new_state, new_reviews, note = update_breakdown(new_state, result)

    if SEND_ALWAYS:
        # Compare against last week's snapshot, not whatever the last hourly check saved —
        # so the weekly digest reflects the whole week, not just "since the last peek."
        baseline = old_state.get("week_baseline") or {
            "total": old_state.get("total", 0),
            "sum": old_state.get("sum", 0),
        }
        body    = build_daily_email(salon, baseline, new_state, new_reviews, note)
        subject = f"{salon['name']} — Weekly Review Check"
        send_email(to_list, subject, body)
        new_state["week_baseline"] = {"total": new_state["total"], "sum": new_state["sum"]}
    elif new_reviews:
        body    = build_alert_email(salon, new_state, new_reviews)
        subject = alert_subject(salon, new_reviews)
        send_email(to_list, subject, body)

    save_state(sid, new_state)


def main():
    salons = load_salons()
    target = SALON_ID or None

    for salon in salons:
        if target and salon["id"] != target:
            continue
        print(f"Processing: {salon['name']} ({salon['id']})")
        try:
            run_salon(salon)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
