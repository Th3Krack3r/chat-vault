# Chat Vault

A free tool that backs up and searches your AI conversations.

Your best AI answers are scattered across ChatGPT, Claude, and Gemini,
unsearchable, and one account problem away from gone. Chat Vault imports
your chat exports and makes them searchable — locally, on your own machine.
No accounts, no external calls, no data leaving your computer.

## Run it

Python 3 only, standard library only — nothing to install.

```bash
python3 app.py
```

Then open <http://127.0.0.1:8000> in your browser. To use a different port:

```bash
python3 app.py 8765
```

Chat Vault stores everything in `vault.db` (SQLite, with FTS5 full-text
search) in this folder. Delete that file to wipe the vault and start over.

## How to export your chats

**ChatGPT:** Settings → Data controls → Export. You get a zip file — unzip
it and upload the `conversations.json` inside.

**Claude:** Settings → Data controls → Export, then upload the
`conversations.json` from the export.

On the **Import chats** page, upload one file at a time. Importing the same
file twice skips the duplicates. If a file matches neither format, Chat Vault
says so plainly instead of silently importing nothing.

## Features

- **Import** ChatGPT and Claude `conversations.json` exports (format detected
  automatically).
- **Search** all imported conversations, ranked by relevance, with a snippet
  around each match.
- **Conversation view** with the full message history for one conversation.
- **Markdown export** of any conversation, from its conversation page.
- **Stats line** on every page: total conversations and messages indexed.

## Monetization

Chat Vault is free, and it makes money two ways:

1. **Ads.** The layout includes an ad slot on every page. To fill it, open
   `config.py` and paste your ad network's embed code into `AD_SLOT_HTML`:

   ```python
   AD_SLOT_HTML = '<script async src="https://your-ad-network.example/tag.js"></script>'
   ```

   Until then the slot shows a placeholder. The slot div in the page HTML
   is marked with `<!-- PASTE AD NETWORK CODE HERE -->`.

2. **Chat Vault Pro — $9 one-time.** The site carries a Pro upsell section
   describing multi-file import in one go plus Markdown export. The checkout
   button links to `STRIPE_PRO_LINK` in `config.py`. It ships as a
   placeholder — replace it with a real Stripe payment link before selling:

   ```python
   STRIPE_PRO_LINK = "https://buy.stripe.com/your-real-link"
   ```

   The Markdown export itself is built and working in this MVP; multi-file
   import is the paid-tier feature to add once checkout is live.

Because the tool runs entirely locally with no server costs per user, ad and
Pro revenue is nearly all margin. The honest caveat: ads pay little per
visitor, so the Pro tier is where the real income is — the free tool's job
is to earn the traffic that converts.
