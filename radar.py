#!/usr/bin/env python3
"""Stealth Founder Radar.

Finds people who recently started a "stealth" company as founder and who just
left one of the companies you track (for example top AI labs or fintechs).
Data layer: Crustdata POST /person/search (https://docs.crustdata.com).

Pipeline: search -> keep the real stealth founder role -> check the last job
they left -> drop interns / advisors -> score -> digest (+ optional Slack, Claude).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

from crustdata_api import Crustdata, day, load_env, normalize_domain
from notify import ask_claude, claude_available, send_slack

FOUNDER = re.compile(r"\b(co-?founder|founder|founding)\b", re.I)
STEALTH = re.compile(r"\bstealth\b", re.I)
NOT_A_REAL_JOB = re.compile(r"\b(intern|internship|advisor|adviser|board|contributor|volunteer|fellow|student|investor|angel)\b", re.I)
SENIOR = re.compile(r"\b(staff|principal|lead|head|director|vp|vice president|chief|cto|ceo|manager|senior|member of technical staff)\b", re.I)

DEFAULT_COMPANIES = "openai.com,anthropic.com,deepmind.google,stripe.com,databricks.com,ramp.com,figma.com,scale.com"

FIELDS = [
    "crustdata_person_id",
    "basic_profile.name",
    "basic_profile.headline",
    "basic_profile.location",
    "social_handles.professional_network_identifier",
    "experience.employment_details",
]


def build_filters(domains: List[str], since: str, country: Optional[str] = None) -> Dict[str, Any]:
    stealth = {"op": "or", "conditions": [
        {"field": "experience.employment_details.current.company_name", "type": "[.]", "value": "Stealth"},
        {"field": "experience.employment_details.current.title", "type": "[.]", "value": "Stealth"},
    ]}
    conds: List[Dict[str, Any]] = [
        {"field": "experience.employment_details.current.title", "type": "(.)", "value": "Founder"},
        stealth,
        {"field": "experience.employment_details.current.start_date", "type": "=>", "value": since},
        {"field": "experience.employment_details.past.company_website_domain", "type": "in", "value": domains},
    ]
    if country:
        conds.append({"field": "basic_profile.location.country", "type": "=", "value": country})
    return {"op": "and", "conditions": conds}


def role_domain(role: Dict[str, Any]) -> str:
    return normalize_domain(role.get("company_website") or "")


def stealth_role(profile: Dict[str, Any], since: str) -> Optional[Dict[str, Any]]:
    current = ((profile.get("experience") or {}).get("employment_details") or {}).get("current") or []
    hits = [r for r in current
            if FOUNDER.search(r.get("title") or "")
            and (STEALTH.search(r.get("name") or "") or STEALTH.search(r.get("title") or ""))
            and day(r.get("start_date")) >= since]
    return max(hits, key=lambda r: r.get("start_date") or "") if hits else None


def last_job(profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Most recently ended past role that was a real job."""
    past = ((profile.get("experience") or {}).get("employment_details") or {}).get("past") or []
    ended = [r for r in past if r.get("end_date")]
    return max(ended, key=lambda r: (r.get("end_date") or "", r.get("start_date") or "")) if ended else None


def qualify(profile: Dict[str, Any], domains: List[str], since: str, left_after: str) -> Optional[Dict[str, Any]]:
    """Return a founder record, or None with the reason kept out of the way."""
    s = stealth_role(profile, since)
    if not s:
        return None
    last = last_job(profile)
    watched = {normalize_domain(d) for d in domains}
    if not last or role_domain(last) not in watched:
        return None  # their most recent job was somewhere else
    if day(last.get("end_date")) < left_after:
        return None  # left the tracked company too long ago
    if NOT_A_REAL_JOB.search(last.get("title") or ""):
        return None
    emp = (profile.get("experience") or {}).get("employment_details") or {}
    years = sum(float(r.get("years_at_company_raw") or 0) for r in emp.get("past") or []
                if role_domain(r) == role_domain(last))
    repeat = any(FOUNDER.search(r.get("title") or "") for r in emp.get("past") or [])
    rec = {
        "person_id": profile.get("crustdata_person_id"),
        "name": (profile.get("basic_profile") or {}).get("name", ""),
        "profile_url": (((profile.get("social_handles") or {}).get("professional_network_identifier")) or {}).get("profile_url", ""),
        "country": ((profile.get("basic_profile") or {}).get("location") or {}).get("country", ""),
        "stealth_title": s.get("title", ""),
        "stealth_since": day(s.get("start_date")),
        "last_company": last.get("name", ""),
        "last_domain": role_domain(last),
        "last_title": last.get("title", ""),
        "left": day(last.get("end_date")),
        "years_at_last_company": round(years, 1),
        "repeat_founder": repeat,
    }
    rec["score"] = score(rec)
    return rec


