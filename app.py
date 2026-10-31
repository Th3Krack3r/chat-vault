#!/usr/bin/env python3
"""Chat Vault — back up and search your AI conversations, locally.

Python 3 standard library only. Run with:

    python3 app.py            # serves on http://127.0.0.1:8000
    python3 app.py 8765       # serves on a different port

Imports ChatGPT and Claude chat exports (conversations.json) into a local
SQLite database with FTS5 full-text search. No accounts, no external calls:
everything stays on the user's own machine.
"""

import hashlib
import html
import json
import os
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vault.db")


# ---------------------------------------------------------------- database

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS conversations (
               id INTEGER PRIMARY KEY,
               title TEXT NOT NULL,
               source TEXT NOT NULL,
               source_hash TEXT UNIQUE,
               message_count INTEGER NOT NULL DEFAULT 0
           )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS messages (
               id INTEGER PRIMARY KEY,
               conversation_id INTEGER NOT NULL REFERENCES conversations(id),
               idx INTEGER NOT NULL,
               role TEXT NOT NULL,
               text TEXT NOT NULL
           )"""
    )
    # Standalone FTS5 index (message_id links back to messages.id).
    db.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
           USING fts5(message_id UNINDEXED, conversation_id UNINDEXED, text)"""
    )
    db.commit()
    return db


def stats(db):
    convs = db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    msgs = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return convs, msgs


# ---------------------------------------------------------------- parsing

def _chatgpt_messages(conv):
    """Yield (role, text) from one ChatGPT conversation dict."""
    mapping = conv.get("mapping") or {}
    for node in mapping.values():
        msg = (node or {}).get("message")
        if not msg:
            continue
        role = (msg.get("author") or {}).get("role") or "unknown"
        content = msg.get("content") or {}
        parts = content.get("parts")
        if isinstance(parts, list):
            text = "".join(p for p in parts if isinstance(p, str))
        else:
            text = content.get("text") or ""
        text = (text or "").strip()
        if text:
            yield role, text


def _claude_messages(conv):
    """Yield (role, text) from one Claude conversation dict."""
    for m in conv.get("chat_messages") or []:
        sender = m.get("sender") or m.get("role") or "unknown"
        role = {"human": "user", "user": "user", "assistant": "assistant"}.get(
            sender, sender
        )
        text = m.get("text")
        if text is None:
            blocks = m.get("content") or []
            text = "".join(
                b.get("text", "") for b in blocks if isinstance(b, dict)
            )
        text = (text or "").strip()
        if text:
            yield role, text


