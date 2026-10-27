"""Automatic MCP send policy. No per-message approval or confirmation tokens.

This protects calls through this MCP process, not direct access to the upstream
loopback bridge by another local program. A request ID is an at-most-once key:
an uncertain operation is never automatically retried, including after a crash.
"""
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict


def canonical_recipient(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"\+?[1-9][0-9]{5,14}", value):
        return value.lstrip("+") + "@s.whatsapp.net"
    if re.fullmatch(r"[1-9][0-9]{5,14}@s\.whatsapp\.net", value):
        return value
    if re.fullmatch(r"[0-9]{6,20}@lid|[0-9]{6,20}(?:-[0-9]{6,12})?@g\.us", value):
        return value
    raise ValueError("Use an exact international phone number or chat JID, never a name.")


def configured_recipients(name: str) -> set[str]:
    return {canonical_recipient(v) for v in os.environ.get(name, "").split(",") if v.strip()}


def perform_send(kind: str, recipient: str, request_id: str, payload: Dict[str, Any],
                 execute: Callable[[str], Dict[str, Any]]) -> Dict[str, Any]:
    """Send once when configured access permits it; repeat IDs return stored results."""
    if os.environ.get("WHATSAPP_SEND_ENABLED", "").lower() != "true":
        return {"success": False, "code": "sending_disabled"}
    try:
        target = canonical_recipient(recipient)
        blocked = configured_recipients("WHATSAPP_DO_NOT_CONTACT")
        allowed = configured_recipients("WHATSAPP_SEND_ALLOWLIST")
    except ValueError as error:
        return {"success": False, "code": "invalid_recipient_policy", "message": str(error)}
    if target in blocked:
        return {"success": False, "code": "recipient_denied"}
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", request_id):
        return {"success": False, "code": "invalid_request_id"}
    directory_policy = (os.environ.get("WHATSAPP_SEND_SAVED_CONTACTS", "").lower()=="true"
                        or bool(os.environ.get("WHATSAPP_DO_NOT_CONTACT_NAMES", "")))
    if directory_policy:
        # The bridge owns verified aliases and saved-contact metadata. Check it
        # before reserving an outbound ID, then enforce again at dispatch time.
        import whatsapp
        policy = whatsapp._bridge_api_request("GET", "recipient/policy", params={"recipient":target}, timeout=10)
        if not policy.get("success"):
            return {"success": False, "code": "recipient_policy_unavailable"}
        if policy.get("data",{}).get("allowed") is not True:
            return {"success": False, "code": "recipient_denied"}
    elif allowed and target not in allowed:
        return {"success": False, "code": "recipient_denied"}
    # A phone-only deny list cannot safely decide whether a LID or group contains
    # the same person. Require an explicit allowlist decision for those addresses.
    if not directory_policy and blocked and not target.endswith("@s.whatsapp.net") and target not in allowed:
        return {"success": False, "code": "recipient_identity_unverified"}
    digest = hashlib.sha256(json.dumps(
        {"kind": kind, "recipient": target, "payload": payload},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    # The directory is deployment configuration, not an argument exposed to the agent.
    root = os.environ.get("WHATSAPP_STORE_DIR")
    if not root:
        return {"success": False, "code": "store_directory_required"}
    directory = Path(root).expanduser()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = directory / "outbound-journal.sqlite"
    if database.is_symlink():
        return {"success": False, "code": "invalid_journal"}
    fd = os.open(database, os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    os.chmod(database, 0o600)
    with sqlite3.connect(database, timeout=10) as connection:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("""CREATE TABLE IF NOT EXISTS outbound_requests (
            request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT digest, result FROM outbound_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if previous:
            if previous[0] != digest:
                return {"success": False, "code": "request_id_conflict"}
            if previous[1] is not None:
                return {**json.loads(previous[1]), "replayed": True}
            return {"success": False, "code": "outcome_unknown", "retry_same_id_only": True}
        connection.execute("INSERT INTO outbound_requests(request_id, digest) VALUES (?, ?)",
                           (request_id, digest))
        connection.commit()  # durable reservation before any network operation
        try:
            result = execute(target)
        except Exception:
            # Do not log exceptions that may contain message text, phone numbers or paths.
            return {"success": False, "code": "outcome_unknown", "retry_same_id_only": True}
        if not result.get("success"):
            return {"success": False, "code": "outcome_unknown", "retry_same_id_only": True}
        # Retain only protocol status and identifiers, never content or attachment bytes.
        stored = {key: result[key] for key in
                  ("success", "message_id", "reply_to_message_id", "status") if key in result}
        stored["request_id"] = request_id
        connection.execute("UPDATE outbound_requests SET result=? WHERE request_id=?",
                           (json.dumps(stored), request_id))
        connection.commit()
        return stored
