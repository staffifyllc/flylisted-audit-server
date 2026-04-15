#!/usr/bin/env python3
"""
FlyListed Social Media Audit Generator
- Sends personalized audit on form submission
- Follows up every 7 days until lead unsubscribes or books a call
"""

import os
import re
import sys
import argparse
import threading
import urllib.request
import urllib.parse
import json
import time
import sqlite3
from datetime import datetime, timedelta

import anthropic

# ─── Config ───────────────────────────────────────────────────────────────────

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
FROM_EMAIL       = os.environ.get("FROM_EMAIL", "social@flylisted.com")
FROM_NAME        = os.environ.get("FROM_NAME", "Paul at FlyListed")
SERVER_URL       = os.environ.get("SERVER_URL", "https://web-production-7aaedd.up.railway.app")

BOOK_LINK  = "https://meetings.hubspot.com/paul-chareth?uuid=fb531c6b-0387-4837-b09a-5e5d52bc2e67"
FOLLOWUP_INTERVAL_DAYS = 7

DB_PATH = os.environ.get("DB_PATH", "/data/leads.db")

# ─── Database ─────────────────────────────────────────────────────────────────

def get_db():
    db_path = DB_PATH
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        db_path = os.path.join(os.path.dirname(__file__), "leads.db")

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            email            TEXT UNIQUE NOT NULL,
            name             TEXT,
            instagram        TEXT,
            audit_sent_at    TEXT,
            followup_count   INTEGER DEFAULT 0,
            next_followup_at TEXT,
            unsubscribed     INTEGER DEFAULT 0,
            created_at       TEXT DEFAULT (datetime('now'))
        )
    """)
    # Migrate old schema if needed
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN followup_count INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN next_followup_at TEXT")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN unsubscribed INTEGER DEFAULT 0")
    except Exception:
        pass
    conn.commit()
    return conn


def save_lead(lead: dict):
    """Record that we sent an audit; schedule first follow-up in 7 days."""
    conn = get_db()
    next_followup = (datetime.utcnow() + timedelta(days=FOLLOWUP_INTERVAL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute("""
            INSERT INTO leads (email, name, instagram, audit_sent_at, followup_count, next_followup_at)
            VALUES (?, ?, ?, datetime('now'), 0, ?)
            ON CONFLICT(email) DO UPDATE SET
                audit_sent_at    = datetime('now'),
                followup_count   = 0,
                next_followup_at = ?,
                unsubscribed     = 0
        """, (lead.get("email", ""), lead.get("name", ""), lead.get("instagram", ""), next_followup, next_followup))
        conn.commit()
    finally:
        conn.close()


def get_followups_due() -> list:
    """Return leads whose next follow-up is due and haven't unsubscribed."""
    conn = get_db()
    try:
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute("""
            SELECT email, name, instagram, followup_count FROM leads
            WHERE unsubscribed = 0
              AND next_followup_at IS NOT NULL
              AND next_followup_at <= ?
        """, (now,)).fetchall()
        return [{"email": r[0], "name": r[1], "instagram": r[2], "followup_count": r[3]} for r in rows]
    finally:
        conn.close()


