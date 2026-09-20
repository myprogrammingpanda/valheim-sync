# Valheim Sync Coordinator — Setup

A tiny Cloudflare Worker that tracks exactly one thing: who's currently
hosting, and which save file is the current authoritative one. This is
the piece every companion app (and the optional Discord bot) talks to —
it's the one thing a new friend group actually needs deployed for the
save-sync app to work at all.

Runs on Cloudflare's free tier. Two ways to set it up: through
Cloudflare's dashboard (no command line at all), or with `wrangler`.
Either produces the same result — pick whichever you're more
comfortable with.

---

## Option A: Dashboard (no command line)

### 1. Create the Worker

1. Log into [dash.cloudflare.com](https://dash.cloudflare.com).
2. Go to **Workers & Pages** in the sidebar → **Create**.
3. Choose to start from a **Worker** (the default "Hello World" template
   is fine as a starting point) and deploy it. Name it something like
   `valheim-sync-coordinator` — this becomes part of its public URL
   (`https://<name>.<your-subdomain>.workers.dev`).
4. Once deployed, click **Edit code** to open the built-in editor.
5. Delete the placeholder code and paste in the full contents of
   `worker.js` from this folder. Save and deploy from the editor.

### 2. Create the KV namespace

1. In the sidebar, find **KV** (under Workers & Pages, or under
   Storage). Click **Create a namespace**, name it anything (e.g.
   `HOST_KV`) — the name here is just a label.
2. Go back to your Worker → **Settings** → **Bindings** (sometimes
   labeled **Variables and Bindings**).
3. Add a **KV Namespace** binding: variable name must be exactly
   `HOST_KV` (this is what `worker.js` expects — `env.HOST_KV`), and
   select the namespace you just created.
4. Save.

### 3. Set the shared secret

1. Still in the Worker's **Settings**, find **Variables and Secrets**
   (sometimes under **Environment Variables**).
2. Add a variable named `SHARED_SECRET`, set its type to **Secret**
   (encrypted, not plaintext), and give it any random string as the
   value — this is what every companion app's `worker_secret` /
   `worker_url` pairing needs to match.
3. Save and deploy.

### 4. Test it

Open a terminal (just for this one check — everything above needed
none) or use any HTTP client, and run:

```bash
curl -H "X-Auth: YOUR_SECRET" https://valheim-sync-coordinator.YOUR-SUBDOMAIN.workers.dev/status
```

You should get back something like:
```json
{"hosting":false,"host_name":null,"since":null,"save_version":0,"save_key":null}
```

That URL and secret are what go into the companion app's Settings
window (**Coordinator URL** / **Coordinator shared secret**).

---

## Option B: Command line (`wrangler`)

```bash
npm install -g wrangler
wrangler login
```

From this folder:

```bash
wrangler kv namespace create HOST_KV
```

Paste the returned `id` into `wrangler.toml`'s `kv_namespaces` block
(it's already there as a placeholder), then:

```bash
wrangler secret put SHARED_SECRET
```

**Note**: if you pipe the value in (e.g. `echo "value" | wrangler secret put ...`)
on Windows PowerShell, the pipe can silently append a trailing newline
to the stored secret, causing every request to fail auth for no
apparent reason. Either type it at the interactive prompt, or pipe it
from a tool that doesn't add a trailing newline (e.g. `printf '%s' "value" | wrangler secret put SHARED_SECRET`
from a Bash-compatible shell).

Then deploy:

```bash
wrangler deploy
```

Wrangler prints the same kind of URL as the dashboard path. Test it the
same way as step 4 above.

---

## Notes

- This Worker has no concept of "games" — it tracks a single global
  hosting claim, shared across however many games a companion app is
  configured for. That's intentional: it keeps this piece simple, and
  is why only one hosting session can be active at a time, period,
  regardless of which game.
- The optional Discord bot (`moonberry-discord-bot`) is a separate
  Worker that calls this one's `/status` endpoint — see its own
  `SETUP.md` if you want Discord notifications later. It's entirely
  optional; nothing here depends on it.
