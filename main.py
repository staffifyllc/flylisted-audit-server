#!/usr/bin/env python3
"""
FlyListed Social Media Audit Generator
Researches a lead's Instagram/web presence and emails a personalized audit.

SETUP:
  pip install anthropic flask

  Set these environment variables (or create a .env file and load it):
    ANTHROPIC_API_KEY   — your Anthropic API key
    SMTP_HOST           — e.g. smtp.gmail.com
    SMTP_PORT           — 587
    SMTP_USER           — your sending email address
    SMTP_PASS           — email password or app password
    FROM_EMAIL          — paul@flylisted.com
    FROM_NAME           — Paul at FlyListed

CLI USAGE (test a single lead):
  python flylisted-audit-generator.py \
    --name "Sarah Johnson" \
    --email "sarah@example.com" \
    --instagram "@sarahjohnsonrealty" \
    --industry "Real Estate"

WEBHOOK SERVER (connect to Mailchimp / your form):
  python flylisted-audit-generator.py --server
  POST to http://localhost:5050/webhook/audit
  Body: { "name": "...", "email": "...", "instagram": "...", "industry": "..." }
"""

import os
import re
import sys
import argparse
import threading
import urllib.request
import json

import anthropic

# ─── Config ───────────────────────────────────────────────────────────────────

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
FROM_EMAIL       = os.environ.get("FROM_EMAIL", "social@flylisted.com")
FROM_NAME        = os.environ.get("FROM_NAME", "Paul at FlyListed")

BOOK_LINK  = "https://meetings.hubspot.com/paul-chareth?uuid=fb531c6b-0387-4837-b09a-5e5d52bc2e67"

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
    """Call Claude with web search tools to research and generate the audit."""
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

    # Agentic loop — server-side tools may pause_turn if they need more iterations
    for _ in range(6):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=AUDIT_SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        # Collect all text blocks
        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"    text block ({len(block.text)} chars): {block.text[:80]!r}")

        if response.stop_reason == "end_turn":
            # Find text block that contains the audit markers
            for block in response.content:
                if block.type == "text" and "SUBJECT:" in block.text:
                    return parse_audit(block.text)
            # No markers found — return empty to trigger error
            break

        if response.stop_reason == "pause_turn":
            continue

        break

    return {}


def parse_audit(text: str) -> dict:
    """Extract structured fields from Claude's marked-up output."""
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


# ─── Email HTML ───────────────────────────────────────────────────────────────

def build_html(audit: dict) -> str:
    """Build FlyListed-branded HTML email from parsed audit data."""

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

  <!-- Header -->
  <tr><td style="background:#000000;padding:28px 40px;text-align:center;">
    <div style="color:#ffffff;font-size:20px;font-weight:700;letter-spacing:3px;">FLYLISTED</div>
    <div style="color:#666;font-size:11px;letter-spacing:3px;margin-top:4px;text-transform:uppercase;">Social Media Audit</div>
  </td></tr>

  <!-- Gradient bar -->
  <tr><td style="height:4px;background:linear-gradient(135deg,#833ab4,#fd1d1d,#fcb045);font-size:0;">&nbsp;</td></tr>

  <!-- Body -->
  <tr><td style="padding:40px;">

    <p style="font-size:15px;color:#222;margin:0 0 8px 0;font-weight:600;">{greeting}</p>
    <p style="font-size:15px;color:#444;line-height:1.7;margin:0 0 36px 0;">{intro}</p>

    <!-- Scores -->
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#000;border-radius:10px;padding:0;margin-bottom:36px;">
    <tr><td style="padding:28px;">
      <div style="color:#777;font-size:10px;letter-spacing:2.5px;text-transform:uppercase;margin-bottom:18px;">First-Look Scores</div>
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding:8px 0;color:#bbb;font-size:14px;">Brand Clarity</td>
          <td align="right">{s_brand}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#bbb;font-size:14px;">Content Consistency</td>
          <td align="right">{s_consist}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#bbb;font-size:14px;">Trust &amp; Authority</td>
          <td align="right">{s_trust}</td>
        </tr>
      </table>
    </td></tr>
    </table>

    <!-- What's Working -->
    <div style="margin-bottom:28px;">
      {section_header("What&#39;s Working", "#833ab4")}
      <ul style="margin:0;padding-left:20px;">{working}</ul>
    </div>

    <!-- What May Be Holding Things Back -->
    <div style="margin-bottom:28px;">
      {section_header("What May Be Holding Things Back", "#fd1d1d")}
      <ul style="margin:0;padding-left:20px;">{holding}</ul>
    </div>

    <!-- What We'd Improve First -->
    <div style="margin-bottom:36px;">
      {section_header("What We&#39;d Improve First", "#fcb045")}
      <ul style="margin:0;padding-left:20px;">{improve}</ul>
    </div>

    <!-- Closing -->
    <p style="font-size:15px;color:#444;line-height:1.7;margin:0 0 32px 0;">{closing}</p>

    <!-- CTA -->
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:36px;">
    <tr><td align="center">
      <a href="{BOOK_LINK}"
         style="display:inline-block;background:linear-gradient(135deg,#833ab4,#fd1d1d,#fcb045);color:#fff;text-decoration:none;font-weight:700;font-size:15px;padding:14px 36px;border-radius:8px;letter-spacing:0.5px;">
        Book a Strategy Call
      </a>
    </td></tr>
    </table>

    <!-- Signature -->
    <p style="font-size:14px;color:#555;line-height:1.6;margin:0;">
      Paul Chareth<br>
      <span style="color:#999;">FlyListed &mdash; Social Media Built for Growth</span><br>
      <a href="https://content.flylisted.com" style="color:#833ab4;text-decoration:none;font-size:13px;">content.flylisted.com</a>
    </p>

  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f9f9f9;border-top:1px solid #eee;padding:20px 40px;text-align:center;">
    <p style="font-size:12px;color:#aaa;margin:0;">FlyListed &bull; You requested a free Instagram audit from our website.</p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


