"""Direct quoted replies bound to an exact stored target."""
from typing import Any, Dict

from outbound_policy import perform_send
from whatsapp import _bridge_api_request


def reply_message(chat_jid: str, message_id: str, message: str,
                  request_id: str) -> Dict[str, Any]:
    """Send a quoted text reply directly when send access is enabled.

    Copy chat_jid and message_id from list_messages. Phone numbers are accepted
    for phone-addressed direct chats; use the stored @lid JID for LID chats.
    The original message fingerprint is checked immediately before sending.
    Reuse request_id and the exact same arguments for every retry. No human
    confirmation token is requested.
    Old media without a captured payload and ambiguous legacy group senders
    fail closed. Never silently fall back to an unquoted send.
    """
    if not chat_jid.strip() or not message_id.strip() or not message.strip():
        return {"success": False, "message": "chat_jid, message_id and message are required"}
    def execute(recipient: str) -> Dict[str, Any]:
        result = _bridge_api_request("GET", "reply/preview", params={
            "chat_jid": recipient, "message_id": message_id,
        })
        if not result.get("success"):
            return result
        target = result["data"]
        if target["chat_jid"] != recipient or target["message_id"] != message_id:
            return {"success": False, "code": "reply_target_mismatch"}
        return _bridge_api_request("POST", "reply", payload={
            "chat_jid": recipient, "message_id": message_id,
            "message": message, "fingerprint": target["fingerprint"],
        })

    return perform_send("reply", chat_jid, request_id,
                        {"message_id": message_id, "message": message}, execute)
