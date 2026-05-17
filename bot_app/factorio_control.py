import asyncio
import datetime
import json
import os
from typing import Optional

import aiohttp
import discord

from bot_app.core import BASE_DIR, logger


FACTORIO_ACCESS_FILE = os.path.join(BASE_DIR, "factorio_access_list.json")
FACTORIO_CONTROL_API_URL = str(os.getenv("FACTORIO_CONTROL_API_URL") or "").strip().rstrip("/")
FACTORIO_CONTROL_API_TOKEN = str(os.getenv("FACTORIO_CONTROL_API_TOKEN") or "").strip()


def _read_timeout_seconds() -> int:
    raw_value = str(os.getenv("FACTORIO_CONTROL_TIMEOUT_SECONDS") or "").strip()
    if not raw_value:
        return 15

    try:
        return max(1, int(raw_value))
    except ValueError:
        logger.warning(
            "Invalid FACTORIO_CONTROL_TIMEOUT_SECONDS=%r. Using default=15.",
            raw_value,
        )
        return 15


FACTORIO_CONTROL_TIMEOUT_SECONDS = _read_timeout_seconds()

_factorio_access_lock = asyncio.Lock()
_factorio_access_loaded = False
_factorio_access_entries: dict[int, dict] = {}


def factorio_api_configured() -> bool:
    return bool(FACTORIO_CONTROL_API_URL and FACTORIO_CONTROL_API_TOKEN)


def factorio_api_configuration_error() -> str:
    if factorio_api_configured():
        return ""
    return "A Factorio control API nincs beállítva a bot környezetében."


def _load_factorio_access_entries_from_disk() -> dict[int, dict]:
    if not os.path.exists(FACTORIO_ACCESS_FILE):
        return {}

    try:
        with open(FACTORIO_ACCESS_FILE, "r", encoding="utf-8") as access_file:
            raw_entries = json.load(access_file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load factorio access list: %s", exc)
        return {}

    if not isinstance(raw_entries, list):
        logger.warning("Ignoring invalid factorio access list payload: expected list.")
        return {}

    entries: dict[int, dict] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            continue

        try:
            user_id = int(raw_entry.get("user_id"))
        except (TypeError, ValueError):
            continue

        if user_id <= 0:
            continue

        entries[user_id] = {
            "user_id": user_id,
            "tag": str(raw_entry.get("tag") or "").strip(),
            "display_name": str(raw_entry.get("display_name") or "").strip(),
            "added_at": str(raw_entry.get("added_at") or "").strip(),
            "added_by_id": str(raw_entry.get("added_by_id") or "").strip(),
            "added_by_tag": str(raw_entry.get("added_by_tag") or "").strip(),
        }

    return entries


def _save_factorio_access_entries_to_disk(entries: dict[int, dict]) -> None:
    payload = sorted(entries.values(), key=lambda item: (item.get("display_name") or item.get("tag") or "").casefold())
    with open(FACTORIO_ACCESS_FILE, "w", encoding="utf-8") as access_file:
        json.dump(payload, access_file, indent=2, ensure_ascii=False)


async def _ensure_factorio_access_loaded_locked() -> None:
    global _factorio_access_loaded
    global _factorio_access_entries

    if _factorio_access_loaded:
        return

    _factorio_access_entries = _load_factorio_access_entries_from_disk()
    _factorio_access_loaded = True


async def is_factorio_user_authorized(user_id: int) -> bool:
    async with _factorio_access_lock:
        await _ensure_factorio_access_loaded_locked()
        return int(user_id) in _factorio_access_entries


async def list_factorio_access_entries() -> list[dict]:
    async with _factorio_access_lock:
        await _ensure_factorio_access_loaded_locked()
        return sorted(
            [dict(entry) for entry in _factorio_access_entries.values()],
            key=lambda item: (item.get("display_name") or item.get("tag") or "").casefold(),
        )


async def add_factorio_access_member(member: discord.Member, added_by: discord.Member) -> tuple[bool, dict]:
    async with _factorio_access_lock:
        await _ensure_factorio_access_loaded_locked()

        existing_entry = _factorio_access_entries.get(member.id)
        changed = existing_entry is None
        added_at = (
            existing_entry.get("added_at")
            if existing_entry and str(existing_entry.get("added_at") or "").strip()
            else datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        )

        entry = {
            "user_id": member.id,
            "tag": str(member),
            "display_name": member.display_name,
            "added_at": added_at,
            "added_by_id": str(added_by.id),
            "added_by_tag": str(added_by),
        }
        _factorio_access_entries[member.id] = entry
        _save_factorio_access_entries_to_disk(_factorio_access_entries)
        return changed, dict(entry)


async def remove_factorio_access_member(member_id: int) -> Optional[dict]:
    async with _factorio_access_lock:
        await _ensure_factorio_access_loaded_locked()
        removed_entry = _factorio_access_entries.pop(int(member_id), None)
        if removed_entry is None:
            return None

        _save_factorio_access_entries_to_disk(_factorio_access_entries)
        return dict(removed_entry)


async def call_factorio_control_api(action: str) -> dict:
    if not factorio_api_configured():
        raise RuntimeError(factorio_api_configuration_error())

    action_key = str(action or "").strip().lower()
    if action_key == "status":
        method = "GET"
        endpoint = "/api/v1/factorio/status"
    elif action_key == "on":
        method = "POST"
        endpoint = "/api/v1/factorio/start"
    elif action_key == "off":
        method = "POST"
        endpoint = "/api/v1/factorio/stop"
    else:
        raise RuntimeError(f"Unsupported factorio action: {action}")

    url = f"{FACTORIO_CONTROL_API_URL}{endpoint}"
    timeout = aiohttp.ClientTimeout(total=FACTORIO_CONTROL_TIMEOUT_SECONDS)
    headers = {"Authorization": f"Bearer {FACTORIO_CONTROL_API_TOKEN}"}

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, headers=headers) as response:
                response_text = await response.text()
    except Exception as exc:
        raise RuntimeError(f"Nem sikerült elérni a Factorio control API-t: {exc}") from exc

    payload: dict
    try:
        decoded_payload = json.loads(response_text) if response_text else {}
    except json.JSONDecodeError:
        decoded_payload = {}

    if not isinstance(decoded_payload, dict):
        decoded_payload = {}

    if response.status >= 400:
        error_message = (
            str(decoded_payload.get("error") or "").strip()
            or str(decoded_payload.get("message") or "").strip()
            or response_text.strip()
            or f"HTTP {response.status}"
        )
        raise RuntimeError(error_message)

    return decoded_payload


