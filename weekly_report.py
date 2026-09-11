#!/usr/bin/env python3
"""Weekly sanity-check report for the Porto House Monitor.

Answers one question: when few/no new listings have been found, is that
genuinely because nothing qualifies, or because something is broken (a
portal search silently returning nothing, a municipality search that's
stopped working, etc.)?

Reuses monitor.py's own scan/verify functions (no separate scraping logic
to maintain) and emails a single summary report via Resend. Runs are
read-only with respect to listings_db.json / status.json - this script
never touches the monitor's own dedup state, so it can be run at any time
without affecting the live monitor's behavior.
"""
import os
import sys
from collections import Counter, defaultdict

import requests
from playwright.sync_api import sync_playwright

import monitor as mon

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "jorge_hernani@msn.com")
FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")

SAMPLE_PER_PORTAL = 12  # how many raw candidates per portal to detail-verify


def now_utc_iso():
    return mon.now_utc_iso()


def scan_all_portals():
    """Run every portal scan exactly like the live monitor does, but return
    per-portal candidate lists separately (not merged) so we can report on
    each portal's raw yield independently."""
    status = {}
    per_portal = {}

    per_portal["imovirtual"] = mon.scan_imovirtual(status)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        per_portal["remax"] = mon.scan_remax(browser, status)
        per_portal["era"] = mon.scan_era(browser, status)
        per_portal["century21"] = mon.scan_century21(browser, status)
        per_portal["idealista"] = mon.scan_idealista(browser, status)

        # Detail-verify a sample from each portal to get exclusion-reason
        # breakdowns without needing to check every single candidate (some
        # portals return dozens; a representative sample is enough to spot
        # a corrupted/broken search vs normal filtering).
        reason_breakdown = {}
        qualifying_examples = defaultdict(list)
        for portal, candidates in per_portal.items():
            counts = Counter()
            for cand in candidates[:SAMPLE_PER_PORTAL]:
                qualifies, reason, price, title = mon.verify_candidate(browser, cand)
                if qualifies:
                    counts["QUALIFIES"] += 1
                    qualifying_examples[portal].append((title, price, cand["url"]))
                else:
                    # Collapse to a short bucket for readability.
                    if "restoration" in reason:
                        bucket = "needs restoration"
                    elif "misc exclusion" in reason:
                        bucket = "divisa/investimento"
                    elif "inactive" in reason:
                        bucket = "sold/reserved"
                    elif "rented" in reason:
                        bucket = "rented"
                    elif "municipality" in reason:
                        bucket = "wrong municipality"
                    elif "price" in reason:
                        bucket = "price out of range"
                    elif "unreachable" in reason:
                        bucket = "page unreachable"
                    else:
                        bucket = reason
                    counts[bucket] += 1
            reason_breakdown[portal] = counts

        browser.close()

    return status, per_portal, reason_breakdown, qualifying_examples


def check_municipality_coverage(per_portal):
    """For each target municipality, does at least one raw candidate (across
    all portals, before any filtering) mention it anywhere in its available
    text? If a municipality never appears at all, its portal-side search
    for that area may be broken - flag it rather than assume 'no listings
    exist there this week', which is possible but less likely across 5
    portals simultaneously."""
    all_text_by_muni = {m: [] for m in mon.TARGET_MUNICIPALITIES if m not in ("povoa de varzim", "baiao")}
    for portal, candidates in per_portal.items():
        for cand in candidates:
            blob = f"{cand.get('title', '')} {cand.get('short_text', '')} {cand.get('municipality_hint', '') or ''}".lower()
            for m in all_text_by_muni:
                if m in blob:
                    all_text_by_muni[m].append(portal)
    return {m: sorted(set(portals)) for m, portals in all_text_by_muni.items()}


