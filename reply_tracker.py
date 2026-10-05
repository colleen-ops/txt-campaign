#!/usr/bin/env python3
"""
Getty reply tracker — daily Slack post.

EnTrance replied conversations -> Close lookup by phone -> rep status changes.
Writes data/slack/{main.md, thread.md, meta.json} for the Claude routine to post
(or posts directly if SLACK_BOT_TOKEN is set):
  main message : (1) drop-out funnel by campaign  (2) progressing status distribution
  thread reply : "Who" list of progressing merchants

Modes:
  python reply_tracker.py                  # EnTrance API
  python reply_tracker.py --csv conv.csv   # UI Conversations export (+ optional --stats stats.json)
  --dry-run                                # print Slack payloads, don't post

Env / GitHub Secrets:
  CLOSE_API_KEY, SLACK_BOT_TOKEN, SLACK_CHANNEL
  ENTRANCE_APP_KEY + ENTRANCE_AUTH_CODE  and/or  ENTRANCE_EMAIL + ENTRANCE_PASSWORD
  ENTRANCE_CHANNELS_URL / ENTRANCE_CAMPAIGNS_URL (optional overrides; default apiv2 workspace paths)
  LOOKBACK_DAYS (14), WORKSPACE_ID (2373), DATA_DIR (data)
"""
import argparse, csv, json, os, re, sys, time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

WORKSPACE_ID = os.getenv("WORKSPACE_ID") or "2373"
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS") or "14")
CLOSE_KEY = os.getenv("CLOSE_API_KEY", "").strip()
SLACK_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
# EnTrance auth (from the entrancesms SDK the MCP server uses):
#   1) app key + auth code  -> "Authorization: Basic base64(key:code)"
#   2) email + password     -> POST /authentication/login -> record.access_token (Bearer)
# Tries 1 first, falls back to 2 on 401/403. MCP notes say 1 was broken on this account.
ENTRANCE_BASE = (os.getenv("ENTRANCE_BASE") or "https://apiv2.entrancegrp.com").rstrip("/")
ENTRANCE_APP_KEY = os.getenv("ENTRANCE_APP_KEY", "").strip()
ENTRANCE_AUTH_CODE = os.getenv("ENTRANCE_AUTH_CODE", "").strip()
ENTRANCE_EMAIL = os.getenv("ENTRANCE_EMAIL", "").strip().strip("\"'")
ENTRANCE_PASSWORD = os.getenv("ENTRANCE_PASSWORD", "").strip("\r\n")
ENTRANCE_CHANNELS_URL = (os.getenv("ENTRANCE_CHANNELS_URL")
                         or f"{ENTRANCE_BASE}/workspaces/{WORKSPACE_ID}/channels")
ENTRANCE_CAMPAIGNS_URL = (os.getenv("ENTRANCE_CAMPAIGNS_URL")
                          or f"{ENTRANCE_BASE}/workspaces/{WORKSPACE_ID}/campaigns")
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
_AUTH = None


def _probe(headers):
    r = requests.get(ENTRANCE_CAMPAIGNS_URL, headers=headers, timeout=30,
                     params={"limit": 1, "order": "DESC"})
    return r.status_code


def entrance_headers():
    global _AUTH
    if _AUTH:
        return _AUTH
    tried = []
    if ENTRANCE_APP_KEY and ENTRANCE_AUTH_CODE:
        import base64
        b = base64.b64encode(f"{ENTRANCE_APP_KEY}:{ENTRANCE_AUTH_CODE}".encode()).decode()
        h = {"Authorization": f"Basic {b}", "Content-Type": "application/json"}
        code = _probe(h)
        tried.append(f"app key/auth code -> {code}")
        if code == 200:
            _AUTH = h
            print("EnTrance auth: app key + auth code")
            return _AUTH
    if ENTRANCE_EMAIL and ENTRANCE_PASSWORD:
        r = requests.post(f"{ENTRANCE_BASE}/authentication/login", timeout=30,
                          json={"email": ENTRANCE_EMAIL, "password": ENTRANCE_PASSWORD})
        tok = (r.json().get("record") or {}).get("access_token") if r.ok else None
        tried.append(f"email/password login -> {r.status_code}")
        if tok:
            _AUTH = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
            print("EnTrance auth: email/password login")
            return _AUTH
        if not r.ok:
            tried.append(r.text[:300])
    sys.exit("EnTrance auth failed: " + " | ".join(tried or ["no credentials set"]))


def _records(page):
    """Find the record list whatever the key is (records / data / channels / rows ...)."""
    if isinstance(page, list):
        return page
    for k in ("records", "data", "channels", "rows", "items", "results"):
        v = page.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for vv in v.values():
                if isinstance(vv, list):
                    return vv
    for v in page.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return []


def entrance_pages(url, extra=None, debug=False):
    headers = entrance_headers()
    lastid, cursor, seen = None, None, set()
    while True:
        params = {"limit": PAGE_CAP, "order": "DESC", **(extra or {})}
        if lastid:
            params["lastid"] = lastid
        if cursor:
            params["last_response"] = cursor
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if debug and lastid is None:
            print(f"DEBUG GET {r.url} -> {r.status_code} body[:300]={r.text[:300]!r}")
        if not r.ok:
            return
        page = r.json()
        recs = [x for x in _records(page) if x.get("id") not in seen]
        if not recs:
            return
        seen.update(x.get("id") for x in recs)
        yield recs
        lastid = page.get("lastId") or recs[-1].get("id")
        cursor = page.get("last_response") if isinstance(page, dict) else None


_CAMP_NAMES = {}


