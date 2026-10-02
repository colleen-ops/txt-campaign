#!/usr/bin/env python3
"""
Getty reply tracker — daily Slack post.

EnTrance replied conversations -> Close lookup by phone -> rep status changes.
Posts to Slack:
  main message : (1) drop-out funnel by campaign  (2) progressing status distribution
  thread reply : "Who" list of progressing merchants

Modes:
  python reply_tracker.py                  # EnTrance API
  python reply_tracker.py --csv conv.csv   # UI Conversations export (+ optional --stats stats.json)
  --dry-run                                # print Slack payloads, don't post

Env / GitHub Secrets:
  CLOSE_API_KEY, ENTRANCE_API_KEY, SLACK_BOT_TOKEN, SLACK_CHANNEL
  ENTRANCE_CHANNELS_URL, ENTRANCE_CAMPAIGNS_URL   (verify vs docs.entrancegrp.com/#task-channels)
  LOOKBACK_DAYS (14), WORKSPACE_ID (2373), DATA_DIR (data)
"""
import argparse, csv, json, os, re, sys, time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

WORKSPACE_ID = os.getenv("WORKSPACE_ID", "2373")
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "14"))
ENTRANCE_KEY = os.getenv("ENTRANCE_API_KEY", "")
CLOSE_KEY = os.getenv("CLOSE_API_KEY", "")
SLACK_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
ENTRANCE_CHANNELS_URL = os.getenv(
    "ENTRANCE_CHANNELS_URL", f"https://entrancegrp.com/api/workspaces/{WORKSPACE_ID}/channels")
ENTRANCE_CAMPAIGNS_URL = os.getenv("ENTRANCE_CAMPAIGNS_URL", "")
PAGE_CAP = 20  # EnTrance hard cap; page with lastid
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
CLOSE = "https://api.close.com/api/v1"

# progressing statuses, in display order
PROGRESSING = ["Submission", "High Priority", "3 Day Follow up", "14 Day Follow up",
               "40 Day Follow up", "Rewarm / Nurture"]
ALSO_PROGRESSING = ("Funded", "Merchant Pass", "Need to Reconnect")
NOT_REP_WORK = ("New", "Outbound (3rd Party System)")   # list reloads / bulk flips


def is_progressing(s):
    return bool(s) and (s in PROGRESSING or s in ALSO_PROGRESSING or "Follow up" in s)


def is_dead(s):
    return bool(s) and ("HARD NO" in s or "SOFT NO" in s)


def group_name(campaign):
    """Campaign row label. GCLV lists roll up into one row (tiny volumes)."""
    c = (campaign or "").strip()
    return "GCLV (all)" if c.upper().startswith("GCLV") else c


def phone10(x):
    d = re.sub(r"\D", "", str(x or ""))
    return d[-10:] if len(d) >= 10 else ""


def parse_ts(v):
    if not v:
        return None
    s = re.sub(r" GMT.*$", "", str(v))
    for fmt in ("%a %b %d %Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            d = datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def get(url, **kw):
    for attempt in range(6):
        r = requests.get(url, timeout=30, **kw)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("retry-after", 2 ** attempt)))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"rate-limited: {url}")


# ------------------------------------------------------------------ EnTrance
def entrance_pages(url, extra=None):
    headers = {"Authorization": f"Bearer {ENTRANCE_KEY}"}
    lastid = None
    while True:
        params = {"limit": PAGE_CAP, "order": "DESC", **(extra or {})}
        if lastid:
            params["lastid"] = lastid
        page = get(url, headers=headers, params=params)
        recs = page.get("records") or page.get("data") or []
        if not recs:
            return
        yield recs
        nxt = page.get("lastId") or recs[-1].get("id")
        if nxt == lastid:
            return
        lastid = nxt


def channels_from_api(since):
    out = []
    for recs in entrance_pages(ENTRANCE_CHANNELS_URL):
        older = False
        for r in recs:  # VERIFY field names vs docs
            lr = parse_ts(r.get("last_response") or r.get("lastResponse"))
            if lr is None:
                continue
            if lr < since:
                older = True
                continue
            out.append({"campaign": r.get("campaign_name") or (r.get("campaign") or {}).get("name", ""),
                        "phone": phone10(r.get("number") or (r.get("contact") or {}).get("number")),
                        "last_response": lr})
        if older:
            break
    return out