def mark_followup_sent(email: str):
    """Increment followup count and schedule next one in 7 days."""
    conn = get_db()
    next_followup = (datetime.utcnow() + timedelta(days=FOLLOWUP_INTERVAL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute("""
            UPDATE leads
            SET followup_count   = followup_count + 1,
                next_followup_at = ?
            WHERE email = ?
        """, (next_followup, email))
        conn.commit()
    finally:
        conn.close()


def unsubscribe_lead(email: str):
    """Mark a lead as unsubscribed so no more follow-ups are sent."""
    conn = get_db()
    try:
        conn.execute("UPDATE leads SET unsubscribed = 1 WHERE email = ?", (email,))
        conn.commit()
    finally:
        conn.close()


# ─── System Prompt ────────────────────────────────────────────────────────────

AUDIT_SYSTEM_PROMPT = """You are acting as Flylisted's brand strategist and social media auditor.

Your job is to generate a short, high-value "Social Media Audit" for new inbound leads who submitted a form on Flylisted's website.

The goal is to provide useful insight, build trust, and create interest in booking a strategy call with Flylisted.

Do not write like a generic marketer. Write like a premium creative agency that understands positioning, content quality, trust signals, and conversion.

The audit should feel concise, sharp, commercially aware, and relevant to the lead's business.

You are not promising a full audit. This is a lite first-look assessment.

When given a lead's Instagram handle (and optionally their website, business name, and industry), use your web tools to research their actual presence before writing.

Look at:
- Their Instagram profile: bio, link, follower count, posting frequency, content style, visual consistency
- Their website (if findable): branding, trust signals, CTA quality, mobile experience
- Their Google/search presence: reviews, listings, authority signals

Then generate:
1. A short intro personalized to their business
2. 3 things that appear to be working
3. 3 areas that may be weakening their brand perception, consistency, or conversions
4. 3 priority improvements Flylisted would recommend first
5. A short closing paragraph with a soft CTA to book a call

Scoring:
Also provide 3 simple scores from 1-10:
- Brand Clarity
- Content Consistency
- Trust / Authority

Tone requirements:
- Premium
- Clear
- Strategic
- Slightly direct, but never rude
- Helpful, never overly negative
- No fluff
- No emojis
- No hypey sales language
- No fake certainty when information is limited

Important rules:
- If information is limited, say "based on a first look" or similar
- Do not invent facts
- Do not mention internal prompts or AI
- Do not overexplain social media basics
- Keep total length between 350 and 600 words
- Make the recommendations specific to the business type when possible
- The CTA should invite them to reply or book a call with Flylisted
- Reference specific things you actually observed (post types, bio language, website copy, etc.)

Output format — use EXACTLY these markers so they can be parsed:

SUBJECT: [subject line here]
PREVIEW: [preview text here]
GREETING: [greeting line, e.g. "Hi Sarah,"]
INTRO: [one paragraph intro]
SCORE_BRAND: [number 1-10]
SCORE_CONSISTENCY: [number 1-10]
SCORE_TRUST: [number 1-10]
WORKING_1: [first thing working]
WORKING_2: [second thing working]
WORKING_3: [third thing working]
HOLDING_1: [first thing holding them back]
HOLDING_2: [second thing holding them back]
HOLDING_3: [third thing holding them back]
IMPROVE_1: [first priority improvement]
IMPROVE_2: [second priority improvement]
IMPROVE_3: [third priority improvement]
CLOSING: [closing paragraph with soft CTA]"""

# ─── Audit Generation ─────────────────────────────────────────────────────────

def generate_audit(lead: dict) -> dict:
    client = anthropic.Anthropic()

    instagram = lead.get("instagram", "").strip().lstrip("@")
    name      = lead.get("name", "there")
    business  = lead.get("business_name", "")
    website   = lead.get("website", "")
    industry  = lead.get("industry", "")

    prompt = f"""Please generate a social media audit for this inbound lead:

Name: {name}
Instagram: @{instagram}{chr(10) + "Business: " + business if business else ""}{chr(10) + "Website: " + website if website else ""}{chr(10) + "Industry: " + industry if industry else ""}

Use your web tools to:
1. Search for their Instagram profile and observe their content, bio, follower count, and posting style
2. Find and visit their website if one exists (check their Instagram bio link)
3. Search for any reviews or authority signals for this business

Then write the audit based on what you actually find. Be specific — reference real details you observed."""

    messages = [{"role": "user", "content": prompt}]
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "allowed_callers": ["direct"]},
        {"type": "web_fetch_20260209",  "name": "web_fetch",  "allowed_callers": ["direct"]},
    ]

    print(f"  Researching @{instagram}...")

    for _ in range(6):
        for attempt in range(5):
            try:
                response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4096,
                    system=AUDIT_SYSTEM_PROMPT,
                    tools=tools,
                    messages=messages,
                )
                break
            except Exception as e:
                if "overloaded" in str(e).lower() or "529" in str(e) or "429" in str(e):
                    wait = 30 * (attempt + 1)
                    print(f"  API busy, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    raise
        else:
            print("  API still unavailable after retries")
            return {}

        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"    text block ({len(block.text)} chars): {block.text[:80]!r}")

        if response.stop_reason == "end_turn":
            for block in response.content:
                if block.type == "text" and "SUBJECT:" in block.text:
                    return parse_audit(block.text)
            break

        if response.stop_reason == "pause_turn":
            continue

        break

    return {}


