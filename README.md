# Inbox Sweep

**Bulk-delete Gmail from a local web page, with optional archiving to [Open Archiver](https://github.com/LogicLabs-OU/OpenArchiver) before anything is removed.**

Inbox Sweep is a single Python file with no dependencies. Run it and a clean web UI opens in your browser. Log in with a Google App Password, filter your mail, and trash or permanently delete thousands of emails in seconds. It can also copy important emails (starred, receipts, bills and so on) into your self-hosted Open Archiver first, and it only deletes once the archive confirms the import.

---

## Features

- **Gmail search plus quick filters.** Use any Gmail query (`from:`, `older_than:`, `larger:`, `has:attachment`…). You can also combine filter chips:
  - **Category:** Promotions, Social, Updates, Forums. Several can be picked, and an email matching any of them is included.
  - **Older than:** 6 months to 5 years.
  - **Bigger than:** 5 / 10 / 25 MB.
  - **Status:** read or unread.
  - **Has:** attachment, "unsubscribe", no label.
  - **Folder:** Trash or Spam.
- **Select all matching.** Act on every result, not just the ones loaded on screen.
- **Top senders.** Scans up to 10,000 emails, ranks senders by count and size, and gives each one a **Delete all** button.
- **Two delete modes:**
  - **Move to Trash** (default): recoverable for 30 days.
  - **Permanently delete:** you have to type `DELETE` to confirm.
- **Fast.** Deletes run in parallel over several IMAP connections, with large batches sent as compact UID ranges.
- **Resilient.** When Gmail throws `System Error` or drops a connection, Inbox Sweep backs off, reconnects, splits the batch and retries. It never gives up halfway through a run.
- **Important emails are protected** unless you choose otherwise. You decide what counts as important:
  - Starred
  - Marked important by Gmail
  - Updates tab (orders, receipts, bills)
  - Promotions tab
  - Any custom Gmail search
- **Open Archiver integration.** Important emails can be archived first, and then deleted:
  - Full emails, attachments included, are uploaded as an EML import.
  - Archives are split automatically to fit Open Archiver's upload limit.
  - Nothing is deleted unless every part reports `imported`.
  - A local copy of every archive zip is kept in `~/inbox-sweep-archives/`.
- **Archive only.** Copy selected emails to Open Archiver without deleting them.
- **Private by design:**
  - The page is served only on `127.0.0.1`, protected by a per-run token.
  - Your App Password lives only in memory.
  - Mail goes straight from your machine to `imap.gmail.com` over TLS.

---

## Requirements

- Python 3.8 or newer (standard library only, nothing to `pip install`).
- A Gmail account with **2-Step Verification** turned on (Google requires it for App Passwords).
- *Optional:* a running [Open Archiver](https://github.com/LogicLabs-OU/OpenArchiver) instance with an API key.

Works on Linux, macOS and Windows.

---

## Quick start

### 1. Create a Google App Password

1. Turn on 2-Step Verification: <https://myaccount.google.com/signinoptions/twosv>
2. Go to <https://myaccount.google.com/apppasswords>, enter a name like `Inbox Sweep`, and click **Create**.
3. Copy the 16-letter code.

> If the App Passwords page says it isn't available, 2-Step Verification is off, or your Workspace admin has disabled App Passwords.

### 2. Run it

```bash
git clone https://github.com/Drunk0Smurf/emailsweep.git
cd inbox-sweep
python3 inbox_sweep.py
```

On Windows:

```powershell
py inbox_sweep.py
```

Your browser opens at <http://localhost:8080>. Log in with your Gmail address and the App Password. Press **Ctrl+C** in the terminal to stop.

To use a different port:

```bash
python3 inbox_sweep.py 9000
```

---

## Using it

1. **Pick filters** and/or type a Gmail search. Results update automatically.
2. **Tick emails**, or click **Select all matching**.
3. Under **Important emails**, choose one of:
   - **Skip them** (default): they're never deleted.
   - **Archive to Open Archiver, then delete.**
   - **Archive everything to Open Archiver, then delete.**
   - **Delete them too.**
4. Choose **Move to Trash** or **Permanently delete**, then click **Delete selected**.

**Clearing out a sender:** open **Top senders**, click **Scan**, then **Delete all** next to the sender. The current filters still apply, and the confirmation box shows exactly what will be removed.

**Emptying the Trash:** select the **Trash** folder filter, click **Select all matching**, switch to **Permanently delete**, and delete.

---

## Open Archiver setup (optional)

1. In Open Archiver, go to **Settings → API Keys → Generate API Key** and copy the key.
2. In Inbox Sweep, click **Open Archiver** (top right) and fill in:

   | Field | What to enter |
   |---|---|
   | **Address** | Where Open Archiver is reachable, e.g. `http://192.168.1.50:3000` |
   | **API key** | The key from step 1 |
   | **APP_URL** | Optional. It's detected automatically. If detection fails, enter the `APP_URL` from Open Archiver's `.env` |
   | **Max upload size** | Keep it below Open Archiver's `BODY_SIZE_LIMIT` (default 90) |
   | **What counts as important** | Tick boxes plus an optional custom Gmail search |

3. Click **Test connection**. It should report *Uploads OK*. Then click **Save**.

### How archiving works

1. The selected important emails are downloaded in full (headers, body, attachments).
2. They're packed into zip files of `.eml` files, organised as `Inbox Sweep/YYYY-MM/…`, and split into parts under your max upload size.
3. Each part is uploaded to `POST /api/v1/upload` and registered as an **EML Import** ingestion source named `Inbox Sweep <date> (N emails)`.
4. Inbox Sweep polls Open Archiver until every part reports `imported`.
5. **Only then** are the emails deleted from Gmail. If anything fails (download, upload, import or a timeout), nothing is deleted.

---

## Configuration

Settings are saved to `~/.config/inbox-sweep/config.json`, created with `600` permissions. The tuning constants are at the top of `inbox_sweep.py`:

| Constant | Default | Meaning |
|---|---|---|
| `BATCH` | `750` | Emails per IMAP command. Halves automatically if Gmail struggles |
| `WORKERS` | `3` | Parallel Gmail connections used for deleting (Gmail allows about 15 per account) |
| `MAX_TRIES` | `6` | Retries per batch before giving up on those emails |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| *Gmail rejected the login* | Use the 16-letter **App Password**, not your normal password. |
| *Gmail is busy, retrying…* | Normal under heavy load. Gmail throttles big jobs and Inbox Sweep backs off automatically. If it happens constantly, lower `WORKERS` or `BATCH`. |
| *Gmail refused N emails after several retries* | Wait a minute and click **Delete** again. The remaining emails are still listed. |
| *Origin doesn't match APP_URL* | Open Archiver rejects form uploads whose `Origin` header doesn't match its `APP_URL`. On the Open Archiver server, run `grep -E '^(APP_URL\|ORIGIN)=' .env` and paste that URL into the **APP_URL** field. |
| *Connection reset during upload* | Check `BODY_SIZE_LIMIT` in Open Archiver's `.env` (it's only `512K` if unset). Run `docker exec open-archiver printenv BODY_SIZE_LIMIT` to see the value the container is actually using. If you go through a reverse proxy, raise its upload limit (`client_max_body_size` in nginx) or point Inbox Sweep at `http://<server-ip>:3000` directly. |
| *One email is bigger than your Max upload size* | Raise `BODY_SIZE_LIMIT` in Open Archiver, recreate the container (`docker compose up -d`), then raise **Max upload size** in Inbox Sweep. |
| *Open Archiver is still importing…* | Large imports can take a while. Check the import in Open Archiver, then run the delete again once it finishes. |

---

## Security notes

- The web UI binds to `127.0.0.1` only, and every API call needs a random token generated at startup.
- Your App Password is never written to disk. Revoke it any time at <https://myaccount.google.com/apppasswords>.
- The Open Archiver API key **is** stored in `~/.config/inbox-sweep/config.json` (mode `600`).
- Archive zips in `~/inbox-sweep-archives/` contain full email content. Delete them once you're happy with what's in Open Archiver.

---

## Disclaimer

Permanent deletes can't be undone. Test with **Move to Trash** first. This project isn't affiliated with Google or with LogicLabs (Open Archiver).