def channels_from_csv(path, since):
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            lr = parse_ts(r.get("Last Response"))
            if lr and lr >= since:
                out.append({"campaign": r.get("Campaign", ""), "phone": phone10(r.get("Number")),
                            "last_response": lr})
    return out


def campaign_stats_api(since):
    """{campaign_name: {dlv, replied, stop}} for campaigns sent in window. VERIFY field names."""
    stats = {}
    if not ENTRANCE_CAMPAIGNS_URL:
        return stats
    for recs in entrance_pages(ENTRANCE_CAMPAIGNS_URL, {"workspaceId": WORKSPACE_ID}):
        older = False
        for c in recs:
            sent = parse_ts(c.get("sent_at") or c.get("sentAt"))
            if not sent:
                continue
            if sent < since:
                older = True
                continue
            stats[c["name"]] = {"dlv": int(c.get("delivered") or 0),
                                "replied": int(c.get("response") or 0),
                                "stop": int(c.get("stop") or 0)}
        if older:
            break
    return stats


# ------------------------------------------------------------------ Close
def close_find_leads(p10):
    """All Close leads with this exact phone on a contact."""
    auth, leads = (CLOSE_KEY, ""), {}
    for q in (f'phone:"+1{p10}"', p10):
        data = get(f"{CLOSE}/lead/", auth=auth, params={
            "query": q, "_fields": "id,display_name,status_label,contacts", "_limit": 10})
        for L in data.get("data", []):
            phones = {phone10(p.get("phone")) for c in L.get("contacts", []) for p in c.get("phones", [])}
            if p10 in phones:
                leads[L["id"]] = L
        if leads:
            break
    return list(leads.values())


def close_rep_changed(lead_id, since):
    """True if a status change since `since` landed on something other than New/Outbound."""
    data = get(f"{CLOSE}/activity/status_change/lead/", auth=(CLOSE_KEY, ""),
               params={"lead_id": lead_id, "date_created__gte": since.isoformat()})
    return any(a.get("new_status_label") not in NOT_REP_WORK for a in data.get("data", []))


# ------------------------------------------------------------------ build
def build(channels, stats, since):
    # one row per phone, newest reply wins
    by_phone = {}
    for ch in sorted(channels, key=lambda r: r["last_response"]):
        if ch["phone"]:
            by_phone[ch["phone"]] = ch
    rows = []
    for p, ch in by_phone.items():
        g = group_name(ch["campaign"])
        leads = close_find_leads(p)
        rank = lambda L: (0 if is_progressing(L["status_label"]) else 1 if is_dead(L["status_label"]) else 2)
        best = min(leads, key=rank) if leads else None
        status = best["status_label"] if best else ""
        rep = bool(best) and (is_progressing(status) or is_dead(status)) and close_rep_changed(best["id"], since)
        rows.append({"group": g, "campaign": ch["campaign"], "phone": p,
                     "lead_id": best["id"] if best else "", "merchant": best["display_name"] if best else "",
                     "status": status, "found": bool(best), "rep_changed": rep,
                     "progressing": rep and is_progressing(status)})
    # merchant-level dedupe: same lead from two phones counts once
    seen, uniq = set(), []
    for r in rows:
        key = r["lead_id"] or r["phone"]
        if key not in seen:
            seen.add(key)
            uniq.append(r)

    # campaign stats roll up into groups; keep only groups that sent in window
    gstats = defaultdict(lambda: {"dlv": 0, "replied": 0, "stop": 0})
    for name, s in stats.items():
        for k in s:
            gstats[group_name(name)][k] += s[k]
    groups = list(gstats) if stats else sorted({r["group"] for r in uniq})
    return uniq, groups, gstats


def short(g):
    return re.sub(r"\s*\(.*?\)", "", g).replace(" Serbia", "").strip() or g


def pct(a, b):
    return f"{a / b:.0%}" if b else "–"


def table(headers, rows, align):
    w = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    fmt = lambda r: "  ".join(str(c).ljust(w[i]) if align[i] == "l" else str(c).rjust(w[i])
                              for i, c in enumerate(r))
    return "\n".join([fmt(headers), "  ".join("-" * x for x in w)] + [fmt(r) for r in rows])


