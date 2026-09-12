/**
 * Valheim Save-Sync Coordinator
 * ------------------------------
 * A tiny Cloudflare Worker that tracks exactly one thing:
 * "who is currently hosting the Valheim session, and which exact save
 * file (save_key) is the current authoritative one?"
 *
 * It does NOT store the save file itself — that lives in cloud storage
 * (R2/B2/etc). This Worker only stores a few bytes of JSON in KV.
 *
 * save_key vs save_version:
 *   - save_key is the ACTUAL filename of the current save in storage
 *     (e.g. "world_save_1788900000.zip"). This is the source of truth
 *     clients compare against to decide whether to download.
 *   - save_version is just a human-readable incrementing counter for
 *     display purposes (e.g. "v14") -- it does NOT drive any sync
 *     decisions, so it can never cause the kind of stale-state bugs a
 *     manually-edited local counter could.
 *
 * Endpoints:
 *   GET  /status                -> current host status (public read, needs auth header)
 *   POST /claim   {name}        -> try to become host
 *   POST /release {name, save_key} -> give up host, record the new save file
 *   POST /announce_code {name, join_code} -> attach a Valheim join code
 *                                             to the current host's status
 *
 * Auth: every request must include header  X-Auth: <SHARED_SECRET>
 * (set via `wrangler secret put SHARED_SECRET`). This just keeps
 * randoms on the internet from poking your coordinator — it is not
 * meant to be bank-grade security, just a lock on the front door.
 */

const KV_KEY = "host_status";

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function getStatus(env) {
  const raw = await env.HOST_KV.get(KV_KEY);
  if (!raw) {
    return { hosting: false, host_name: null, since: null, save_version: 0, save_key: null };
  }
  return JSON.parse(raw);
}

async function setStatus(env, status) {
  await env.HOST_KV.put(KV_KEY, JSON.stringify(status));
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // --- auth check ---
    const auth = request.headers.get("X-Auth");
    if (!auth || auth !== env.SHARED_SECRET) {
      return json({ error: "unauthorized" }, 401);
    }

    // --- GET /status ---
    if (request.method === "GET" && url.pathname === "/status") {
      const status = await getStatus(env);
      return json(status);
    }

    // --- POST /claim ---
    if (request.method === "POST" && url.pathname === "/claim") {
      const body = await request.json().catch(() => ({}));
      const name = (body.name || "unknown").toString().slice(0, 64);

      const current = await getStatus(env);
      if (current.hosting) {
        // Someone already hosting — tell the caller who, don't overwrite.
        return json({ ok: false, reason: "already_hosting", current }, 409);
      }

      const next = {
        hosting: true,
        host_name: name,
        since: Date.now(),
        save_version: current.save_version || 0,
        save_key: current.save_key || null,
        join_code: null,
      };
      await setStatus(env, next);
      return json({ ok: true, current: next });
    }

    // --- POST /release ---
    if (request.method === "POST" && url.pathname === "/release") {
      const body = await request.json().catch(() => ({}));
      const name = (body.name || "unknown").toString().slice(0, 64);
      const saveKey = body.save_key ? body.save_key.toString().slice(0, 256) : null;
      const current = await getStatus(env);

      const next = {
        hosting: false,
        host_name: null,
        since: null,
        // save_version is purely informational (shown in Discord etc) --
        // save_key below is what actually drives sync decisions.
        save_version: (current.save_version || 0) + 1,
        save_key: saveKey || current.save_key || null,
        last_host: name,
        released_at: Date.now(),
      };
      await setStatus(env, next);
      return json({ ok: true, current: next });
    }

    // --- POST /announce_code ---
    // Lets the CURRENT host attach a Valheim Join Code to their session,
    // so everyone else's app can display it without anyone typing it
    // into a chat app manually. Only works if the caller is the one
    // currently marked as host (prevents random overwrites).
    if (request.method === "POST" && url.pathname === "/announce_code") {
      const body = await request.json().catch(() => ({}));
      const name = (body.name || "unknown").toString().slice(0, 64);
      const joinCode = (body.join_code || "").toString().slice(0, 32);

      const current = await getStatus(env);
      if (!current.hosting || current.host_name !== name) {
        return json({ ok: false, reason: "not_current_host", current }, 409);
      }

      const next = { ...current, join_code: joinCode };
      await setStatus(env, next);
      return json({ ok: true, current: next });
    }

    return json({ error: "not_found" }, 404);
  },
};
