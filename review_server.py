#!/usr/bin/env python3
"""
review_server.py — the review screen.

A small local web page for reception: the queue of referrals waiting to be
checked, the fields pulled off each one, and a button that creates the
patient, case and document in Nookal. The payer is not created — that stays
in the Nookal UI, where the session cap lives.

Fields-only by design. The source PDF is one click away, and once created
it also sits in the patient's Documents in Nookal.

Standard library only: no framework to install on the clinic PC and nothing
to keep patched.

    python3 review_server.py --queue queue --inbox inbox
    # then open http://127.0.0.1:8765

Binds to 127.0.0.1 — reachable from the machine it runs on, not from the
network. Referral data never leaves the PC.
"""

from __future__ import annotations

import argparse
import html
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nookal_client import NookalClient, NookalConfig, NookalError
from referral_pipeline import (BLOCKED, DEFAULT_SESSIONS, DONE, NEEDS_PAYER,
                               NEEDS_REVIEW, Queue, create_in_nookal)

EDITABLE = [
    ("patient_name", "Patient name", "text"),
    ("dob", "Date of birth", "date"),
    ("medicare_no", "Medicare number", "text"),
    ("medicare_irn", "IRN (from the card)", "text"),
    ("referral_date", "Referral date", "date"),
    ("services_count", "Sessions referred", "number"),
    ("gp_name", "Referring GP", "text"),
    ("gp_provider_number", "Provider number", "text"),
    ("gp_practice", "GP practice", "text"),
]

STATE_LABEL = {
    NEEDS_REVIEW: ("Needs review", "review"),
    BLOCKED: ("Needs a decision", "blocked"),
    NEEDS_PAYER: ("Add payer in Nookal", "payer"),
    DONE: ("Done", "done"),
}

CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       margin: 0; padding: 24px; background: #f6f7f9; color: #1c2024; }
@media (prefers-color-scheme: dark) {
  body { background: #16181c; color: #e8eaed; }
  .card, table { background: #22252a !important; }
  input { background: #191b1f; color: #e8eaed; border-color: #3a3f46; }
  a { color: #7fb2ff; }
}
.wrap { max-width: 940px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: #6b7280; margin: 0 0 24px; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 12px 14px; border-bottom: 1px solid #e5e7eb33; }
th { font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
     color: #6b7280; }
.card { background: #fff; border-radius: 10px; padding: 20px; margin-bottom: 18px; }
.pill { display: inline-block; font-size: 12px; font-weight: 600;
        padding: 3px 9px; border-radius: 99px; }
.review { background: #fef3c7; color: #92400e; }
.blocked { background: #fee2e2; color: #991b1b; }
.payer  { background: #dbeafe; color: #1e40af; }
.done   { background: #dcfce7; color: #166534; }
.field { display: grid; grid-template-columns: 200px 1fr; gap: 14px;
         align-items: start; margin-bottom: 14px; }
label { padding-top: 8px; font-weight: 600; font-size: 14px; }
input { width: 100%; padding: 8px 10px; font-size: 15px;
        border: 1px solid #d1d5db; border-radius: 7px; }
.ok input { border-color: #86c79b; }
.note { font-size: 13px; color: #6b7280; margin-top: 4px; }
.note.warn { color: #b45309; }
button { font-size: 15px; font-weight: 600; padding: 10px 18px;
         border: 0; border-radius: 8px; cursor: pointer; }
.primary { background: #2563eb; color: #fff; }
.plain { background: #e5e7eb; color: #1c2024; }
.banner { padding: 14px 16px; border-radius: 9px; margin-bottom: 18px; }
.banner.warn { background: #fef3c7; color: #92400e; }
.banner.err { background: #fee2e2; color: #991b1b; }
.banner.good { background: #dcfce7; color: #166534; }
dl { display: grid; grid-template-columns: 220px 1fr; gap: 8px 14px; margin: 0; }
dt { font-weight: 600; }
dd { margin: 0; }
a.back { display: inline-block; margin-bottom: 16px; }
"""


def e(value) -> str:
    return html.escape("" if value is None else str(value))


def page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{e(title)}</title><style>{CSS}</style></head>"
            f"<body><div class='wrap'>{body}</div></body></html>").encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    # ------------------------------------------------------------ plumbing

    @property
    def queue(self) -> Queue:
        return self.server.queue

    def send_html(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        return {k: v[0].strip()
                for k, v in urllib.parse.parse_qs(raw).items()}

    # ---------------------------------------------------------------- GET

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self.send_html(self.render_list())
        if path.startswith("/r/") and path.endswith("/pdf"):
            return self.send_pdf(path.split("/")[2])
        if path.startswith("/r/"):
            item_id = path.split("/")[2]
            if self.queue.get(item_id) is None:
                return self.send_html(page("Not found",
                                           "<h1>Not found</h1>"), 404)
            return self.send_html(self.render_item(item_id))
        self.send_html(page("Not found", "<h1>Not found</h1>"), 404)

    def send_pdf(self, item_id: str) -> None:
        item = self.queue.get(item_id)
        if not item or not os.path.exists(item.pdf_path):
            return self.send_html(page("Missing", "<h1>PDF not found</h1>"),
                                  404)
        with open(item.pdf_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition",
                         f'inline; filename="{os.path.basename(item.pdf_path)}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --------------------------------------------------------------- POST

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        parts = path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "r":
            return self.send_html(page("Not found", "<h1>Not found</h1>"), 404)
        item_id = parts[1]
        action = parts[2] if len(parts) > 2 else "save"
        item = self.queue.get(item_id)
        if not item:
            return self.send_html(page("Not found", "<h1>Not found</h1>"), 404)

        form = self.read_form()
        if action in ("save", "create"):
            for name, _label, _kind in EDITABLE:
                value = form.get(name, "")
                if value:
                    item.fields[name] = value
                    item.confidence[name] = "edited"
                else:
                    item.fields.pop(name, None)
            if item.fields.get("services_count"):
                try:
                    item.fields["services_count"] = int(
                        item.fields["services_count"])
                except ValueError:
                    pass
            self.queue.save(item)

        if action == "create":
            client = self.server.client
            if client is None:
                item.message = ("No Nookal connection — start the server "
                                "with a valid nookal_config.json.")
                item.state = BLOCKED
                self.queue.save(item)
            else:
                create_in_nookal(client, item, queue=self.queue)
        elif action == "done":
            item.state = DONE
            self.queue.save(item)

        self.redirect(f"/r/{item_id}")

    # ------------------------------------------------------------- render

    def render_list(self) -> bytes:
        items = self.queue.all()
        pending = [i for i in items if i.state in (NEEDS_REVIEW, BLOCKED)]
        rows = []
        for item in items:
            label, cls = STATE_LABEL.get(item.state, (item.state, ""))
            flagged = len(item.flagged())
            rows.append(
                f"<tr><td><a href='/r/{e(item.id)}'>{e(item.display_name)}</a>"
                f"</td><td><span class='pill {cls}'>{e(label)}</span></td>"
                f"<td>{e(item.fields.get('referral_date') or '—')}</td>"
                f"<td>{flagged if item.state == NEEDS_REVIEW else '—'}</td>"
                f"<td>{e(item.received[:10])}</td></tr>")
        table = ("<table><tr><th>Patient</th><th>Status</th>"
                 "<th>Referral date</th><th>To check</th><th>Received</th>"
                 "</tr>" + ("".join(rows) or
                            "<tr><td colspan='5'>Nothing in the queue.</td>"
                            "</tr>") + "</table>")
        return page("Referrals", (
            f"<h1>Referrals</h1>"
            f"<p class='sub'>{len(pending)} waiting for review</p>{table}"))

    def render_item(self, item_id: str) -> bytes:
        item = self.queue.get(item_id)
        if not item:
            return page("Not found", "<h1>Not found</h1>")

        banner = ""
        if item.message:
            kind = "err" if item.state == BLOCKED else "good"
            banner = f"<div class='banner {kind}'>{e(item.message)}</div>"

        if item.state in (NEEDS_PAYER, DONE):
            return page(item.display_name,
                        self.render_created(item, banner))

        rows = []
        for name, label, kind in EDITABLE:
            value = item.fields.get(name, "")
            confidence = item.confidence.get(name, "missing")
            note = item.notes.get(name, "")
            cls = "field ok" if confidence == "ok" else "field"
            warn = " warn" if confidence in ("missing", "check") else ""
            hint = (f"<div class='note{warn}'>{e(note)}</div>"
                    if note else "")
            if name == "services_count" and not value:
                # Pre-fill the MBS default, and say that is what it is —
                # the reviewer confirms it by creating, or corrects it.
                value = DEFAULT_SESSIONS
                hint = ("<div class='note warn'>Not stated on the referral — "
                        f"pre-filled with the MBS default of "
                        f"{DEFAULT_SESSIONS} per calendar year. Confirm "
                        "before creating. Never enter 0 in Nookal: that "
                        "means Unlimited.</div>")
            rows.append(
                f"<div class='{cls}'><label for='{name}'>{e(label)}</label>"
                f"<div><input id='{name}' name='{name}' type='{kind}' "
                f"value='{e(value)}'>{hint}</div></div>")

        source = ("read directly from the PDF" if item.source == "digital"
                  else "read by OCR from a scan")
        return page(item.display_name, f"""
          <a class='back' href='/'>&larr; All referrals</a>
          {banner}
          <h1>{e(item.display_name)}</h1>
          <p class='sub'>{e(item.pages)} page(s), {source} &middot;
             <a href='/r/{e(item.id)}/pdf' target='_blank'>open the PDF</a></p>
          <form method='post' action='/r/{e(item.id)}/create'>
            <div class='card'>{''.join(rows)}</div>
            <button class='primary' type='submit'>Create in Nookal</button>
            <button class='plain' type='submit'
                    formaction='/r/{e(item.id)}/save'>Save for later</button>
          </form>
        """)

    def render_created(self, item, banner: str) -> str:
        payer = item.nookal.get("payer", {})
        sessions = payer.get("sessions")
        warn = payer.get("sessions_warning", "")
        done_button = ""
        if item.state == NEEDS_PAYER:
            done_button = (f"<form method='post' action='/r/{e(item.id)}/done'>"
                           "<button class='primary' type='submit'>"
                           "Payer added — mark done</button></form>")
        return f"""
          <a class='back' href='/'>&larr; All referrals</a>
          {banner}
          <h1>{e(item.display_name)}</h1>
          <p class='sub'>Patient {e(item.nookal.get('patient_id'))} &middot;
             case {e(item.nookal.get('case_id'))} &middot;
             <a href='/r/{e(item.id)}/pdf' target='_blank'>open the PDF</a></p>
          <div class='banner warn'>Now add the payer in Nookal by hand:
             Case &rarr; Add Payer &rarr; Medicare. {e(warn)}</div>
          <div class='card'><dl>
            <dt>Payer type</dt><dd>Medicare</dd>
            <dt>Sessions</dt><dd>{e(sessions if sessions else 'not stated')}</dd>
            <dt>Referring GP</dt><dd>{e(payer.get('referring_gp') or '—')}</dd>
            <dt>Provider number</dt>
            <dd>{e(payer.get('provider_number') or '—')}</dd>
            <dt>Referral date</dt>
            <dd>{e(payer.get('referral_date') or '—')}</dd>
          </dl></div>
          {done_button}
        """


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def scan_inbox(queue: Queue, inbox: str) -> int:
    """Pull any PDF in the inbox that isn't in the queue yet."""
    if not os.path.isdir(inbox):
        return 0
    known = {os.path.abspath(i.pdf_path) for i in queue.all()}
    added = 0
    for name in sorted(os.listdir(inbox)):
        if not name.lower().endswith(".pdf"):
            continue
        path = os.path.abspath(os.path.join(inbox, name))
        if path in known:
            continue
        try:
            queue.add_pdf(path)
            added += 1
        except Exception as exc:                       # noqa: BLE001
            print(f"  could not read {name}: {exc}")
    return added


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--queue", default="queue")
    ap.add_argument("--inbox", default="inbox",
                    help="folder watched for new referral PDFs")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config", default="nookal_config.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="review normally, but make every Nookal write a "
                         "logged no-op")
    args = ap.parse_args()

    queue = Queue(args.queue)
    added = scan_inbox(queue, args.inbox)
    if added:
        print(f"  read {added} new referral(s) from {args.inbox}/")

    try:
        client = NookalClient(NookalConfig.load(args.config),
                              dry_run=args.dry_run)
    except NookalError as exc:
        client = None
        print(f"  NOT connected to Nookal: {exc}")
        print("  Review still works; 'Create in Nookal' will not.")

    server = ReviewServer(("127.0.0.1", args.port), Handler)
    server.queue = queue
    server.client = client
    if args.dry_run:
        print("  DRY RUN — nothing will be written to Nookal.")
    print(f"\n  Review screen: http://127.0.0.1:{args.port}\n"
          "  Leave this window open. Press Control-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("  stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