def parse_audit(text: str) -> dict:
    def extract(marker):
        pattern = rf"^{marker}:\s*(.+)$"
        m = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    return {
        "subject":             extract("SUBJECT"),
        "preview":             extract("PREVIEW"),
        "greeting":            extract("GREETING"),
        "intro":               extract("INTRO"),
        "score_brand":         extract("SCORE_BRAND"),
        "score_consistency":   extract("SCORE_CONSISTENCY"),
        "score_trust":         extract("SCORE_TRUST"),
        "working":            [extract("WORKING_1"), extract("WORKING_2"), extract("WORKING_3")],
        "holding":            [extract("HOLDING_1"), extract("HOLDING_2"), extract("HOLDING_3")],
        "improve":            [extract("IMPROVE_1"), extract("IMPROVE_2"), extract("IMPROVE_3")],
        "closing":             extract("CLOSING"),
        "raw":                 text,
    }


# ─── Email Helpers ────────────────────────────────────────────────────────────

def unsubscribe_link(email: str) -> str:
    encoded = urllib.parse.quote(email)
    return f"{SERVER_URL}/unsubscribe?email={encoded}"


def footer_html(email: str) -> str:
    unsub = unsubscribe_link(email)
    return f"""
  <tr><td style="background:#f9f9f9;border-top:1px solid #eee;padding:20px 40px;text-align:center;">
    <p style="font-size:12px;color:#aaa;margin:0 0 8px 0;">FlyListed &bull; You requested a free Instagram audit from our website.</p>
    <p style="font-size:11px;color:#ccc;margin:0;">
      <a href="{unsub}" style="color:#ccc;text-decoration:underline;">Unsubscribe</a>
    </p>
  </td></tr>"""


# ─── Audit Email HTML ─────────────────────────────────────────────────────────

def build_html(audit: dict, to_email: str = "") -> str:

    def score_badge(value, colors):
        return (
            f'<span style="background:linear-gradient(135deg,{colors});color:#fff;'
            f'padding:4px 14px;border-radius:20px;font-size:13px;font-weight:700;">'
            f'{value}/10</span>'
        )

    def list_items(items):
        return "".join(
            f'<li style="margin-bottom:10px;color:#333;font-size:15px;line-height:1.6;">{item}</li>'
            for item in items if item
        )

    def section_header(title, color):
        return (
            f'<div style="font-size:11px;font-weight:700;letter-spacing:2px;'
            f'text-transform:uppercase;color:#000;border-left:3px solid {color};'
            f'padding-left:12px;margin-bottom:14px;">{title}</div>'
        )

    greeting   = audit.get("greeting", "Hi there,")
    intro      = audit.get("intro", "")
    closing    = audit.get("closing", "")
    working    = list_items(audit.get("working", []))
    holding    = list_items(audit.get("holding", []))
    improve    = list_items(audit.get("improve", []))

    s_brand   = score_badge(audit.get("score_brand", "-"),   "#833ab4, #fd1d1d")
    s_consist = score_badge(audit.get("score_consistency","-"), "#fd1d1d, #fcb045")
    s_trust   = score_badge(audit.get("score_trust", "-"),   "#833ab4, #fcb045")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Your FlyListed Social Media Audit</title>