# ─── Email Sending ────────────────────────────────────────────────────────────

def send_email(to_email: str, to_name: str, subject: str, html: str, plain: str = "") -> bool:
    """Send email via SendGrid API."""
    if not SENDGRID_API_KEY:
        print("\n" + "="*60)
        print("SendGrid not configured — printing audit instead of sending.")
        print(f"To: {to_name} <{to_email}>")
        print(f"Subject: {subject}")
        print("="*60)
        print(plain or "[see HTML output]")
        return True

    payload = {
        "personalizations": [{"to": [{"email": to_email, "name": to_name}]}],
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
    """Research lead, generate audit, build email, send."""
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
    html = build_html(audit)

    subject = audit.get("subject") or f"Your Free Instagram Audit &mdash; Here's What We Found"
    plain   = audit.get("raw", "")

    return send_email(email, name, subject, html, plain)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def cli():
    parser = argparse.ArgumentParser(description="FlyListed Social Media Audit Generator")
    parser.add_argument("--name",      default="",  help="Lead's first name")
    parser.add_argument("--email",     default="",  help="Lead's email address")
    parser.add_argument("--instagram", default="",  help="Instagram handle (with or without @)")
    parser.add_argument("--business",  default="",     help="Business name (optional)")
    parser.add_argument("--website",   default="",     help="Website URL (optional)")
    parser.add_argument("--industry",  default="",     help="Industry / business type (optional)")
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
    """Run a simple Flask webhook server for form integration."""
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        print("Flask not installed. Run: pip install flask")
        sys.exit(1)

    app = Flask(__name__)

    @app.route("/webhook/audit", methods=["GET", "POST"])
    def receive_lead():
        if request.method == "GET":
            return jsonify({"status": "ok"}), 200
        # Accept JSON or form-encoded data
        data = request.get_json(silent=True) or request.form.to_dict()

        # Normalize common field name variations (Mailchimp, Typeform, etc.)
        lead = {
            "name":          (data.get("name") or data.get("FNAME") or data.get("first_name") or ""),
            "email":         (data.get("email") or data.get("EMAIL") or ""),
            "instagram":     (data.get("instagram") or data.get("INSTAGRAM") or data.get("MERGE2") or ""),
            "business_name": (data.get("business_name") or data.get("business") or data.get("MERGE3") or ""),
            "website":       (data.get("website") or data.get("WEBSITE") or data.get("MERGE4") or ""),
            "industry":      (data.get("industry") or data.get("INDUSTRY") or data.get("MERGE5") or ""),
        }

        if not lead["email"]:
            return jsonify({"error": "email is required"}), 400
        if not lead["instagram"]:
            return jsonify({"error": "instagram handle is required"}), 400

        # Fire-and-forget in background thread so webhook returns immediately
        thread = threading.Thread(target=process_lead, args=(lead,), daemon=True)
        thread.start()

        return jsonify({"status": "processing", "message": "Audit generation started"}), 200

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "service": "flylisted-audit-generator"}), 200

    print(f"\nFlyListed Audit Generator running on port {port}")
    print(f"POST to: http://localhost:{port}/webhook/audit")
    print(f"Health:  http://localhost:{port}/health\n")
    print("Required fields: email, instagram")
    print("Optional fields: name, business_name, website, industry\n")
    app.run(host="0.0.0.0", port=port, debug=False)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
