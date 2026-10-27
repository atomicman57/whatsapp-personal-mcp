"""Direct sends with automatic policy and persistent at-most-once request IDs."""
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

import audio
from outbound_policy import perform_send
from whatsapp import _bridge_api_request, MEDIA_REQUEST_TIMEOUT_SECONDS


def send_message(recipient: str, message: str, request_id: str) -> Dict[str, Any]:
    """Send text immediately when send access is enabled; no confirmation step.

    Use an exact phone number/JID and a unique request_id for each intended send.
    Reuse the SAME ID and content on retries, even after a timeout or restart.
    Never replace an uncertain request's ID to force another send.
    """
    if not message.strip():
        return {"success": False, "code": "message_required"}
    return perform_send("text", recipient, request_id, {"message": message},
                        lambda target: _bridge_api_request("POST", "send", payload={
                            "recipient": target, "message": message}))


def send_file(recipient: str, media_path: str, request_id: str,
              caption: str = "") -> Dict[str, Any]:
    """Send an attachment from WHATSAPP_STORE_DIR/outbox, optionally with a caption.

    Sends immediately when configured. Copy the file into outbox first; arbitrary
    filesystem paths and symlinks escaping outbox are refused. Reuse request_id
    for retries. The exact attachment bytes are frozen for this operation.
    """
    return _send_media("file", recipient, media_path, request_id, caption)


def send_audio_message(recipient: str, media_path: str, request_id: str) -> Dict[str, Any]:
    """Send an outbox audio file as a voice note without per-message approval.

    Requires configured send access and a stable request_id. Non-Ogg audio uses
    the existing local FFmpeg conversion. Never change IDs to retry uncertainty.
    """
    return _send_media("audio", recipient, media_path, request_id, "")


def _send_media(kind: str, recipient: str, media_path: str, request_id: str,
                caption: str) -> Dict[str, Any]:
    root = os.environ.get("WHATSAPP_STORE_DIR")
    if not root:
        return {"success": False, "code": "store_directory_required"}
    outbox = Path(root).expanduser().resolve() / "outbox"
    source = Path(media_path).expanduser().resolve()
    extensions = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".pdf",
                  ".ogg", ".mp3", ".m4a", ".wav"}
    audio_extensions = {".ogg", ".mp3", ".m4a", ".wav"}
    if (not source.is_relative_to(outbox) or not source.is_file()
            or source.suffix.lower() not in (audio_extensions if kind == "audio" else extensions)):
        return {"success": False, "code": "invalid_outbox_file"}
    with source.open("rb") as stream:
        content = stream.read(16 * 1024 * 1024 + 1)
    if not content or len(content) > 16 * 1024 * 1024:
        return {"success": False, "code": "invalid_media_size"}
    digest = hashlib.sha256(content).hexdigest()

    def execute(target: str) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="outbound-", dir=root) as temporary:
            frozen = Path(temporary) / ("attachment" + source.suffix.lower())
            frozen.write_bytes(content)
            os.chmod(frozen, 0o600)
            converted = None
            try:
                if kind == "audio" and frozen.suffix != ".ogg":
                    converted = audio.convert_to_opus_ogg_temp(str(frozen))
                return _bridge_api_request("POST", "send", payload={
                    "recipient": target, "message": caption,
                    "media_path": converted or str(frozen),
                }, timeout=MEDIA_REQUEST_TIMEOUT_SECONDS)
            finally:
                if converted:
                    Path(converted).unlink(missing_ok=True)

    return perform_send(kind, recipient, request_id,
                        {"sha256": digest, "extension": source.suffix.lower(), "caption": caption}, execute)