</head>
<body style="margin:0;padding:0;background:#f0f0f0;font-family:'Inter',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f0f0;padding:24px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:4px;overflow:hidden;max-width:600px;width:100%;">
  <tr><td style="background:#000000;padding:28px 40px;text-align:center;">
    <div style="color:#ffffff;font-size:20px;font-weight:700;letter-spacing:3px;">FLYLISTED</div>
    <div style="color:#666;font-size:11px;letter-spacing:3px;margin-top:4px;text-transform:uppercase;">Social Media Audit</div>
  </td></tr>
  <tr><td style="height:4px;background:linear-gradient(135deg,#833ab4,#fd1d1d,#fcb045);font-size:0;">&nbsp;</td></tr>
  <tr><td style="padding:40px;">
    <p style="font-size:15px;color:#222;margin:0 0 8px 0;font-weight:600;">{greeting}</p>
    <p style="font-size:15px;color:#444;line-height:1.7;margin:0 0 36px 0;">{intro}</p>
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#000;border-radius:10px;padding:0;margin-bottom:36px;">
    <tr><td style="padding:28px;">
      <div style="color:#777;font-size:10px;letter-spacing:2.5px;text-transform:uppercase;margin-bottom:18px;">First-Look Scores</div>
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr><td style="padding:8px 0;color:#bbb;font-size:14px;">Brand Clarity</td><td align="right">{s_brand}</td></tr>
        <tr><td style="padding:8px 0;color:#bbb;font-size:14px;">Content Consistency</td><td align="right">{s_consist}</td></tr>
        <tr><td style="padding:8px 0;color:#bbb;font-size:14px;">Trust &amp; Authority</td><td align="right">{s_trust}</td></tr>
      </table>
    </td></tr>
    </table>
    <div style="margin-bottom:28px;">
      {section_header("What&#39;s Working", "#833ab4")}
      <ul style="margin:0;padding-left:20px;">{working}</ul>
    </div>
    <div style="margin-bottom:28px;">
      {section_header("What May Be Holding Things Back", "#fd1d1d")}
      <ul style="margin:0;padding-left:20px;">{holding}</ul>
    </div>
    <div style="margin-bottom:36px;">
      {section_header("What We&#39;d Improve First", "#fcb045")}
      <ul style="margin:0;padding-left:20px;">{improve}</ul>
    </div>
    <p style="font-size:15px;color:#444;line-height:1.7;margin:0 0 32px 0;">{closing}</p>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:36px;">
    <tr><td align="center">
      <a href="{BOOK_LINK}" style="display:inline-block;background:linear-gradient(135deg,#833ab4,#fd1d1d,#fcb045);color:#fff;text-decoration:none;font-weight:700;font-size:15px;padding:14px 36px;border-radius:8px;letter-spacing:0.5px;">
        Book a Strategy Call
      </a>
    </td></tr>
    </table>
    <p style="font-size:14px;color:#555;line-height:1.6;margin:0;">
      Paul Chareth<br>
      <span style="color:#999;">FlyListed &mdash; Social Media Built for Growth</span><br>
      <a href="https://content.flylisted.com" style="color:#833ab4;text-decoration:none;font-size:13px;">content.flylisted.com</a>
    </p>
  </td></tr>
  {footer_html(to_email)}
