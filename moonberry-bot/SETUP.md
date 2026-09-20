# Moonberry Setup

A generic Discord bot for game-hosting notifications. Right now it knows
about Valheim; adding another game later is just adding one config block
to `worker.js` — no restructuring needed.

Runs on Cloudflare Workers (same free account/pattern as your existing
Valheim coordinator) — no new hosting service to sign up for.

---

## 1. Create the Discord Application + Bot

1. Go to https://discord.com/developers/applications → **New Application**.
   Name it "Moonberry".
2. In the sidebar, go to **Bot** → **Add Bot**.
3. Under **Privileged Gateway Intents**, you don't need to enable any of
   these — Moonberry never listens to messages, only responds to slash
   commands and posts on its own.
4. Click **Reset Token** (or **Copy**) to get your **Bot Token**. Save it
   somewhere safe — this is `DISCORD_BOT_TOKEN`.
5. Go to **General Information** in the sidebar. Copy the **Public Key**
   — this is `DISCORD_PUBLIC_KEY`. Also copy the **Application ID** from
   here, you'll need it for command registration.

## 2. Invite the bot to your server

1. Go to **OAuth2** → **URL Generator**.
2. Under **Scopes**, check `bot` and `applications.commands`.
3. Under **Bot Permissions**, check `Send Messages` and `Read Message History`.
4. Copy the generated URL at the bottom, open it in a browser, and invite
   Moonberry to your server.

## 3. Get your Discord Channel ID

1. In Discord, go to **User Settings → Advanced** and turn on **Developer Mode**.
2. Right-click the channel you want Moonberry to post hosting alerts in,
   click **Copy Channel ID**.

## 4. Fill in worker.js

Open `worker.js` and edit the `GAMES` object:

```js
valheim: {
  displayName: "Valheim",
  emoji: "🌙",
  channelId: "PASTE_YOUR_CHANNEL_ID",
  recommendedPassword: "PASTE_YOUR_GROUP_PASSWORD",
  statusUrl: "https://valheim-sync-coordinator.baikings.workers.dev/status",
  statusSecret: "REPLACE_WITH_YOUR_COORDINATOR_SHARED_SECRET",   // same secret as your existing coordinator
},
```

## 5. Install dependencies and deploy

```bash
cd moonberry-bot
npm install
wrangler deploy
```

The `npm install` step matters — this Worker uses the `discord-interactions`
library for signature verification (a well-tested approach, rather than
hand-rolled crypto). Skipping it will cause the deploy to fail or Discord's
"could not verify" error when you set the Interactions Endpoint URL.

Wrangler prints a URL like `https://moonberry.yoursubdomain.workers.dev`.

Then set the three secrets:

```bash
wrangler secret put DISCORD_BOT_TOKEN
wrangler secret put DISCORD_PUBLIC_KEY
wrangler secret put NOTIFY_SECRET   # make up any random string, used to
                                     # authenticate calls from your
                                     # companion apps
```

Redeploy once more after setting secrets: `wrangler deploy`.

## 6. Point Discord at your bot

1. Back in the Discord Developer Portal → **General Information**.
2. Set **Interactions Endpoint URL** to:
   `https://moonberry.yoursubdomain.workers.dev/interactions`
3. Discord will immediately test this URL (sends a ping) — if it goes
   green/saves successfully, verification worked. If it fails, double
   check the Worker deployed successfully and the public key is correct.

## 7. Register the `/status` slash command

This is a one-time REST call (not something `wrangler` does for you).
Run this once from any terminal with `curl` installed, filling in your
own Application ID and Bot Token, and your Discord Server (Guild) ID
(right-click your server icon → Copy Server ID with Developer Mode on):

```bash
curl -X POST "https://discord.com/api/v10/applications/YOUR_APPLICATION_ID/guilds/YOUR_SERVER_ID/commands" \
  -H "Authorization: Bot YOUR_BOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "status",
    "description": "Check who is currently hosting",
    "options": [
      {
        "name": "game",
        "description": "Which game to check",
        "type": 3,
        "required": false,
        "choices": [
          { "name": "Valheim", "value": "valheim" }
        ]
      }
    ]
  }'
```

Guild-specific commands (using your server ID) show up instantly. Global
commands (omitting `/guilds/YOUR_SERVER_ID`) can take up to an hour to
propagate — use guild-specific while testing.

**When you add a new game later:** re-run this same curl command with an
updated `choices` array including the new game, so `/status` can offer it
as an option too.

## 8. Test it

In Discord, type `/status` — Moonberry should reply with current Valheim
hosting status, live from your coordinator.

For the "posts when someone hosts" feature, that's triggered by the
Valheim companion app itself (see the updated `main.py` — it now calls
Moonberry's `/notify/valheim` endpoint automatically). Start hosting from
the companion app and you should see Moonberry post in your channel.