def parse_export(raw_bytes):
    """Parse an export file.

    Returns (conversations, errors) where conversations is a list of
    (title, source, [(role, text), ...]) and errors is a list of strings.
    A file whose entries match neither known format is rejected with a
    clear error instead of silently importing nothing.
    """
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [], ["That file is not valid JSON: %s" % exc]
    if isinstance(data, dict):
        # Tolerate a wrapper object holding the list.
        for key in ("conversations", "data", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return [], [
            "Unrecognized format: expected a JSON list of conversations. "
            "ChatGPT exports a conversations.json with a 'mapping' per "
            "conversation; Claude exports a conversations.json with "
            "'chat_messages' per conversation."
        ]

    conversations, errors = [], []
    for i, conv in enumerate(data):
        if not isinstance(conv, dict):
            errors.append("Entry %d is not an object; skipped." % i)
            continue
        if conv.get("mapping") is not None:
            title = conv.get("title") or "Untitled conversation"
            msgs = list(_chatgpt_messages(conv))
            source = "ChatGPT"
        elif conv.get("chat_messages") is not None:
            title = conv.get("name") or conv.get("title") or "Untitled conversation"
            msgs = list(_claude_messages(conv))
            source = "Claude"
        else:
            errors.append(
                "Entry %d (%s) matches neither the ChatGPT format "
                "('mapping') nor the Claude format ('chat_messages'); "
                "skipped." % (i, conv.get("title") or conv.get("name") or "?")
            )
            continue
        if not msgs:
            errors.append("'%s' has no readable messages; skipped." % title)
            continue
        conversations.append((title, source, msgs))
    if not conversations and not errors:
        errors.append("No conversations found in that file.")
    return conversations, errors


def import_conversations(db, conversations):
    """Insert parsed conversations, skipping exact duplicates.

    Returns (imported, skipped).
    """
    imported, skipped = 0, 0
    for title, source, msgs in conversations:
        digest = hashlib.sha256(
            json.dumps([title, msgs], sort_keys=True).encode("utf-8")
        ).hexdigest()
        exists = db.execute(
            "SELECT id FROM conversations WHERE source_hash = ?", (digest,)
        ).fetchone()
        if exists:
            skipped += 1
            continue
        cur = db.execute(
            "INSERT INTO conversations (title, source, source_hash, message_count)"
            " VALUES (?, ?, ?, ?)",
            (title, source, digest, len(msgs)),
        )
        conv_id = cur.lastrowid
        for idx, (role, text) in enumerate(msgs):
            mid = db.execute(
                "INSERT INTO messages (conversation_id, idx, role, text)"
                " VALUES (?, ?, ?, ?)",
                (conv_id, idx, role, text),
            ).lastrowid
            db.execute(
                "INSERT INTO messages_fts (message_id, conversation_id, text)"
                " VALUES (?, ?, ?)",
                (mid, conv_id, text),
            )
        imported += 1
    db.commit()
    return imported, skipped


# ---------------------------------------------------------------- search

def _fts_query(query):
    words = re.findall(r"[\w]+", query, flags=re.UNICODE)
    if not words:
        return None
    return " OR ".join('"%s"' % w.replace('"', "") for w in words)


def search(db, query):
    """Return [(conversation_row, snippet_html, message_count)] best-first."""
    fts = _fts_query(query)
    rows = []
    if fts:
        try:
            rows = db.execute(
                """SELECT conversation_id, snippet(messages_fts, 2, '<mark>', '</mark>', '…', 24) AS snip
                   FROM messages_fts WHERE messages_fts MATCH ? ORDER BY rank""",
                (fts,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        # Fallback: plain substring search with a Python-built snippet.
        like = "%%%s%%" % query
        rows = db.execute(
            "SELECT conversation_id, text FROM messages WHERE text LIKE ?",
            (like,),
        ).fetchall()
        rows = [(r["conversation_id"], _python_snippet(r["text"], query))
                for r in rows]
    seen, results = set(), []
    for conv_id, snip in rows:
        if conv_id in seen:
            continue
        seen.add(conv_id)
        conv = db.execute(
            "SELECT * FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        if conv:
            results.append((conv, snip, conv["message_count"]))
    return results


def _python_snippet(text, query):
    low = text.lower()
    pos = low.find(query.lower())
    if pos < 0:
        window = text[:80]
    else:
        start = max(0, pos - 40)
        window = text[start:pos + len(query) + 40]
    safe = html.escape(window)
    safe = safe.replace(html.escape(query), "<mark>%s</mark>" % html.escape(query))
    return ("…" if pos > 40 else "") + safe + ("…" if len(window) < len(text) else "")


# ---------------------------------------------------------------- pages

CSS = """
body{font-family:system-ui,-apple-system,sans-serif;max-width:760px;margin:2rem auto;
padding:0 1rem;color:#1a1a1a;line-height:1.5}
a{color:#1a56db} nav a{margin-right:1rem}
header{border-bottom:2px solid #1a1a1a;padding-bottom:.5rem;margin-bottom:1rem}
.stats{color:#555;font-size:.95rem}
.ad-slot{border:1px dashed #999;background:#fafafa;text-align:center;color:#888;
font-size:.85rem;padding:1.2rem;margin:1.5rem 0}
.pro{border:1px solid #2e7d32;background:#f1f8f1;padding:1rem 1.2rem;margin:1.5rem 0}
.pro h2{margin-top:0;color:#2e7d32}
.btn{display:inline-block;background:#1a56db;color:#fff !important;text-decoration:none;
padding:.5rem 1rem;border-radius:6px;margin-top:.5rem}
.msg{border-left:3px solid #ccc;padding:.4rem .8rem;margin:.6rem 0}
.msg .role{font-weight:700;font-size:.85rem;text-transform:uppercase;color:#555}
.msg.user{border-color:#1a56db}.msg.assistant{border-color:#2e7d32}
mark{background:#ffe082}
input[type=text]{width:70%;padding:.5rem}input[type=submit],button{padding:.5rem 1rem}
footer{margin-top:2rem;border-top:1px solid #ddd;padding-top:.5rem;color:#555;font-size:.9rem}
.error{color:#b00020}.ok{color:#2e7d32}
"""

AD_COMMENT = "<!-- PASTE AD NETWORK CODE HERE -->"


def layout(title, body, db):
    convs, msgs = stats(db)
    ad_inner = config.AD_SLOT_HTML or (
        '<span>Ad space &mdash; paste your ad network code into '
        "config.py (AD_SLOT_HTML) to fill this slot.</span>"
    )
    pro = """
<div class="pro">
<h2>Chat Vault Pro &mdash; $9 one-time</h2>
<p>Pro adds <strong>multi-file import in one go</strong> (drop your whole
export history at once instead of one file at a time) and
<strong>Markdown export</strong> of any conversation, so you can take a great
answer with you anywhere.</p>
<a class="btn" href="%s">Get Chat Vault Pro &mdash; $9 one-time</a>
</div>""" % html.escape(config.STRIPE_PRO_LINK, quote=True)
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>%s &mdash; Chat Vault</title><style>%s</style></head><body>"
            "<header><h1>Chat Vault</h1><nav>"
            "<a href='/'>Home</a><a href='/search'>Search</a>"
            "<a href='/upload'>Import chats</a></nav>"
            "<p class='stats'>%d conversations &middot; %d messages indexed</p>"
            "</header>%s<div class='ad-slot'>%s%s</div>%s"
            "<footer>Your data stays on your own machine. Chat Vault never "
            "sends your conversations anywhere.</footer></body></html>"
            % (html.escape(title), CSS, convs, msgs, body, AD_COMMENT,
               ad_inner, pro))


def home_page(db):
    recent = db.execute(
        "SELECT * FROM conversations ORDER BY id DESC LIMIT 20"
    ).fetchall()
    items = "".join(
        "<li><a href='/conversation?id=%d'>%s</a> (%s, %d messages)</li>"
        % (r["id"], html.escape(r["title"]), html.escape(r["source"]),
           r["message_count"])
        for r in recent
    )
    body = """
<h2>Back up and search your AI conversations</h2>
<p>Your best AI answers are scattered across ChatGPT, Claude, and Gemini,
unsearchable, and one account problem away from gone. Chat Vault imports
your chat exports and makes them searchable &mdash; locally, on your machine.</p>
<form action="/search" method="get">
<input type="text" name="q" placeholder="Search your conversations…" autofocus>
<input type="submit" value="Search">
</form>
<p><a class="btn" href="/upload">Import a chat export</a></p>
<h3>Recent conversations</h3>
<ul>%s</ul>""" % (items or "<li>No conversations imported yet.</li>")
    return layout("Home", body, db)


def search_page(db, query):
    if not query:
        body = """
<h2>Search</h2>
<form action="/search" method="get">
<input type="text" name="q" placeholder="Search your conversations…" autofocus>
<input type="submit" value="Search">
</form>"""
        return layout("Search", body, db)
    results = search(db, query)
    if not results:
        body = ("<h2>Search results for %s</h2><p>No matches found.</p>"
                % html.escape(query))
    else:
        items = "".join(
            "<li><a href='/conversation?id=%d'>%s</a> (%d messages)<br>%s</li>"
            % (r["id"], html.escape(r["title"]), count, snip)
            for r, snip, count in results
        )
        body = ("<h2>Search results for %s</h2><ul>%s</ul>"
                % (html.escape(query), items))
    return layout("Search", body, db)


def upload_page(db, message=""):
    body = """
<h2>Import a chat export</h2>
%s
<p>Upload a <code>conversations.json</code> file exported from ChatGPT or
Claude. Chat Vault detects the format automatically. Importing the same file
twice skips the duplicates.</p>
<form action="/upload" method="post" enctype="multipart/form-data">
<input type="file" name="export" accept=".json,application/json" required>
<input type="submit" value="Import">
</form>
<p>How to export: ChatGPT &mdash; Settings &rarr; Data controls &rarr; Export
(you get a zip; unzip it and upload the <code>conversations.json</code>
inside). Claude &mdash; Settings &rarr; Data controls &rarr; Export.</p>
<p>Free import handles one file at a time. Chat Vault Pro adds multi-file
import in one go.</p>""" % message
    return layout("Import chats", body, db)


def conversation_page(db, conv_id):
    conv = db.execute(
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    if not conv:
        return layout("Not found", "<p class='error'>No such conversation.</p>", db)
    msgs = db.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY idx",
        (conv_id,),
    ).fetchall()
    blocks = "".join(
        "<div class='msg %s'><div class='role'>%s</div>%s</div>"
        % (html.escape(m["role"]), html.escape(m["role"]),
           html.escape(m["text"]).replace("\n", "<br>"))
        for m in msgs
    )
    body = ("<h2>%s</h2><p>%s &middot; %d messages &middot; "
            "<a href='/export?id=%d'>Export as Markdown</a></p>%s"
            % (html.escape(conv["title"]), html.escape(conv["source"]),
               conv["message_count"], conv["id"], blocks))
    return layout(conv["title"], body, db)


def markdown_export(db, conv_id):
    conv = db.execute(
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    if not conv:
        return None
    msgs = db.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY idx",
        (conv_id,),
    ).fetchall()
    parts = ["# %s" % conv["title"], "",
             "_Exported from Chat Vault (%s)._ " % conv["source"], ""]
    for m in msgs:
        parts.append("**%s:**" % m["role"].capitalize())
        parts.append("")
        parts.append(m["text"])
        parts.append("")
    return "\n".join(parts)


# ------------------------------------------------------- multipart parsing

def parse_multipart(body, boundary):
    """Return {field_name: (filename, bytes)} for file parts. Minimal parser,
    sufficient for browser file uploads of JSON text files."""
    files = {}
    sep = b"--" + boundary
    for part in body.split(sep):
        if b"\r\n\r\n" not in part:
            continue
        headers, content = part.split(b"\r\n\r\n", 1)
        if content.endswith(b"\r\n"):
            content = content[:-2]
        name = re.search(rb'name="([^"]+)"', headers)
        fname = re.search(rb'filename="([^"]*)"', headers)
        if name and fname and fname.group(1):
            files[name.group(1).decode()] = (
                fname.group(1).decode(errors="replace"), content
            )
    return files


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, body, status=200, content_type="text/html; charset=utf-8",
              filename=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if filename:
            self.send_header(
                "Content-Disposition", 'attachment; filename="%s"' % filename
            )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        db = get_db()
        try:
            path = urlparse(self.path)
            qs = parse_qs(path.query)
            if path.path in ("/", ""):
                self._send(home_page(db))
            elif path.path == "/search":
                self._send(search_page(db, qs.get("q", [""])[0].strip()))
            elif path.path == "/upload":
                self._send(upload_page(db))
            elif path.path == "/conversation":
                try:
                    conv_id = int(qs.get("id", ["0"])[0])
                except ValueError:
                    conv_id = 0
                self._send(conversation_page(db, conv_id))
            elif path.path == "/export":
                try:
                    conv_id = int(qs.get("id", ["0"])[0])
                except ValueError:
                    conv_id = 0
                md = markdown_export(db, conv_id)
                if md is None:
                    self._send(layout("Not found",
                                     "<p class='error'>No such conversation.</p>",
                                     db), status=404)
                else:
                    title = db.execute(
                        "SELECT title FROM conversations WHERE id = ?",
                        (conv_id,),
                    ).fetchone()["title"]
                    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-") or "conversation"
                    self._send(md, content_type="text/markdown; charset=utf-8",
                               filename=safe + ".md")
            else:
                self._send(layout("Not found",
                                 "<p class='error'>Page not found.</p>", db),
                           status=404)
        finally:
            db.close()

    def do_POST(self):
        db = get_db()
        try:
            if urlparse(self.path).path != "/upload":
                self._send(layout("Not found",
                                 "<p class='error'>Page not found.</p>", db),
                           status=404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            ctype = self.headers.get("Content-Type") or ""
            m = re.search(r"boundary=([^;]+)", ctype)
            if not m or length <= 0:
                self._send(upload_page(
                    db, "<p class='error'>No file was uploaded.</p>"))
                return
            body = self.rfile.read(length)
            files = parse_multipart(body, m.group(1).strip().encode())
            if "export" not in files:
                self._send(upload_page(
                    db, "<p class='error'>No file was uploaded.</p>"))
                return
            filename, raw = files["export"]
            conversations, errors = parse_export(raw)
            if not conversations:
                detail = "<br>".join(html.escape(e) for e in errors)
                self._send(upload_page(
                    db, "<p class='error'>Could not import <b>%s</b>:<br>%s</p>"
                    % (html.escape(filename), detail)))
                return
            imported, skipped = import_conversations(db, conversations)
            notes = []
            if imported:
                notes.append("<p class='ok'>Imported %d conversation(s) from %s.</p>"
                             % (imported, html.escape(filename)))
            if skipped:
                notes.append("<p>Skipped %d duplicate(s) already in the vault.</p>"
                             % skipped)
            if errors:
                notes.append("<p class='error'>Warnings:<br>%s</p>"
                             % "<br>".join(html.escape(e) for e in errors))
            notes.append("<p><a href='/'>Back to home</a></p>")
            self._send(layout("Import result", "".join(notes), db))
        finally:
            db.close()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    get_db().close()  # create tables on first run
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("Chat Vault running at http://127.0.0.1:%d  (Ctrl-C to stop)" % port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