def render(rows, groups, gstats):
    day = datetime.now(timezone.utc).strftime("%a %b %d")
    # ---- funnel
    H = ["Campaign", "Dlv", "Replied", "%dlv", "Live", "Threads", "InClose", "Worked", "%found", "Prog"]
    body, tot = [], Counter()
    order = sorted(groups, key=lambda g: -gstats[g]["dlv"] if gstats else 0)
    for g in order:
        s = gstats.get(g, {"dlv": 0, "replied": 0, "stop": 0})
        rr = [r for r in rows if r["group"] == g]
        live = s["replied"] - s["stop"]
        found = sum(r["found"] for r in rr)
        worked = sum(r["rep_changed"] for r in rr)
        prog = sum(r["progressing"] for r in rr)
        body.append([short(g) if g != "GCLV (all)" else "GCLV (all)", f"{s['dlv']:,}", f"{s['replied']:,}", pct(s["replied"], s["dlv"]), live,
                     len(rr), found, worked, pct(worked, found), prog])
        tot.update(dlv=s["dlv"], replied=s["replied"], live=live, threads=len(rr),
                   found=found, worked=worked, prog=prog)
    body.append(["TOTAL", f"{tot['dlv']:,}", f"{tot['replied']:,}", pct(tot["replied"], tot["dlv"]),
                 tot["live"], tot["threads"], tot["found"], tot["worked"],
                 pct(tot["worked"], tot["found"]), tot["prog"]])
    funnel = table(H, body, "lrrrrrrrrr")

    # ---- progressing distribution
    prog_rows = [r for r in rows if r["progressing"]]
    cols = [g for g in order if any(r["group"] == g for r in prog_rows)]
    statuses = PROGRESSING + sorted({r["status"] for r in prog_rows} - set(PROGRESSING))
    D = []
    for st in statuses:
        cnt = [sum(1 for r in prog_rows if r["group"] == g and r["status"] == st) for g in cols]
        if sum(cnt):
            D.append([st] + [c or "–" for c in cnt] + [sum(cnt)])
    D.append(["TOTAL"] + [sum(1 for r in prog_rows if r["group"] == g) for g in cols] + [len(prog_rows)])
    dist = table(["Status"] + [short(c) if c != "GCLV (all)" else "GCLV" for c in cols] + ["Total"], D, "l" + "r" * (len(cols) + 1)) if prog_rows else "_none_"

    main = (f"*EnTrance → Close drop-out funnel* · {day} · campaigns sent last {LOOKBACK_DAYS}d\n"
            f"```{funnel}```\n"
            f"_Worked = rep moved status (Sub / HP / follow-up / rewarm / HARD NO); excludes Outbound reloads & New flips._\n\n"
            f"*Progressing leads — status in Close*\n```{dist}```\n"
            f"_Merchant list in thread_ 👇")

    # ---- who (thread)
    rank = {s: i for i, s in enumerate(statuses)}
    who = sorted(prog_rows, key=lambda r: (rank.get(r["status"], 99), r["group"]))
    lines = [f"*Who — progressing ({len(who)})*"]
    lines += [f"• *{r['status']}* — <https://app.close.com/lead/{r['lead_id']}/|{r['merchant']}> · {r['group']}"
              for r in who]
    return main, "\n".join(lines)


def slack_post(text, thread_ts=None):
    r = requests.post("https://slack.com/api/chat.postMessage", timeout=30,
                      headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
                      json={"channel": SLACK_CHANNEL, "text": text, "thread_ts": thread_ts,
                            "unfurl_links": False, "mrkdwn": True})
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"Slack error: {j.get('error')}")
    return j["ts"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="EnTrance Conversations export")
    ap.add_argument("--stats", help="JSON {campaign: {dlv, replied, stop}} (CSV mode)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not CLOSE_KEY:
        sys.exit("CLOSE_API_KEY missing")
    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    channels = channels_from_csv(a.csv, since) if a.csv else channels_from_api(since)
    stats = json.load(open(a.stats)) if a.stats else ({} if a.csv else campaign_stats_api(since))
    rows, groups, gstats = build(channels, stats, since)
    if stats:
        rows = [r for r in rows if r["campaign"] in stats]   # only campaigns sent in window

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"{datetime.now(timezone.utc):%Y-%m-%d}.csv"
    if rows:
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    main_msg, thread_msg = render(rows, groups, gstats)
    if a.dry_run or not SLACK_TOKEN:
        print(main_msg, "\n\n--- thread ---\n", thread_msg)
        return
    ts = slack_post(main_msg)
    slack_post(thread_msg, thread_ts=ts)
    print(f"posted ts={ts}")


if __name__ == "__main__":
    main()