def extract_factorio_container_payload(payload: dict) -> dict:
    container_payload = payload.get("container")
    if isinstance(container_payload, dict):
        return container_payload
    return payload


def format_factorio_status_message(payload: dict) -> str:
    container_payload = extract_factorio_container_payload(payload)
    target = str(container_payload.get("target") or "factorio-server")
    status = str(container_payload.get("status") or "unknown")
    running = bool(container_payload.get("running"))
    started_at = str(container_payload.get("started_at") or "").strip()
    finished_at = str(container_payload.get("finished_at") or "").strip()
    compose_project = str(container_payload.get("compose_project") or "").strip()
    compose_service = str(container_payload.get("compose_service") or "").strip()

    lines = [
        f"Konténer: `{target}`",
        f"Állapot: `{status}`",
        f"Fut: {'igen' if running else 'nem'}",
    ]

    if compose_project or compose_service:
        compose_label = compose_project or "-"
        service_label = compose_service or "-"
        lines.append(f"Compose: `{compose_label}` / `{service_label}`")

    if started_at:
        lines.append(f"Started: `{started_at}`")
    if finished_at and finished_at != "0001-01-01T00:00:00Z":
        lines.append(f"Finished: `{finished_at}`")

    action = str(payload.get("action") or "").strip()
    message = str(payload.get("message") or "").strip()
    changed = payload.get("changed")
    if action:
        lines.insert(0, f"Művelet: `{action}`")
    if message:
        prefix = "Változás" if changed is True else "Info"
        lines.insert(0, f"{prefix}: {message}")

    return "\n".join(lines)
