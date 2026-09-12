/**
 * Moonberry
 * --------
 * A generic Discord bot for game-hosting notifications, built to support
 * more than one game later without restructuring anything.
 *
 * How it works:
 *   1. Each game's companion app (e.g. the Valheim save-sync app) calls
 *      POST /notify/<game> whenever something worth announcing happens
 *      (right now: "someone started hosting"). This Worker then posts a
 *      message to that game's configured Discord channel.
 *   2. Discord sends slash-command invocations (e.g. /status) to
 *      POST /interactions. This Worker verifies the request really came
 *      from Discord, then replies with live status pulled directly from
 *      that game's own coordinator.
 *
 * ADDING A NEW GAME LATER: just add one entry to the GAMES object below
 * with its own channel ID, recommended password, and coordinator details.
 * Nothing else in this file needs to change.
 *
 * NOTE: signature verification uses the "discord-interactions" npm
 * package rather than hand-rolled WebCrypto Ed25519 calls -- this is
 * the approach basically every working Cloudflare Workers Discord bot
 * example uses, since it's a well-tested implementation rather than
 * something built from scratch. Run `npm install` in this folder
 * before deploying (see SETUP.md).
 */

import { verifyKey } from "discord-interactions";
import { GAMES } from "./games.config.js";

// ---------------------------------------------------------------------
// Game registry lives in games.config.js (gitignored, not committed).
// See games.config.example.js for the template.
// ---------------------------------------------------------------------

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

// ---------------------------------------------------------------------
// Discord REST helpers
// ---------------------------------------------------------------------