def score(rec: Dict[str, Any]) -> int:
    """Simple, explainable 0-10 score. Tune the weights for your fund."""
    pts = 3
    if SENIOR.search(rec["last_title"]):
        pts += 2
    pts += min(3, int(rec["years_at_last_company"] // 1.5))
    if rec["repeat_founder"]:
        pts += 2
    return min(10, pts)


def run(client: Crustdata, domains: List[str], since: str, left_after: str,
        country: Optional[str], max_results: int) -> Dict[str, Any]:
    seen, founders, scanned = set(), [], 0
    for profile in client.person_search(build_filters(domains, since, country), FIELDS, max_results=max_results):
        scanned += 1
        rec = qualify(profile, domains, since, left_after)
        if rec and rec["person_id"] not in seen:
            seen.add(rec["person_id"])
            founders.append(rec)
    founders.sort(key=lambda r: (-r["score"], r["stealth_since"]), reverse=False)
    return {"scanned": scanned, "founders": founders}


def summary(result: Dict[str, Any]) -> Dict[str, Any]:
    f = result["founders"]
    return {
        "profiles_scanned": result["scanned"],
        "stealth_founders": len(f),
        "by_last_company": dict(Counter(r["last_company"] for r in f).most_common()),
        "by_country": dict(Counter(r["country"] or "unknown" for r in f).most_common()),
        "repeat_founders": sum(1 for r in f if r["repeat_founder"]),
    }


def slack_text(founders: List[Dict[str, Any]], since: str, limit: int = 10) -> str:
    lines = [f":telescope: Stealth Founder Radar: {len(founders)} new stealth founders since {since}"]
    for r in founders[:limit]:
        lines.append(f"• *{r['name']}* ({r['score']}/10) ex-{r['last_company']} {r['last_title']}, "
                     f"left {r['left'][:7]}, stealth since {r['stealth_since'][:7]} <{r['profile_url']}|profile>")
        if r.get("note"):
            lines.append(f"   _{r['note']}_")
    if len(founders) > limit:
        lines.append(f"…and {len(founders) - limit} more in out/founders.csv")
    return "\n".join(lines)


def write_outputs(result: Dict[str, Any], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "founders.json"), "w") as fh:
        json.dump({"summary": summary(result), "founders": result["founders"]}, fh, indent=1)
    cols = ["score", "name", "profile_url", "country", "last_company", "last_title", "left",
            "years_at_last_company", "stealth_title", "stealth_since", "repeat_founder", "note"]
    with open(os.path.join(out_dir, "founders.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(result["founders"])


def only_new(founders: List[Dict[str, Any]], state_path: str) -> List[Dict[str, Any]]:
    seen = set()
    if os.path.exists(state_path):
        with open(state_path) as fh:
            seen = set(json.load(fh))
    return [f for f in founders if str(f["person_id"]) not in seen]


def save_state(founders: List[Dict[str, Any]], state_path: str) -> None:
    seen = set()
    if os.path.exists(state_path):
        with open(state_path) as fh:
            seen = set(json.load(fh))
    seen.update(str(f["person_id"]) for f in founders)
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump(sorted(seen), fh, indent=1)


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    p = argparse.ArgumentParser(description="Stealth Founder Radar (Crustdata data layer)")
    p.add_argument("--companies", default=os.environ.get("TRACKED_COMPANIES", DEFAULT_COMPANIES),
                   help="comma list of company domains whose alumni you track")
    p.add_argument("--since-days", type=int, default=int(os.environ.get("SINCE_DAYS", "90")),
                   help="stealth role started in the last N days")
    p.add_argument("--left-within-days", type=int, default=int(os.environ.get("LEFT_WITHIN_DAYS", "270")),
                   help="left the tracked company in the last N days")
    p.add_argument("--country", default=os.environ.get("COUNTRY") or None, help='e.g. "United States"')
    p.add_argument("--max-results", type=int, default=200)
    p.add_argument("--notes", action="store_true", help="add a one-line diligence note per founder with Claude")
    p.add_argument("--dry-run", action="store_true", help="never send to Slack")
    p.add_argument("--all", action="store_true", help="include founders already alerted on")
    p.add_argument("--out", default="out")
    p.add_argument("--state", default=".state/seen.json")
    args = p.parse_args(argv)

    today = dt.date.today()
    since = (today - dt.timedelta(days=args.since_days)).isoformat()
    left_after = (today - dt.timedelta(days=args.left_within_days)).isoformat()
    domains = [normalize_domain(d) for d in args.companies.split(",") if d.strip()]

    result = run(Crustdata(), domains, since, left_after, args.country, args.max_results)
    if not args.all:
        result["founders"] = only_new(result["founders"], args.state)
    if args.notes:
        if not claude_available():
            print("Claude not configured (set ANTHROPIC_API_KEY and pip install anthropic): skipping notes", file=sys.stderr)
        else:
            for r in result["founders"]:
                r["note"] = ask_claude(
                    "In one sentence (max 25 words), say what a VC should ask this stealth founder first. "
                    f"Last role: {r['last_title']} at {r['last_company']} for {r['years_at_last_company']} years; "
                    f"now {r['stealth_title']}. No hype. Return only the sentence.") or ""
    write_outputs(result, args.out)
    print(json.dumps(summary(result), indent=1))
    if result["founders"]:
        print(send_slack(slack_text(result["founders"], since), dry_run=args.dry_run))
        if not args.dry_run:
            save_state(result["founders"], args.state)
    else:
        print("No new stealth founders.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
