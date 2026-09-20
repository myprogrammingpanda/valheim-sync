"""
Valheim Save-Sync Companion App
--------------------------------
Runs quietly in the system tray. Its job:

  1. Ask the coordinator (a Cloudflare Worker) whether anyone is
     currently hosting.
       - If yes: tell you who, so you can join them via Steam.
       - If no: claim the host slot, download the latest save from
         cloud storage (R2), and launch Valheim for you.
  2. While you're hosting, watch the Valheim process.
  3. When Valheim closes (you quit), if you were the host:
     zip up the save, upload it to R2, and release the host claim
     so the next person who opens the app can take over.

Everything it touches is scoped to:
  - Valheim's own save folder
  - Valheim's own log file (read-only)
  - The two Cloudflare endpoints you configured

Nothing else on your machine is read, modified, or transmitted.
All actions are written to sync.log next to this script so you can
audit exactly what it did and when.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import boto3
import psutil
import requests

# --------------------------------------------------------------------------
# Version -- bump this with every release you cut on GitHub. Must exactly
# match the tag name you give that release (e.g. "1.0.0" for tag "1.0.0").
# --------------------------------------------------------------------------
APP_VERSION = "1.0.2"

# --------------------------------------------------------------------------
# Setup / config loading
# --------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "sync.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("valheim-sync")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error(
            "config.json not found. Copy config.example.json to config.json "
            "and fill in your values."
        )
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # expand %ENV% style Windows paths
    for key in ("valheim_worlds_folder", "valheim_log_path"):
        cfg[key] = os.path.expandvars(cfg[key])
    return cfg


CFG = load_config()


def get_player_name() -> str:
    """Resolve this player's display name without requiring everyone to
    hand-edit config.json. Priority:
      1. A previously-saved name in player_name.txt (next to this script/exe)
      2. A real (non-placeholder) name already set in config.json
      3. Ask once via a small popup, then remember it for next time
    This lets one person build/share a single config.json + exe with
    everyone, and each friend just gets asked their name the first time.
    """
    name_file = APP_DIR / "player_name.txt"
    if name_file.exists():
        saved = name_file.read_text(encoding="utf-8").strip()
        if saved:
            return saved

    configured = str(CFG.get("player_name", "")).strip()
    if configured and configured.lower() not in ("", "yournamehere", "change_me"):
        return configured

    # Ask once via a tiny popup (no console needed)
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    name = simpledialog.askstring(
        "Valheim Save-Sync", "What's your name/handle? (shown to friends)"
    )
    root.destroy()

    name = (name or "Player").strip() or "Player"
    name_file.write_text(name, encoding="utf-8")
    return name


PLAYER_NAME = get_player_name()

# --------------------------------------------------------------------------
# Update checking (GitHub Releases) -- entirely optional, never blocks or
# interrupts an actual game session. If github_repo isn't set in
# config.json, this is skipped silently.
# --------------------------------------------------------------------------

_last_update_check = 0.0
_UPDATE_CHECK_INTERVAL_SECONDS = 60 * 60  # once an hour -- GitHub's public
# API allows 60 unauthenticated requests/hour per IP, so this stays well
# under that even if several friends check independently.

_cached_update_message = None


def check_for_update() -> str | None:
    """
    Checks GitHub's public Releases API for the latest published version.
    Returns a short message to show in the tray tooltip if an update is
    available, or None if up to date / not configured / check failed.
    Throttled to once an hour -- returns the cached result in between.
    """
    global _last_update_check, _cached_update_message

    repo = CFG.get("github_repo")
    if not repo:
        return None  # update checking not configured, skip silently

    now = time.time()
    if now - _last_update_check < _UPDATE_CHECK_INTERVAL_SECONDS:
        return _cached_update_message

    _last_update_check = now
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            timeout=10,
            headers={"Accept": "application/vnd.github+json"},
        )
        r.raise_for_status()
        data = r.json()
        latest_tag = data.get("tag_name", "").lstrip("v")

        if latest_tag and latest_tag != APP_VERSION:
            release_url = data.get("html_url", f"https://github.com/{repo}/releases/latest")
            _cached_update_message = f"Update available: v{latest_tag} — {release_url}"
            log.info(
                "A new version is available: v%s (you're on v%s). Get it: %s",
                latest_tag,
                APP_VERSION,
                release_url,
            )
        else:
            _cached_update_message = None
    except requests.RequestException as e:
        log.warning("Could not check for updates (non-fatal): %s", e)
        # keep whatever the previous cached result was rather than
        # clearing it over a transient network blip

    return _cached_update_message


# --------------------------------------------------------------------------
# Coordinator (Cloudflare Worker) client
# --------------------------------------------------------------------------


def _headers():
    return {"X-Auth": CFG["worker_secret"], "Content-Type": "application/json"}


def get_status() -> dict:
    r = requests.get(f"{CFG['worker_url']}/status", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def claim_host() -> dict:
    r = requests.post(
        f"{CFG['worker_url']}/claim",
        headers=_headers(),
        json={"name": PLAYER_NAME},
        timeout=10,
    )
    return r.json()


def announce_join_code(join_code: str):
    r = requests.post(
        f"{CFG['worker_url']}/announce_code",
        headers=_headers(),
        json={"name": PLAYER_NAME, "join_code": join_code},
        timeout=10,
    )
    return r.json()


def notify_moonberry(host_name: str, join_code: str | None = None, event: str = "started"):
    """
    Tells Moonberry (a separate Cloudflare Worker/Discord bot) that
    hosting has started or ended, so it can post in Discord. This is
    entirely optional -- if moonberry_url isn't set in config.json, or
    the request fails for any reason, we just log it and move on. A
    Discord notification failing should never block or crash an actual
    game session.
    """
    moonberry_url = CFG.get("moonberry_url")
    moonberry_secret = CFG.get("moonberry_secret")
    if not moonberry_url or not moonberry_secret:
        return  # Moonberry integration not configured, skip silently

    try:
        r = requests.post(
            f"{moonberry_url}/notify/valheim",
            headers={"X-Auth": moonberry_secret, "Content-Type": "application/json"},
            json={"host_name": host_name, "join_code": join_code, "event": event},
            timeout=10,
        )
        if r.status_code == 200 and r.json().get("ok"):
            log.info("Notified Moonberry (Discord) - event: %s.", event)
        else:
            # The request reached Moonberry fine, but something failed on
            # ITS end (bad Discord token, wrong channel, etc) -- this is
            # exactly the kind of silent failure that used to be
            # invisible in sync.log before this check existed.
            log.warning(
                "Moonberry responded but did NOT confirm success (status %s): %s",
                r.status_code,
                r.text[:300],
            )
    except requests.RequestException as e:
        log.warning("Could not reach Moonberry (Discord notification skipped): %s", e)


def release_host(save_key: str | None = None) -> dict:
    r = requests.post(
        f"{CFG['worker_url']}/release",
        headers=_headers(),
        json={"name": PLAYER_NAME, "save_key": save_key},
        timeout=10,
    )
    return r.json()


JOIN_CODE_PATTERN = re.compile(r"with join code (\w{4,8}) is active", re.IGNORECASE)
# NOTE: Valheim generates a transitional "registered with join code X"
# line immediately followed by a DIFFERENT, actually-active code (the
# one shown in the pause menu). Matching on "is active" specifically
# avoids grabbing the wrong (superseded) code -- confirmed as a real
# mismatch with the old, more permissive pattern.


def try_scrape_join_code() -> str | None:
    """
    Watches Player.log for a Join Code for as long as Valheim is actually
    running -- no arbitrary timeout, since session start time varies a
    lot depending on world size (a small world might register a join
    code in 10 seconds, a huge one can take over a minute). This just
    keeps checking every few seconds until either:
      - it finds a match, or
      - Valheim's process exits (meaning the session ended before a
        code ever appeared -- at that point there's nothing left to wait
        for, so we give up gracefully).
    """
    log_path = Path(CFG["valheim_log_path"])

    while valheim_is_running():
        if log_path.exists():
            try:
                text = log_path.read_text(encoding="utf-8", errors="ignore")
                matches = JOIN_CODE_PATTERN.findall(text)
                if matches:
                    return matches[-1]  # most recent "is active" code
            except Exception:
                pass
        time.sleep(3)

    return None  # Valheim closed before a join code ever showed up





# --------------------------------------------------------------------------
# R2 (save file storage) client
# --------------------------------------------------------------------------


def r2_client():
    """
    Builds an S3-compatible client. Despite the function name (kept for
    minimal diff), this works with ANY S3-compatible provider -- R2,
    Backblaze B2, etc -- because the full endpoint URL now comes directly
    from config.json instead of being assembled from an R2-specific
    account ID. This means switching providers (or switching back) is
    just editing config.json -- no code changes needed either direction.
    """
    endpoint = CFG.get("storage_endpoint_url")
    if not endpoint:
        # Backward-compatible fallback for configs still using the old
        # R2-only field name.
        endpoint = f"https://{CFG['r2_account_id']}.r2.cloudflarestorage.com"

    access_key = CFG.get("storage_access_key_id", CFG.get("r2_access_key_id"))
    secret_key = CFG.get("storage_secret_access_key", CFG.get("r2_secret_access_key"))

    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        # Without explicit timeouts, a network hiccup can leave an
        # upload/download hanging far longer than reasonable for a
        # small save file -- this makes failures fail fast with a clear
        # error instead of silently sitting there indefinitely.
        config=BotoConfig(
            connect_timeout=15,
            read_timeout=120,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def storage_bucket_name() -> str:
    return CFG.get("storage_bucket_name", CFG.get("r2_bucket_name"))


def legacy_static_object_key() -> str:
    """The old single-file key from before versioned saves existed.
    Used only as a one-time fallback if the coordinator has no save_key
    recorded yet (e.g. right after upgrading to this version)."""
    return CFG.get("storage_object_key", CFG.get("r2_object_key", "world_save.zip"))


def new_save_key() -> str:
    """Generates a unique key for a fresh upload, so each session's save
    gets its own file instead of overwriting the same one every time."""
    return f"world_save_{int(time.time())}.zip"


def download_save(dest_zip: Path, key: str) -> bool:
    client = r2_client()
    try:
        client.download_file(storage_bucket_name(), key, str(dest_zip))
        return True
    except client.exceptions.ClientError as e:
        log.warning("Could not download save '%s' from cloud storage (%s)", key, e)
        return False


def upload_save(src_zip: Path, key: str):
    client = r2_client()
    client.upload_file(str(src_zip), storage_bucket_name(), key)


def prune_old_saves(keep: int = 5):
    """
    Keeps cloud storage from growing forever now that every session
    creates a new file instead of overwriting one. Keeps the most recent
    `keep` save files (by upload time) and deletes older ones. Never
    fatal -- a pruning failure shouldn't affect an actual game session,
    it just means storage grows a bit more before the next successful
    prune tidies it up.
    """
    try:
        client = r2_client()
        bucket = storage_bucket_name()
        paginator = client.get_paginator("list_objects_v2")
        all_saves = []
        for page in paginator.paginate(Bucket=bucket, Prefix="world_save_"):
            for obj in page.get("Contents", []):
                all_saves.append(obj["Key"])

        if len(all_saves) <= keep:
            return

        # Keys are named world_save_<unix_timestamp>.zip, so a plain
        # string sort works correctly for chronological order too.
        all_saves.sort(reverse=True)
        to_delete = all_saves[keep:]
        for key in to_delete:
            client.delete_object(Bucket=bucket, Key=key)
        log.info("Pruned %d old save version(s), kept the most recent %d.", len(to_delete), keep)
    except Exception:
        log.exception("Failed to prune old save versions (non-fatal, continuing).")


# --------------------------------------------------------------------------
# Local save file handling
# --------------------------------------------------------------------------


def get_world_target():
    """
    Valheim 1.0 changed the world save format entirely: pre-1.0, a world
    was two flat files (name.db + name.fwl). As of 1.0, it's a whole
    FOLDER named after the world, containing chunked data files,
    metadata, and integrity markers.

    This auto-detects which format is actually present on this machine,
    so the sync logic keeps working correctly regardless of whether
    someone has updated to 1.0 or not -- rather than hardcoding an
    assumption that could silently sync the wrong (or no) data.

    Returns ("folder", Path) for the 1.0+ format, or
            ("files", [Path, Path]) for the legacy pre-1.0 format.
    """
    folder = Path(CFG["valheim_worlds_folder"])
    name = CFG["valheim_world_name"]

    new_format_dir = folder / name
    if new_format_dir.is_dir():
        return "folder", new_format_dir

    return "files", [folder / f"{name}.db", folder / f"{name}.fwl"]


def backup_local_save():
    """Keep a timestamped copy of the current local save before overwriting,
    just in case something goes wrong with a cloud sync. Handles both the
    1.0+ folder format and the legacy flat-file format."""
    backup_dir = APP_DIR / "local_backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    kind, target = get_world_target()
    if kind == "folder":
        if target.exists():
            shutil.copytree(target, backup_dir / f"{target.name}_{stamp}")
    else:
        for f in target:
            if f.exists():
                shutil.copy2(f, backup_dir / f"{f.stem}_{stamp}{f.suffix}")
    log.info("Backed up local save (if present) to %s", backup_dir)


def zip_world(dest_zip: Path):
    """
    Zips whatever format is actually present. For the 1.0+ folder
    format, the world name is preserved as a path prefix inside the zip
    (e.g. "world/_main.1.db2") so that extracting it back into
    worlds_local correctly recreates the subfolder -- not just dumps
    loose chunk files into worlds_local directly.
    """
    kind, target = get_world_target()
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        if kind == "folder":
            for f in target.rglob("*"):
                if f.is_file():
                    arcname = str(Path(target.name) / f.relative_to(target))
                    zf.write(f, arcname=arcname)
        else:
            for f in target:
                if f.exists():
                    zf.write(f, arcname=f.name)


def unzip_world(src_zip: Path):
    """
    Extracts into worlds_local. Works correctly for both formats without
    needing to know which one is inside: zip_world() above always stores
    the right relative paths (either "world/..." for the folder format,
    or flat filenames for the legacy format), so a plain extractall()
    reconstructs whichever structure was actually zipped.
    """
    folder = Path(CFG["valheim_worlds_folder"])
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src_zip, "r") as zf:
        zf.extractall(folder)


SAVE_KEY_PATTERN = re.compile(r"^world_save_\d+\.zip$")


def read_local_save_key() -> str | None:
    """
    Returns the save key this machine last successfully synced to, or
    None if unknown/unrecognized. Deliberately strict: if the file
    contains anything that doesn't look like a real save key (e.g. a
    bare number from the old integer-based version scheme, or a stray
    manual edit), this returns None rather than guessing -- which
    forces a fresh download from the coordinator's authoritative
    save_key instead of trusting a possibly-stale local record. This is
    what makes the new scheme self-healing against exactly the kind of
    manual-edit drift the old version counter was vulnerable to.
    """
    p = APP_DIR / CFG["local_version_file"]
    if not p.exists():
        return None
    content = p.read_text().strip()
    if SAVE_KEY_PATTERN.match(content):
        return content
    return None  # unrecognized/old format -- treat as unknown, resync


def write_local_save_key(key: str):
    (APP_DIR / CFG["local_version_file"]).write_text(key)


# --------------------------------------------------------------------------
# Valheim process control
# --------------------------------------------------------------------------


def valheim_is_running() -> bool:
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] and proc.info["name"].lower() == "valheim.exe":
            return True
    return False


def launch_valheim():
    log.info("Launching Valheim...")
    os.startfile(CFG["valheim_launch_uri"])  # noqa: S606 (Windows-only, intentional)


def wait_for_valheim_start(timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        if valheim_is_running():
            return True
        time.sleep(2)
    return False


def wait_for_valheim_exit():
    log.info("Watching Valheim process. Waiting for it to close...")
    while valheim_is_running():
        time.sleep(5)
    log.info("Valheim has closed.")


# --------------------------------------------------------------------------
# Main state machine
# --------------------------------------------------------------------------

STATE_IDLE = "idle"
STATE_WAITING = "waiting_for_other_host"
STATE_HOSTING = "hosting"

# Set by the tray menu's "Play Now" item. main_loop only attempts to
# become host when this is set -- otherwise it just reports status.
# Without this, the app would auto-relaunch Valheim every poll cycle
# forever after you finish a session, since "nobody's hosting" is true
# right up until someone (usually you again) claims it.
import threading
PLAY_REQUESTED = threading.Event()
# Auto-launch on the very first run only, so opening the app for the
# first time still just starts playing with no extra click. Every
# session after that requires an explicit "Play Now" click from the
# tray menu -- this is what prevents the app from auto-relaunching
# Valheim in a loop after you finish a session.
PLAY_REQUESTED.set()


def sync_down_if_needed(cloud_save_key: str | None):
    local_save_key = read_local_save_key()

    # Fallback for the transition period right after upgrading to this
    # versioned-save scheme: if the coordinator doesn't have a save_key
    # yet (nobody's uploaded under the new scheme yet), fall back to the
    # old static filename so existing saves aren't stranded.
    effective_cloud_key = cloud_save_key or legacy_static_object_key()

    if not cloud_save_key:
        log.info(
            "Coordinator has no versioned save_key yet -- falling back to "
            "legacy filename '%s' for this sync.",
            effective_cloud_key,
        )

    if effective_cloud_key != local_save_key:
        log.info(
            "Cloud save ('%s') differs from local record ('%s'). Downloading...",
            effective_cloud_key,
            local_save_key,
        )
        backup_local_save()
        tmp_zip = APP_DIR / "_incoming_save.zip"
        if download_save(tmp_zip, effective_cloud_key):
            unzip_world(tmp_zip)
            tmp_zip.unlink(missing_ok=True)
            write_local_save_key(effective_cloud_key)
            log.info("Local save updated to '%s'.", effective_cloud_key)
    else:
        log.info("Local save is already up to date ('%s').", local_save_key)


def become_host_and_play():
    log.info("No one is hosting. Attempting to claim host...")
    result = claim_host()
    if not result.get("ok"):
        log.info("Someone beat us to it: %s", result.get("current"))
        return

    current = result["current"]

    # CRITICAL: everything from here on is wrapped in try/finally.
    # If ANYTHING goes wrong (network blip, SSL error, missing file,
    # anything) we MUST still release the host claim in the finally
    # block below. Without this, a crash here leaves the coordinator
    # stuck saying "X is hosting" forever, locking everyone out with
    # no way to recover except manually resetting it.
    uploaded_successfully = False
    try:
        sync_down_if_needed(current.get("save_key"))

        launch_valheim()
        if not wait_for_valheim_start():
            log.warning("Valheim didn't seem to start within the timeout.")
            return  # falls through to finally, which releases the claim

        log.info(
            "You're hosting as '%s'. Friends can join you via Steam. "
            "This app will auto-sync the save when you close the game.",
            PLAYER_NAME,
        )

        # Try to find and share the Join Code, which lets friends connect
        # without needing you to appear "online" on Steam. This checks
        # continuously for as long as Valheim is running -- no fixed
        # timeout, since world load time varies a lot by world size.
        log.info("Watching for a Valheim Join Code to share (checking Player.log)...")
        join_code = try_scrape_join_code()

        if join_code:
            announce_join_code(join_code)
            log.info("Shared join code '%s' with the group.", join_code)
        else:
            log.info(
                "Session ended before a join code was ever found in the log "
                "(that's fine, Steam invite still works for joining)."
            )

        notify_moonberry(PLAYER_NAME, join_code)

        wait_for_valheim_exit()

        out_zip = APP_DIR / "_outgoing_save.zip"

        zip_start = time.time()
        log.info("Zipping save...")
        zip_world(out_zip)
        zip_seconds = time.time() - zip_start
        zip_size_mb = out_zip.stat().st_size / (1024 * 1024)
        log.info("Zip complete: %.1f MB in %.1fs.", zip_size_mb, zip_seconds)

        uploaded_key = new_save_key()
        upload_start = time.time()
        log.info("Uploading save as '%s'...", uploaded_key)
        upload_save(out_zip, uploaded_key)
        upload_seconds = time.time() - upload_start
        log.info("Upload complete in %.1fs.", upload_seconds)
        out_zip.unlink(missing_ok=True)
        uploaded_successfully = True

    except Exception:
        # log.exception() includes the full traceback in sync.log,
        # so future issues are visible there instead of only in a
        # console window that might get missed.
        log.exception(
            "Something went wrong while hosting. Releasing the host "
            "claim so no one else gets locked out."
        )

    finally:
        if uploaded_successfully:
            release_result = release_host(save_key=uploaded_key)
            write_local_save_key(uploaded_key)
            log.info(
                "Save uploaded ('%s') and host slot released. Thanks for playing!",
                uploaded_key,
            )
            prune_old_saves(keep=CFG.get("max_saved_versions", 5))
            notify_moonberry(PLAYER_NAME, event="ended")
        else:
            release_host()
            log.info("Host slot released (no upload happened this time).")


def main_loop(update_tray_text=None, notify=None):
    last_notified_host = None  # tracks who we've already notified about,
    # so we only pop a notification ONCE per session start, not every
    # 30-second poll while that person keeps hosting.
    is_first_check = True  # the auto-play-on-open behavior should only
    # ever apply to this very first check -- if someone else is already
    # hosting right now, that intent gets discarded rather than sitting
    # around waiting to fire the instant they stop (which would look
    # exactly like an unwanted, unrequested auto-launch).

    while True:
        try:
            status = get_status()

            if status.get("hosting") and status.get("host_name") == PLAYER_NAME and not valheim_is_running():
                # This is OUR OWN claim, but Valheim isn't actually running
                # on this machine. That means a previous run of this app
                # crashed, was force-closed, or the PC slept/lost power
                # before it could release the claim in its finally block.
                # Safe to auto-clear: if it were genuinely still hosting,
                # valheim_is_running() would be True.
                log.warning(
                    "Found a stale host claim from a previous run (no Valheim "
                    "process is actually running). Auto-releasing it."
                )
                release_host()
                last_notified_host = None
                # loop back around immediately to re-check status fresh
                continue

            if status.get("hosting"):
                if is_first_check and PLAY_REQUESTED.is_set():
                    # Someone else is already hosting on our very first
                    # check -- discard the auto-play intent instead of
                    # letting it linger until they eventually stop.
                    PLAY_REQUESTED.clear()
                    log.info(
                        "Someone else is already hosting -- won't auto-play "
                        "later when they stop. Use 'Play Now' if you want to "
                        "host after them."
                    )

                host_name = status.get("host_name")
                join_code = status.get("join_code")
                code_part = f" (Join Code: {join_code})" if join_code else ""
                msg = f"{host_name} is hosting — join via Steam{code_part}"
                log.info(msg)
                if update_tray_text:
                    update_tray_text(msg)

                # Fire a one-time desktop notification when someone NEW
                # starts hosting -- but never for ourselves, since we
                # already know we just started (we're the one who clicked
                # Play Now).
                if host_name != last_notified_host and host_name != PLAYER_NAME and notify:
                    notify(
                        "Valheim Save-Sync",
                        f"{host_name} started hosting — open Steam to join!{code_part}",
                    )
                last_notified_host = host_name
            elif PLAY_REQUESTED.is_set():
                last_notified_host = None  # reset so the next host triggers a fresh notification
                PLAY_REQUESTED.clear()
                if update_tray_text:
                    update_tray_text("No host — claiming and starting Valheim...")
                become_host_and_play()
                if update_tray_text:
                    update_tray_text(
                        "Session ended. Right-click tray icon → Play Now to host again."
                    )
            else:
                last_notified_host = None
                log.info("No one hosting. Waiting for 'Play Now' to be clicked.")
                update_msg = check_for_update()
                if update_tray_text:
                    if update_msg:
                        update_tray_text(f"Idle — Play Now to host | {update_msg}")
                    else:
                        update_tray_text("Idle — right-click tray icon → Play Now to host")

            is_first_check = False
        except requests.RequestException as e:
            log.error("Coordinator unreachable: %s", e)
            if update_tray_text:
                update_tray_text("Coordinator unreachable — retrying...")

        time.sleep(CFG["poll_interval_seconds"])


# --------------------------------------------------------------------------
# System tray wrapper
# --------------------------------------------------------------------------


def run_with_tray():
    import threading

    import pystray
    from PIL import Image, ImageDraw

    def make_icon_image():
        img = Image.new("RGB", (64, 64), "white")
        d = ImageDraw.Draw(img)
        d.ellipse((8, 8, 56, 56), fill="green")
        return img

    icon = pystray.Icon("valheim-sync", make_icon_image(), "Valheim Save-Sync: starting...")

    def update_text(text):
        icon.title = f"Valheim Save-Sync: {text}"

    def on_play_now(icon, item):
        PLAY_REQUESTED.set()
        update_text("Play requested — will start shortly...")

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    icon.menu = pystray.Menu(
        pystray.MenuItem("Play Now", on_play_now),
        pystray.MenuItem("Quit", on_quit),
    )

    def notify(title, message):
        try:
            icon.notify(message, title)
        except Exception:
            # Notifications aren't supported on every platform/desktop
            # environment -- fail quietly rather than crashing the app
            # over something non-essential.
            log.warning("Could not show desktop notification (unsupported on this system).")

    t = threading.Thread(target=main_loop, args=(update_text, notify), daemon=True)
    t.start()
    icon.run()


if __name__ == "__main__":
    if "--no-tray" in sys.argv:
        main_loop()
    else:
        run_with_tray()