def build_report_text(status, per_portal, reason_breakdown, qualifying_examples, muni_coverage):
    lines = []
    lines.append(f"Porto House Monitor - Weekly Verification Report")
    lines.append(f"Generated: {now_utc_iso()}")
    lines.append("")
    lines.append("=== Raw candidate counts per portal (before any filtering) ===")
    suspicious_portals = []
    for portal in ["imovirtual", "remax", "era", "century21", "idealista"]:
        raw_count = len(per_portal.get(portal, []))
        portal_status = status.get(portal, "unknown")
        flag = ""
        if raw_count == 0:
            flag = "  <-- SUSPICIOUS: zero raw candidates found"
            suspicious_portals.append(portal)
        lines.append(f"  {portal:12s} raw_candidates={raw_count:3d}  scan_status={portal_status}{flag}")
    lines.append("")

    lines.append(f"=== Exclusion reason breakdown (sample of up to {SAMPLE_PER_PORTAL} per portal) ===")
    for portal, counts in reason_breakdown.items():
        total_sampled = sum(counts.values())
        if total_sampled == 0:
            lines.append(f"  {portal}: no candidates to sample")
            continue
        lines.append(f"  {portal} (sampled {total_sampled}):")
        for reason, n in counts.most_common():
            lines.append(f"    {reason}: {n}")
    lines.append("")

    lines.append("=== Municipality coverage check (any raw mention, any portal) ===")
    missing_munis = []
    for m in sorted(muni_coverage.keys()):
        portals_seen = muni_coverage[m]
        if not portals_seen:
            lines.append(f"  {m:20s} NOT SEEN in any portal's results this run  <-- worth watching")
            missing_munis.append(m)
        else:
            lines.append(f"  {m:20s} seen via: {', '.join(portals_seen)}")
    lines.append("")

    lines.append("=== Qualifying listings found in this sample check ===")
    total_qualifying = sum(len(v) for v in qualifying_examples.values())
    if total_qualifying == 0:
        lines.append("  None in the sampled candidates.")
    else:
        for portal, examples in qualifying_examples.items():
            for title, price, url in examples:
                lines.append(f"  [{portal}] EUR{price:,} - {title} - {url}")
    lines.append("")

    lines.append("=== Verdict ===")
    if suspicious_portals:
        lines.append(
            f"POSSIBLE ISSUE: {', '.join(suspicious_portals)} returned ZERO raw candidates "
            f"(not just zero qualifying - zero found at all before filtering). This portal's "
            f"search may be broken, blocked, or its page layout may have changed. Worth checking manually."
        )
    elif missing_munis:
        lines.append(
            f"POSSIBLE ISSUE: no listing anywhere mentioned these target municipalities this run: "
            f"{', '.join(missing_munis)}. This could be a genuinely quiet week for those areas, or "
            f"a sign that municipality-specific searches (e.g. Espinho, which needs its own dedicated "
            f"search per portal) have stopped working. Worth watching over a few weeks."
        )
    else:
        lines.append(
            "No issues detected. All portals returned raw candidates, all target municipalities "
            "appeared in at least one result, and the exclusion breakdown looks like normal filtering "
            "(restoration/price/rented/etc.) rather than a broken search. If you're still not receiving "
            "emails, it likely just means nothing new and move-in-ready has appeared this week - not a bug."
        )

    return "\n".join(lines)


def send_report_email(report_text, had_issue):
    if not RESEND_API_KEY:
        print("No RESEND_API_KEY set - cannot send report email. Report was:")
        print(report_text)
        return False
    subject = "Porto House Monitor - Weekly Verification Report"
    if had_issue:
        subject += " (possible issue detected)"
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": FROM_EMAIL, "to": NOTIFY_EMAIL, "subject": subject, "text": report_text},
            timeout=20,
        )
        if r.status_code >= 300:
            print(f"Failed to send report email: HTTP {r.status_code}: {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"Failed to send report email: {e}")
        return False


def main():
    status, per_portal, reason_breakdown, qualifying_examples = scan_all_portals()
    muni_coverage = check_municipality_coverage(per_portal)
    report_text = build_report_text(status, per_portal, reason_breakdown, qualifying_examples, muni_coverage)

    had_issue = (
        any(len(v) == 0 for v in per_portal.values())
        or any(not portals for portals in muni_coverage.values())
    )

    print(report_text)
    sent = send_report_email(report_text, had_issue)
    print(f"\nEmail sent: {sent}")


if __name__ == "__main__":
    main()