</table>
</td></tr>
</table>
</body>
</html>"""


# ─── Follow-up Email HTML ─────────────────────────────────────────────────────

# Different messages for each week so they don't feel repetitive
FOLLOWUP_VARIANTS = [
    {
        "subject": "Hey {first} — did you get a chance to look at your audit?",
        "body": (
            "I sent over your social media audit last week and wanted to check in — "
            "did you get a chance to look it over?\n\n"
            "If anything resonated or you have questions about the recommendations, "
            "I'd love to walk you through what we'd actually do for your brand. "
            "No pressure — just a quick call to see if there's a fit."
        ),
        "cta": "Book a Strategy Call",
    },
    {
        "subject": "Still thinking about it, {first}?",
        "body": (
            "Just following up on the audit I sent a couple weeks back. "
            "A few of the things we flagged — especially around content consistency and trust signals — "
            "are pretty quick wins once you have the right system in place.\n\n"
            "If you've been on the fence, sometimes it just takes a 20-minute call to get clarity "
            "on what's actually worth your time and what isn't."
        ),
        "cta": "Grab a Time to Talk",
    },
    {
        "subject": "One thing I'd fix first on your profile, {first}",
        "body": (
            "I've been thinking about your profile since I sent the audit. "
            "If I had to pick one thing to fix first, it would be tightening up the bio and link strategy — "
            "that's usually the highest-leverage change for converting profile visitors into leads.\n\n"
            "Happy to walk you through exactly how we'd approach it on a quick call."
        ),
        "cta": "Book a 20-Min Call",
    },
    {
        "subject": "Last thing I'll say, {first}",
        "body": (
            "I don't want to keep showing up in your inbox if the timing isn't right — "
            "so this is the last nudge for a while.\n\n"
            "If you ever want to revisit working together, the link below is always open. "
            "And if you'd rather I stop reaching out, just click unsubscribe below — no hard feelings at all."
        ),
        "cta": "Book a Call Whenever You're Ready",
    },
]


def get_followup_variant(followup_count: int) -> dict:
    """Cycle through variants, repeating the last one after we run out."""
    idx = min(followup_count, len(FOLLOWUP_VARIANTS) - 1)
    return FOLLOWUP_VARIANTS[idx]


def build_followup_html(name: str, followup_count: int, to_email: str) -> tuple:
    """Returns (subject, html, plain) for a follow-up email."""
    first   = name.split()[0] if name else "there"
    variant = get_followup_variant(followup_count)

    subject = variant["subject"].format(first=first)
    body    = variant["body"]
    cta     = variant["cta"]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background:#f0f0f0;font-family:'Inter',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f0f0;padding:24px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:4px;overflow:hidden;max-width:600px;width:100%;">
  <tr><td style="background:#000000;padding:28px 40px;text-align:center;">
    <div style="color:#ffffff;font-size:20px;font-weight:700;letter-spacing:3px;">FLYLISTED</div>
  </td></tr>
  <tr><td style="height:4px;background:linear-gradient(135deg,#833ab4,#fd1d1d,#fcb045);font-size:0;">&nbsp;</td></tr>
  <tr><td style="padding:40px;">
    <p style="font-size:15px;color:#222;margin:0 0 20px 0;font-weight:600;">Hi {first},</p>
    {''.join(f'<p style="font-size:15px;color:#444;line-height:1.7;margin:0 0 20px 0;">{para}</p>' for para in body.split(chr(10)+chr(10)))}
    <table width="100%" cellpadding="0" cellspacing="0" style="margin:32px 0;">
    <tr><td align="center">
      <a href="{BOOK_LINK}" style="display:inline-block;background:linear-gradient(135deg,#833ab4,#fd1d1d,#fcb045);color:#fff;text-decoration:none;font-weight:700;font-size:15px;padding:14px 36px;border-radius:8px;letter-spacing:0.5px;">
        {cta}
      </a>
    </td></tr>
    </table>
    <p style="font-size:14px;color:#555;line-height:1.6;margin:0;">
      Paul Chareth<br>
      <span style="color:#999;">FlyListed &mdash; Social Media Built for Growth</span><br>
      <a href="https://content.flylisted.com" style="color:#833ab4;text-decoration:none;font-size:13px;">content.flylisted.com</a>
    </p>
  </td></tr>
  {footer_html(to_email)}
</table>
</td></tr>
</table>
</body>
</html>"""

    plain = f"Hi {first},\n\n{body}\n\n{cta}: {BOOK_LINK}\n\nPaul Chareth\nFlyListed\n\nUnsubscribe: {unsubscribe_link(to_email)}"

    return subject, html, plain


# ─── Follow-up Scheduler ──────────────────────────────────────────────────────

def send_followup(lead: dict):
    email          = lead.get("email", "")
    name           = lead.get("name", "")
    followup_count = lead.get("followup_count", 0)

    subject, html, plain = build_followup_html(name, followup_count, email)
    print(f"  Sending follow-up #{followup_count + 1} to {email}...")

    if send_email(email, name, subject, html, plain):
        mark_followup_sent(email)
        print(f"  Follow-up #{followup_count + 1} sent to {email}")


def run_followup_scheduler():
    """Background thread: check every hour for follow-ups due."""
    print("  Follow-up scheduler running (checks every hour)")
    while True:
        try:
            due = get_followups_due()
            if due:
                print(f"  {len(due)} follow-up(s) due")
                for lead in due:
                    send_followup(lead)
        except Exception as e:
            print(f"  Scheduler error: {e}")
        time.sleep(3600)


# ─── Email Sending ────────────────────────────────────────────────────────────

def send_email(to_email: str, to_name: str, subject: str, html: str, plain: str = "") -> bool:
    if not SENDGRID_API_KEY:
        print(f"\nTo: {to_name} <{to_email}>\nSubject: {subject}\n{plain}")
        return True

    payload = {
        "personalizations": [{
            "to": [{"email": to_email, "name": to_name}],
            "cc": [{"email": "info@flylisted.com", "name": "FlyListed"}],
        }],
        "from": {"email": FROM_EMAIL, "name": FROM_NAME},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": plain or "Please view this email in an HTML-capable client."},
            {"type": "text/html",  "value": html},
        ],
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data=data,
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type":  "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            print(f"  Sent to {to_email} (status {resp.status})")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  SendGrid error {e.code}: {body}")
        return False
    except Exception as e:
        print(f"  Email error: {e}")
        return False