def _first(r, *keys):
    for k in keys:
        v = r
        for part in k.split("."):
            v = v.get(part) if isinstance(v, dict) else None
        if v not in (None, ""):
            return v
    return None


def channels_from_api(since):
    out, raw, no_ts, no_phone, pages = [], 0, 0, 0, 0
    seen_ids = set()
    # Recent blast replies mostly sit UNCLAIMED (Claims tab); Messages inbox = claimed. Pull both.
    for claimed in ("false", "true"):
        cpages = craw = 0
        for recs in entrance_pages(ENTRANCE_CHANNELS_URL, {"claimed": claimed}, debug=True):
            pages += 1
            cpages += 1
            if pages == 1:
                sample = {k: (str(v)[:60] if not isinstance(v, (dict, list)) else type(v).__name__)
                          for k, v in recs[0].items()}
                print("DEBUG first channel record:", json.dumps(sample, default=str))
            page_all_old = True
            for r in recs:
                if r.get("id") in seen_ids:
                    continue
                seen_ids.add(r.get("id"))
                raw += 1
                craw += 1
                lr = parse_ts(_first(r, "last_response", "lastResponse", "last_response_at",
                                     "last_inbound_at", "updated_at"))
                if lr is None:
                    no_ts += 1
                    continue
                if lr >= since:
                    page_all_old = False
                if lr < since or r.get("replied") is False:
                    continue
                phone = phone10(_first(r, "number", "phone", "contact.number"))
                if not phone:
                    no_phone += 1
                    continue
                cid = _first(r, "campaign_id", "last_campaign_id", "campaigns.id", "campaign.id")
                camp = (_first(r, "campaigns.name", "campaign_name", "campaign.name")
                        or _CAMP_NAMES.get(cid) or _CAMP_NAMES.get(str(cid)) or "")
                out.append({"campaign": camp, "phone": phone, "last_response": lr})
            if page_all_old or cpages >= 500:   # sorted by last_response DESC
                break
        print(f"DEBUG claimed={claimed}: pages={cpages} raw={craw}")
    named = sum(1 for o in out if o["campaign"])
    print(f"DEBUG channels: pages={pages} raw={raw} kept={len(out)} no_ts={no_ts} "
          f"no_phone={no_phone} with_campaign={named}")
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
    for recs in entrance_pages(ENTRANCE_CAMPAIGNS_URL, {"types": "SINGLE_BLAST"}):
        older = False
        for c in recs:
            _CAMP_NAMES[c.get("id")] = c.get("name", "")
            _CAMP_NAMES[str(c.get("id"))] = c.get("name", "")
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
    H = ["Campaign", "Dlv", "Replied-STOP", "Threads", "Progress"]
    body, tot = [], Counter()
    order = sorted(groups, key=lambda g: -gstats[g]["dlv"] if gstats else 0)
    for g in order:
        s = gstats.get(g, {"dlv": 0, "replied": 0, "stop": 0})
        rr = [r for r in rows if r["group"] == g]
        live = s["replied"] - s["stop"]
        found = sum(r["found"] for r in rr)
        worked = sum(r["rep_changed"] for r in rr)
        prog = sum(r["progressing"] for r in rr)
        body.append([short(g) if g != "GCLV (all)" else "GCLV (all)", f"{s['dlv']:,}", f"{live:,}",
                     len(rr), prog])
        tot.update(dlv=s["dlv"], replied=s["replied"], live=live, threads=len(rr),
                   found=found, worked=worked, prog=prog)
    body.append(["TOTAL", f"{tot['dlv']:,}", f"{tot['live']:,}", tot["threads"], tot["prog"]])
    funnel = table(H, body, "lrrrr")

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
            f"_Replied-STOP = real replies · Threads = reply conversations matched by phone · "
            f"Progress = Sub / HP / follow-up / rewarm in Close after a rep status change._\n\n"
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

    stats = json.load(open(a.stats)) if a.stats else ({} if a.csv else campaign_stats_api(since))
    channels = channels_from_csv(a.csv, since) if a.csv else channels_from_api(since)
    t = requests.get(f"{CLOSE}/me/", auth=(CLOSE_KEY, ""), timeout=30)
    if t.status_code == 401:
        sys.exit("Close auth failed (401): CLOSE_API_KEY secret is wrong/expired — "
                 "Close → Settings → Developer → API Keys, create a key, paste it into the secret.")
    rows, groups, gstats = build(channels, stats, since)
    if stats:
        before = len(rows)
        rows = [r for r in rows if r["campaign"] in stats]   # only campaigns sent in window
        print(f"DEBUG rows: {before} matched-in-Close-step, {len(rows)} after campaign filter")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"{datetime.now(timezone.utc):%Y-%m-%d}.csv"
    if rows:
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    main_msg, thread_msg = render(rows, groups, gstats)

    # Files the Claude routine picks up and posts to Slack (main + thread reply)
    today = f"{datetime.now(timezone.utc):%Y-%m-%d}"
    slack_dir = DATA_DIR / "slack"
    slack_dir.mkdir(parents=True, exist_ok=True)
    (slack_dir / "main.md").write_text(main_msg)
    (slack_dir / "thread.md").write_text(thread_msg)
    (slack_dir / "meta.json").write_text(json.dumps({
        "date": today, "generated_at": datetime.now(timezone.utc).isoformat(),
        "threads": len(rows), "progressing": sum(r["progressing"] for r in rows)}, indent=2))

    if a.dry_run or not SLACK_TOKEN:
        print(main_msg, "\n\n--- thread ---\n", thread_msg)
        return
    ts = slack_post(main_msg)
    slack_post(thread_msg, thread_ts=ts)
    print(f"posted ts={ts}")


if __name__ == "__main__":
    main()