async function postDiscordMessage(env, channelId, content) {
  const res = await fetch(`https://discord.com/api/v10/channels/${channelId}/messages`, {
    method: "POST",
    headers: {
      Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ content }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Discord API error ${res.status}: ${text}`);
  }
  return res.json();
}

async function fetchGameStatus(game, env) {
  const headers = {
    "X-Auth": game.statusSecret,
    "User-Agent": "Moonberry-Bot/1.0",
  };

  let res;
  if (game.statusBinding && env[game.statusBinding]) {
    // Preferred path: direct Worker-to-Worker call via Service Binding,
    // bypassing the public internet entirely (and Cloudflare's 1042
    // loop-prevention block on Worker-to-workers.dev fetches).
    res = await env[game.statusBinding].fetch(game.statusUrl, { headers });
  } else {
    // Fallback for a future game whose coordinator ISN'T a Cloudflare
    // Worker on this account (e.g. hosted elsewhere) -- ordinary public
    // fetch works fine in that case, since the 1042 restriction only
    // applies to Worker-to-Worker calls on workers.dev.
    res = await fetch(game.statusUrl, { headers });
  }

  if (!res.ok) {
    const bodyText = await res.text().catch(() => "(no body)");
    throw new Error(`Status fetch failed: ${res.status} - ${bodyText}`);
  }
  return res.json();
}


// ---------------------------------------------------------------------
// Discord Interactions signature verification (required by Discord --
// this proves a request claiming to be from Discord actually is)
// ---------------------------------------------------------------------

async function verifyDiscordRequest(request, publicKeyHex, body) {
  const signature = request.headers.get("X-Signature-Ed25519");
  const timestamp = request.headers.get("X-Signature-Timestamp");
  if (!signature || !timestamp) return false;

  return verifyKey(body, signature, timestamp, publicKeyHex);
}

// ---------------------------------------------------------------------
// Slash command handling
// ---------------------------------------------------------------------

const GAME_CHOICES = Object.keys(GAMES).map((key) => ({
  name: GAMES[key].displayName,
  value: key,
}));

const CHANNEL_KV_PREFIX = "channel:";

async function getChannelForGame(env, gameKey, fallbackChannelId) {
  const stored = await env.MOONBERRY_KV.get(CHANNEL_KV_PREFIX + gameKey);
  return stored || fallbackChannelId;
}

async function setChannelForGame(env, gameKey, channelId) {
  await env.MOONBERRY_KV.put(CHANNEL_KV_PREFIX + gameKey, channelId);
}

async function handleStatusCommand(interaction, env) {
  const gameOption = interaction.data.options?.find((o) => o.name === "game");
  const gameKey = gameOption ? gameOption.value : Object.keys(GAMES)[0];
  const game = GAMES[gameKey];

  if (!game) {
    return { content: `Unknown game "${gameKey}".` };
  }

  try {
    const status = await fetchGameStatus(game, env);
    if (status.hosting) {
      const codePart = status.join_code ? ` | Join Code: **${status.join_code}**` : "";
      return {
        content: `${game.emoji} **${game.displayName}**: ${status.host_name} is currently hosting${codePart}`,
      };
    } else {
      return {
        content: `${game.emoji} **${game.displayName}**: nobody is hosting right now.`,
      };
    }
  } catch (e) {
    return { content: `⚠️ Couldn't reach the ${game.displayName} coordinator right now.` };
  }
}

async function handleSetChannelCommand(interaction, env) {
  const gameOption = interaction.data.options?.find((o) => o.name === "game");
  const gameKey = gameOption ? gameOption.value : Object.keys(GAMES)[0];
  const game = GAMES[gameKey];

  if (!game) {
    return { content: `Unknown game "${gameKey}".` };
  }

  // The channel this command was typed in becomes the new notification
  // target -- no need to paste a channel ID manually.
  const channelId = interaction.channel_id;
  await setChannelForGame(env, gameKey, channelId);

  return {
    content: `${game.emoji} Got it — ${game.displayName} hosting notifications will now be posted in this channel.`,
  };
}

// ---------------------------------------------------------------------
// Main request router
// ---------------------------------------------------------------------

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // --- Discord Interactions endpoint (slash commands) ---
    if (request.method === "POST" && url.pathname === "/interactions") {
      const bodyText = await request.text();
      const valid = await verifyDiscordRequest(request, env.DISCORD_PUBLIC_KEY, bodyText);
      if (!valid) {
        return new Response("Invalid request signature", { status: 401 });
      }

      const interaction = JSON.parse(bodyText);

      // Discord's handshake ping -- must reply with type 1
      if (interaction.type === 1) {
        return json({ type: 1 });
      }

      // Slash command invocation
      if (interaction.type === 2 && interaction.data.name === "status") {
        const reply = await handleStatusCommand(interaction, env);
        return json({ type: 4, data: reply });
      }

      if (interaction.type === 2 && interaction.data.name === "set-channel") {
        const reply = await handleSetChannelCommand(interaction, env);
        return json({ type: 4, data: reply });
      }

      return json({ type: 4, data: { content: "Unknown command." } });
    }

    // --- POST /notify/<game> -- called by a game's companion app ---
    if (request.method === "POST" && url.pathname.startsWith("/notify/")) {
      const gameKey = url.pathname.split("/notify/")[1];
      const game = GAMES[gameKey];
      if (!game) {
        return json({ error: "unknown_game" }, 404);
      }

      const auth = request.headers.get("X-Auth");
      if (!auth || auth !== env.NOTIFY_SECRET) {
        return json({ error: "unauthorized" }, 401);
      }

      const body = await request.json().catch(() => ({}));
      const hostName = body.host_name || "Someone";
      const joinCode = body.join_code || null;
      const event = body.event || "started"; // "started" or "ended"

      let message;
      if (event === "ended") {
        message = `${game.emoji} **${hostName}** stopped hosting ${game.displayName}. World save synced to the cloud.`;
      } else {
        const codeLine = joinCode
          ? `Join Code: **${joinCode}**`
          : `Join via Steam invite (no join code found this session)`;

        message =
          `${game.emoji} **${hostName}** just started hosting ${game.displayName}!\n` +
          `Password: \`${game.recommendedPassword}\`\n` +
          codeLine;
      }

      try {
        const targetChannelId = await getChannelForGame(env, gameKey, game.channelId);
        await postDiscordMessage(env, targetChannelId, message);
        return json({ ok: true });
      } catch (e) {
        return json({ ok: false, error: e.message }, 500);
      }
    }

    return json({ error: "not_found" }, 404);
  },
};