# ─── Full Pipeline ────────────────────────────────────────────────────────────

def process_lead(lead: dict) -> bool:
    name  = lead.get("name", "there")
    email = lead.get("email", "").strip()

    if not email:
        print("Error: email is required")
        return False

    instagram = lead.get("instagram", "").strip().lstrip("@")
    if not instagram:
        print("Error: instagram handle is required")
        return False

    print(f"\nProcessing lead: {name} (@{instagram})")

    audit = generate_audit(lead)

    if not audit or not audit.get("intro"):
        print("  Error: audit generation failed or returned empty content")
        if audit.get("raw"):
            print("\n  Raw output:\n", audit["raw"])
        return False

    print("  Building email...")
    html    = build_html(audit, to_email=email)
    subject = audit.get("subject") or "Your Free Instagram Audit — Here's What We Found"
    plain   = audit.get("raw", "")

    success = send_email(email, name, subject, html, plain)
    if success:
        save_lead(lead)
    return success


# ─── CLI ──────────────────────────────────────────────────────────────────────

def cli():
    parser = argparse.ArgumentParser(description="FlyListed Social Media Audit Generator")
    parser.add_argument("--name",      default="",  help="Lead's first name")
    parser.add_argument("--email",     default="",  help="Lead's email address")
    parser.add_argument("--instagram", default="",  help="Instagram handle (with or without @)")
    parser.add_argument("--business",  default="",  help="Business name (optional)")
    parser.add_argument("--website",   default="",  help="Website URL (optional)")
    parser.add_argument("--industry",  default="",  help="Industry / business type (optional)")
    parser.add_argument("--server",    action="store_true", help="Run as webhook server instead")
    parser.add_argument("--port",      type=int, default=5050, help="Webhook server port (default 5050)")

    args = parser.parse_args()

    if args.server:
        run_server(args.port)
        return

    lead = {
        "name":          args.name,
        "email":         args.email,
        "instagram":     args.instagram,
        "business_name": args.business,
        "website":       args.website,
        "industry":      args.industry,
    }

    success = process_lead(lead)
    sys.exit(0 if success else 1)


# ─── Webhook Server ───────────────────────────────────────────────────────────

def run_server(port: int = 5050):
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        print("Flask not installed. Run: pip install flask")
        sys.exit(1)

    app = Flask(__name__)

    # Start follow-up scheduler
    threading.Thread(target=run_followup_scheduler, daemon=True).start()

    @app.route("/webhook/audit", methods=["GET", "POST"])
    def receive_lead():
        if request.method == "GET":
            return jsonify({"status": "ok"}), 200
        data = request.get_json(silent=True) or request.form.to_dict()
        print(f"  Webhook payload keys: {list(data.keys())}")
        print(f"  Webhook payload: {data}")

        def mc(key):
            return data.get(f"data[{key}]") or data.get(f"data[merges][{key}]") or ""

        lead = {
            "name":          (mc("FNAME") or mc("first_name") or data.get("name") or data.get("FNAME") or ""),
            "email":         (mc("EMAIL") or mc("email") or data.get("email") or data.get("EMAIL") or ""),
            "instagram":     (mc("INSTAGRAM") or mc("MERGE2") or data.get("instagram") or data.get("INSTAGRAM") or data.get("MERGE2") or ""),
            "business_name": (mc("MERGE3") or data.get("business_name") or ""),
            "website":       (mc("MERGE4") or data.get("website") or ""),
            "industry":      (mc("MERGE5") or data.get("industry") or ""),
        }

        if not lead["email"]:
            return jsonify({"error": "email is required"}), 400

        threading.Thread(target=process_lead, args=(lead,), daemon=True).start()
        return jsonify({"status": "processing", "message": "Audit generation started"}), 200

    @app.route("/unsubscribe", methods=["GET"])
    def unsubscribe():
        email = request.args.get("email", "").strip()
        if email:
            unsubscribe_lead(email)
            print(f"  Unsubscribed: {email}")
        return """<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;text-align:center;padding:60px;">
<h2>You've been unsubscribed.</h2>
<p style="color:#666;">You won't receive any more emails from FlyListed.<br>If this was a mistake, reply to any of our emails and we'll add you back.</p>
</body></html>"""

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "service": "flylisted-audit-generator"}), 200

    print(f"\nFlyListed Audit Generator running on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
