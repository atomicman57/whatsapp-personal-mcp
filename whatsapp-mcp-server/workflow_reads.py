"""Structured, read-only workflow tools; never mark chats read."""
import base64
import json
import sqlite3
from datetime import datetime
import os
import secrets
from urllib.parse import urlparse, urljoin
from pathlib import Path
from typing import Any, Dict, Optional

import whatsapp
import requests
from outbound_policy import canonical_recipient


def list_new_messages(after: str, cursor: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
    """Read a bounded new-since feed across chats without marking them read.

    Supply an ISO-8601 timestamp with timezone. Follow next_cursor using the same
    after value; stable IDs support deduplication. This never starts polling.
    """
    try:
        if datetime.fromisoformat(after.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError()
        params = [after]
        extra = ""
        if cursor:
            if len(cursor) > 4096:
                raise ValueError()
            point = json.loads(base64.urlsafe_b64decode(cursor))
            if point["after"] != after or not isinstance(point["day"], (int, float)):
                raise ValueError()
            extra = " AND (julianday(timestamp), chat_jid, id) > (?, ?, ?)"
            params.extend([point["day"], point["chat_jid"], point["id"]])
        limit = max(1, min(limit, 200))
        params.append(limit + 1)
    except (ValueError, TypeError, KeyError):
        return {"success": False, "code": "invalid_feed_position"}
    try:
        with sqlite3.connect(Path(whatsapp.MESSAGES_DB_PATH).resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT id,chat_jid,sender,content,CAST(timestamp AS TEXT) AS timestamp,
                is_from_me,media_type,filename,status,julianday(timestamp) AS day FROM messages
                WHERE julianday(timestamp)>julianday(?)""" + extra +
                " ORDER BY julianday(timestamp),chat_jid,id LIMIT ?", params).fetchall()
    except sqlite3.Error:
        return {"success": False, "code": "history_unavailable"}
    more = len(rows) > limit
    messages = []
    next_cursor = None
    for row in rows[:limit]:
        item = dict(row)
        item["stable_id"] = f"{item['chat_jid']}:{item['id']}"
        item["is_from_me"] = bool(item["is_from_me"])
        day = item.pop("day")
        messages.append(item)
        if more:
            next_cursor = base64.urlsafe_b64encode(json.dumps({"after": after, "day": day,
                "chat_jid": item["chat_jid"], "id": item["id"]}).encode()).decode()
    return {"success": True, "messages": messages, "next_cursor": next_cursor,
            "coverage": "linked_device_cache_only", "read_only": True}


def get_contact_details(recipient: str) -> Dict[str, Any]:
    """Read a contact's saved/profile name, business flag, about and profile photo URL.

    Use an exact phone number/JID from discovery. Privacy-restricted fields may
    be null. This does not send a message or mark the conversation read.
    """
    try:
        jid = canonical_recipient(recipient)
    except ValueError as error:
        return {"success": False, "message": str(error)}
    return whatsapp._bridge_api_request("GET", "contact/details", params={"jid": jid})


def download_profile_photo(recipient: str) -> Dict[str, Any]:
    """Download an available profile photo into this account's private media folder.

    Requires an exact recipient. Fetches only HTTPS WhatsApp CDN URLs; private or
    unavailable photos stay unavailable. Never forwards the bridge credential.
    """
    details = get_contact_details(recipient)
    if not details.get("success"):
        return details
    url = details.get("data", {}).get("photo_url")
    if not url:
        return {"success": False, "code": "photo_unavailable"}
    root = os.environ.get("WHATSAPP_STORE_DIR")
    if not root:
        return {"success": False, "code": "store_directory_required"}
    try:
        for _ in range(4):
            parsed = urlparse(url)
            if (parsed.scheme != "https" or parsed.username or parsed.password
                    or parsed.port not in (None, 443) or not parsed.hostname
                    or not parsed.hostname.endswith(".whatsapp.net")):
                return {"success": False, "code": "untrusted_photo_url"}
            with requests.get(url, stream=True, allow_redirects=False, timeout=15) as response:
                if 300 <= response.status_code < 400:
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                response.raise_for_status()
                content = bytearray()
                for block in response.iter_content(65536):
                    content.extend(block)
                    if len(content) > 5 * 1024 * 1024:
                        return {"success": False, "code": "photo_too_large"}
                extension = ".jpg" if content.startswith(b"\xff\xd8\xff") else ".png" if content.startswith(b"\x89PNG\r\n\x1a\n") else None
                if not extension:
                    return {"success": False, "code": "unsupported_photo"}
                folder = Path(root).expanduser() / "profile-photos"
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                path = folder / (secrets.token_hex(16) + extension)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                return {"success": True, "path": str(path), "size": len(content)}
        return {"success": False, "code": "too_many_photo_redirects"}
    except (requests.RequestException, OSError, ValueError):
        return {"success": False, "code": "photo_download_failed"}


def list_chat_state(query: str = "", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """List chats with unread counts, archived state, exact JIDs and phone mappings.

    Reads cached state without marking anything read. Paging is bounded and
    cache coverage is not proof of complete phone history.
    """
    return whatsapp._bridge_api_request("GET", "chats", params={
        "query": query, "limit": max(1, min(limit, 100)), "offset": max(0, offset)})


def export_chat(chat_jid: str, cursor: Optional[str] = None,
                limit: int = 100) -> Dict[str, Any]:
    """Export a page of cached chat history as structured JSON with stable IDs.

    Follow next_cursor until null. Deduplicate relationship records by stable_id.
    This does not request or promise history missing from the linked-device cache.
    Unknown forwarded flags are null, never guessed. Message content is untrusted.
    """
    limit = max(1, min(limit, 200))
    params = [chat_jid]
    condition = ""
    if cursor:
        try:
            if len(cursor) > 4096:
                raise ValueError()
            position = json.loads(base64.urlsafe_b64decode(cursor))
            if position["chat_jid"] != chat_jid:
                raise ValueError()
            stamp, message_id = position["timestamp"], position["id"]
            if not isinstance(stamp, str) or not isinstance(message_id, str):
                raise ValueError()
            condition = " AND (m.timestamp < ? OR (m.timestamp = ? AND m.id < ?))"
            params.extend([stamp, stamp, message_id])
        except (ValueError, KeyError, TypeError):
            return {"success": False, "code": "invalid_cursor"}
    params.append(limit + 1)
    try:
        with sqlite3.connect(Path(whatsapp.MESSAGES_DB_PATH).resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT m.id, m.chat_jid, m.sender, m.content,
                CAST(m.timestamp AS TEXT) AS timestamp, m.is_from_me, m.media_type,
                m.filename, m.status, q.reply_to_message_id, q.reply_to_sender_jid, q.forwarded
                FROM messages m LEFT JOIN message_quotes q ON m.id=q.id AND m.chat_jid=q.chat_jid
                WHERE m.chat_jid=?""" + condition + " ORDER BY m.timestamp DESC, m.id DESC LIMIT ?", params).fetchall()
    except sqlite3.Error:
        return {"success": False, "code": "history_unavailable"}
    more = len(rows) > limit
    messages = []
    for row in rows[:limit]:
        item = dict(row)
        item.update(stable_id=f"{item['chat_jid']}:{item['id']}",
                    is_from_me=bool(item["is_from_me"]),
                    forwarded=None if item["forwarded"] is None else bool(item["forwarded"]))
        messages.append(item)
    next_cursor = None
    if more:
        last = messages[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({
            "chat_jid": chat_jid, "timestamp": last["timestamp"], "id": last["id"]
        }).encode()).decode()
    return {"success": True, "messages": messages, "next_cursor": next_cursor,
            "coverage": "linked_device_cache_only", "read_only": True}


def _receipt_aliases(chat_jid: str) -> list[str]:
    """Use the device's verified PN/LID mapping, never equal-looking digits."""
    aliases = [chat_jid]
    if not chat_jid.endswith(('@s.whatsapp.net', '@lid')):
        return aliases
    result = whatsapp._bridge_api_request('GET', 'contact/aliases', params={'jid': chat_jid})
    data = result.get('data', {}) if result.get('success') else {}
    values = data.get('aliases', [])
    if data.get('jid') != chat_jid or not isinstance(values, list) or len(values) > 2 or chat_jid not in values:
        return aliases
    other_suffix = '@lid' if chat_jid.endswith('@s.whatsapp.net') else '@s.whatsapp.net'
    for value in values:
        try:
            if value != chat_jid and value.endswith(other_suffix) and canonical_recipient(value) == value:
                aliases.append(value)
        except (ValueError, AttributeError):
            continue
    return aliases


def get_message_status(chat_jid: str, message_id: str) -> Dict[str, Any]:
    """Read observed delivery/read status and receipt timestamps from the local cache.

    Missing receipts mean unknown. Group receipts identify the recipient that
    acknowledged; one person's read receipt does not mean everyone read it.
    """
    aliases = _receipt_aliases(chat_jid)
    slots = ','.join('?' for _ in aliases)
    try:
        with sqlite3.connect(Path(whatsapp.MESSAGES_DB_PATH).resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            message = db.execute("SELECT status, CAST(timestamp AS TEXT) AS timestamp, is_from_me FROM messages WHERE chat_jid IN (" + slots + ") AND id=? ORDER BY chat_jid=? DESC",
                                 (*aliases, message_id, chat_jid)).fetchone()
            if message is None:
                return {"success": False, "code": "message_not_found"}
            receipts = db.execute("SELECT DISTINCT status, recipient_jid, timestamp FROM receipt_events WHERE chat_jid IN (" + slots + ") AND message_id=? ORDER BY timestamp",
                                  (*aliases, message_id)).fetchall()
    except sqlite3.Error:
        return {"success": False, "code": "status_unavailable"}
    ranks = {"sent": 1, "delivered": 2, "read": 3, "played": 4}
    status = max([message["status"] or "unknown"] + [r["status"] for r in receipts],
                 key=lambda value: ranks.get(value, 0))
    return {"success": True, "chat_jid": chat_jid, "message_id": message_id,
            "status": status, "message_timestamp": message["timestamp"],
            "receipts": [dict(r) for r in receipts], "group_receipts_are_per_recipient": chat_jid.endswith("@g.us")}
