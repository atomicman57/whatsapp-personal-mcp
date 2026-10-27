import os
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import List, Dict, Any, Callable, Optional
from mcp.server.fastmcp import FastMCP
from macos_contacts import contacts_app_status as macos_contacts_app_status
from direct_sending import send_message, send_file, send_audio_message
from replies import reply_message
from workflow_reads import export_chat, list_chat_state, get_message_status, get_contact_details, list_new_messages, download_profile_photo
from transcription import transcribe_audio_file as whisper_transcribe_audio_file
from transcription import whisper_status as get_whisper_status
from whatsapp import (
    get_bridge_status as whatsapp_get_bridge_status,
    get_own_identity as whatsapp_get_own_identity,
    search_contacts as whatsapp_search_contacts,
    search_contacts_chats_and_groups as whatsapp_search_contacts_chats_and_groups,
    list_messages as whatsapp_list_messages,
    list_chats as whatsapp_list_chats,
    list_joined_groups as whatsapp_list_joined_groups,
    get_group_info as whatsapp_get_group_info,
    create_group as whatsapp_create_group,
    update_group_participants as whatsapp_update_group_participants,
    set_chat_read_state as whatsapp_set_chat_read_state,
    is_on_whatsapp as whatsapp_is_on_whatsapp,
    set_chat_mute as whatsapp_set_chat_mute,
    set_chat_archive as whatsapp_set_chat_archive,
    set_contact_block as whatsapp_set_contact_block,
    send_typing_indicator as whatsapp_send_typing_indicator,
    get_chat as whatsapp_get_chat,
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
    get_contact_chats as whatsapp_get_contact_chats,
    get_last_interaction as whatsapp_get_last_interaction,
    get_message_context as whatsapp_get_message_context,
    download_media as whatsapp_download_media
)

# Initialize FastMCP server
mcp = FastMCP("whatsapp")

mcp.tool()(reply_message)
mcp.tool()(send_message)
mcp.tool()(send_file)
mcp.tool()(send_audio_message)
mcp.tool()(export_chat)
mcp.tool()(list_chat_state)
mcp.tool()(get_message_status)
mcp.tool()(get_contact_details)
mcp.tool()(list_new_messages)
mcp.tool()(download_profile_photo)

MAX_PAGE_SIZE = 100
MAX_PAGE_NUMBER = 1_000
MAX_CONTEXT_MESSAGES = 50


def _structured(value):
    """Convert cache dataclasses to the dictionaries advertised by MCP schemas."""
    if is_dataclass(value):
        return _structured(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _structured(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_structured(item) for item in value]
    return value


def _clamp_integer(value: int, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(parsed, maximum))


def _mutation(execute: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    if os.environ.get("WHATSAPP_MUTATIONS_ENABLED", "").lower() != "true":
        return {"success": False, "code": "mutations_disabled"}
    return execute()


@mcp.tool()
def set_contact_name(phone_number: str, name: str) -> Dict[str, Any]:
    """Create/rename a WhatsApp contact directly when mutation access is enabled.

    Uses an exact international phone number. Sends a WhatsApp contact sync
    patch; it does not create an iCloud/macOS address-book entry.
    """
    from whatsapp import _bridge_api_request
    return _mutation(lambda: _bridge_api_request("POST", "contact/name", payload={
        "phone_number": phone_number, "name": name}))

@mcp.tool()
def search_contacts(query: str) -> List[Dict[str, Any]]:
    """Search known WhatsApp chats and optional macOS Contacts.app entries.

    Opaque LID candidates retain their JID and a null phone_number. Resolve them
    using get_contact_details; never interpret the LID's digits as a phone.
    
    Args:
        query: Search term to match against contact names or phone numbers
    """
    if not query.strip():
        return []
    contacts = whatsapp_search_contacts(query)
    return _structured(contacts)


@mcp.tool()
def contacts_app_status() -> Dict[str, Any]:
    """Check whether read-only macOS Contacts.app lookup is available and authorized."""
    return macos_contacts_app_status()


@mcp.tool()
def bridge_status() -> Dict[str, Any]:
    """Check live WhatsApp bridge connectivity and local messages.db freshness.

    Read tools use the local database even when the bridge is unavailable. Inspect
    reads_may_be_stale before treating local results as current.
    """
    return whatsapp_get_bridge_status()


@mcp.tool()
def whisper_status() -> Dict[str, Any]:
    """Report whether optional local Whisper transcription is installed."""
    return get_whisper_status()


@mcp.tool()
def get_own_identity() -> Dict[str, Any]:
    """Get the connected WhatsApp identity for resolving references such as me or myself."""
    return whatsapp_get_own_identity()


@mcp.tool()
def search_contacts_chats_and_groups(
    query: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Search direct chats, groups, recent chats, and optional macOS contacts.

    Args:
        query: Non-empty name or phone-number fragment.
        limit: Maximum number of unified results, from 1 to 100.
    """
    if not query.strip():
        return []
    return whatsapp_search_contacts_chats_and_groups(query, max(1, min(limit, 100)))


@mcp.tool()
def list_joined_groups() -> Dict[str, Any]:
    """List WhatsApp groups currently joined by the connected identity."""
    return whatsapp_list_joined_groups()


@mcp.tool()
def get_group_info(group_jid: str) -> Dict[str, Any]:
    """Get live WhatsApp group metadata and participant roles.

    Args:
        group_jid: Group JID ending in @g.us.
    """
    return whatsapp_get_group_info(group_jid)


@mcp.tool()
def is_on_whatsapp(phone_numbers: List[str]) -> Dict[str, Any]:
    """Check whether one or more international phone numbers are registered on WhatsApp.

    It performs a live WhatsApp lookup and does not send a message.
    """
    return whatsapp_is_on_whatsapp(phone_numbers)


@mcp.tool()
def create_group(name: str, participants: List[str]) -> Dict[str, Any]:
    """Create a group directly when mutation access is enabled."""
    return _mutation(lambda: whatsapp_create_group(name, participants))


def _change_group_participants(action: str, group_jid: str,
                               participants: List[str]) -> Dict[str, Any]:
    return _mutation(lambda: whatsapp_update_group_participants(group_jid, participants, action))


@mcp.tool()
def add_participants_to_group(group_jid: str, participants: List[str]) -> Dict[str, Any]:
    """Add group participants directly when mutation access is enabled."""
    return _change_group_participants("add", group_jid, participants)


@mcp.tool()
def remove_participants_from_group(group_jid: str, participants: List[str]) -> Dict[str, Any]:
    """Remove group participants directly when mutation access is enabled."""
    return _change_group_participants("remove", group_jid, participants)


@mcp.tool()
def promote_participants_to_admins(group_jid: str, participants: List[str]) -> Dict[str, Any]:
    """Promote group participants directly when mutation access is enabled."""
    return _change_group_participants("promote", group_jid, participants)


@mcp.tool()
def demote_participants_from_admins(group_jid: str, participants: List[str]) -> Dict[str, Any]:
    """Demote group admins directly when mutation access is enabled."""
    return _change_group_participants("demote", group_jid, participants)


@mcp.tool()
def set_chat_read_state(chat_jid: str, read: bool) -> Dict[str, Any]:
    """Mark a chat read/unread directly when mutation access is enabled."""
    return _mutation(lambda: whatsapp_set_chat_read_state(chat_jid, read))


@mcp.tool()
def set_chat_mute(chat_jid: str, muted: bool, duration_seconds: int = 0) -> Dict[str, Any]:
    """Mute/unmute a chat directly; zero duration means indefinitely."""
    return _mutation(lambda: whatsapp_set_chat_mute(chat_jid, muted, duration_seconds))


@mcp.tool()
def set_chat_archive(chat_jid: str, archived: bool) -> Dict[str, Any]:
    """Archive/unarchive a chat directly when mutation access is enabled."""
    return _mutation(lambda: whatsapp_set_chat_archive(chat_jid, archived))


@mcp.tool()
def set_contact_block(jid: str, blocked: bool) -> Dict[str, Any]:
    """Block/unblock a contact directly when mutation access is enabled."""
    return _mutation(lambda: whatsapp_set_contact_block(jid, blocked))


@mcp.tool()
def send_typing_indicator(chat_jid: str, state: str, media: str = "") -> Dict[str, Any]:
    """Update composing/paused presence directly when mutation access is enabled."""
    return _mutation(lambda: whatsapp_send_typing_indicator(chat_jid, state, media))

@mcp.tool()
def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> List[Dict[str, Any]]:
    """Get WhatsApp messages matching specified criteria with optional context.
    
    Args:
        after: Optional ISO-8601 formatted string to only return messages after this date
        before: Optional ISO-8601 formatted string to only return messages before this date
        sender_phone_number: Optional phone number to filter messages by sender
        chat_jid: Optional chat JID to filter messages by chat
        query: Optional search term to filter messages by content
        limit: Maximum number of messages to return (default 20)
        page: Page number for pagination (default 0)
        include_context: Whether to include messages before and after matches (default True)
        context_before: Number of messages to include before each match (default 1)
        context_after: Number of messages to include after each match (default 1)
    """
    limit = _clamp_integer(limit, 20, 1, MAX_PAGE_SIZE)
    page = _clamp_integer(page, 0, 0, MAX_PAGE_NUMBER)
    context_before = _clamp_integer(
        context_before, 1, 0, MAX_CONTEXT_MESSAGES
    )
    context_after = _clamp_integer(context_after, 1, 0, MAX_CONTEXT_MESSAGES)
    messages = whatsapp_list_messages(
        after=after,
        before=before,
        sender_phone_number=sender_phone_number,
        chat_jid=chat_jid,
        query=query,
        limit=limit,
        page=page,
        include_context=include_context,
        context_before=context_before,
        context_after=context_after
    )
    return _structured(messages)

@mcp.tool()
def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Dict[str, Any]]:
    """Get WhatsApp chats matching specified criteria.
    
    Args:
        query: Optional search term to filter chats by name or JID
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
        include_last_message: Whether to include the last message in each chat (default True)
        sort_by: Field to sort results by, either "last_active" or "name" (default "last_active")
    """
    limit = _clamp_integer(limit, 20, 1, MAX_PAGE_SIZE)
    page = _clamp_integer(page, 0, 0, MAX_PAGE_NUMBER)
    chats = whatsapp_list_chats(
        query=query,
        limit=limit,
        page=page,
        include_last_message=include_last_message,
        sort_by=sort_by
    )
    return _structured(chats)

@mcp.tool()
def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Dict[str, Any]]:
    """Get WhatsApp chat metadata by JID.
    
    Args:
        chat_jid: The JID of the chat to retrieve
        include_last_message: Whether to include the last message (default True)
    """
    chat = whatsapp_get_chat(chat_jid, include_last_message)
    return _structured(chat)

@mcp.tool()
def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Dict[str, Any]]:
    """Get WhatsApp chat metadata by sender phone number.
    
    Args:
        sender_phone_number: The phone number to search for
    """
    chat = whatsapp_get_direct_chat_by_contact(sender_phone_number)
    return _structured(chat)

@mcp.tool()
def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Dict[str, Any]]:
    """Get all WhatsApp chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    limit = _clamp_integer(limit, 20, 1, MAX_PAGE_SIZE)
    page = _clamp_integer(page, 0, 0, MAX_PAGE_NUMBER)
    chats = whatsapp_get_contact_chats(jid, limit, page)
    return _structured(chats)

@mcp.tool()
def get_last_interaction(jid: str) -> str:
    """Get most recent WhatsApp message involving the contact.
    
    Args:
        jid: The JID of the contact to search for
    """
    message = whatsapp_get_last_interaction(jid)
    return message

@mcp.tool()
def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> Dict[str, Any]:
    """Get context around a specific WhatsApp message.
    
    Args:
        message_id: The ID of the message to get context for
        before: Number of messages to include before the target message (default 5)
        after: Number of messages to include after the target message (default 5)
    """
    before = _clamp_integer(before, 5, 0, MAX_CONTEXT_MESSAGES)
    after = _clamp_integer(after, 5, 0, MAX_CONTEXT_MESSAGES)
    context = whatsapp_get_message_context(message_id, before, after)
    return _structured(context)

@mcp.tool()
def download_media(message_id: str, chat_jid: str) -> Dict[str, Any]:
    """Download media from a WhatsApp message and get the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        A dictionary containing success status, a status message, and the file path if successful
    """
    file_path = whatsapp_download_media(message_id, chat_jid)
    
    if file_path:
        return {
            "success": True,
            "message": "Media downloaded successfully",
            "file_path": file_path
        }
    else:
        return {
            "success": False,
            "message": "Failed to download media"
        }


@mcp.tool()
def transcribe_audio_message(
    message_id: str,
    chat_jid: str,
    language: Optional[str] = None,
    initial_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Download and locally transcribe audio from a WhatsApp message with Whisper.

    Whisper support is optional and is installed with ``uv sync --extra whisper``.
    The first transcription downloads the configured model. Audio stays on this
    machine during transcription.

    Args:
        message_id: ID of the WhatsApp audio message.
        chat_jid: JID of the chat containing the message.
        language: Optional ISO language code such as ``en``; auto-detected if omitted.
        initial_prompt: Optional vocabulary or context hint for Whisper.
    """
    file_path = whatsapp_download_media(message_id, chat_jid)
    if not file_path:
        return {
            "success": False,
            "message": "Failed to download audio from the WhatsApp message",
        }
    result = whisper_transcribe_audio_file(file_path, language, initial_prompt)
    result.setdefault("message_id", message_id)
    result.setdefault("chat_jid", chat_jid)
    return result

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--http", action="store_true")
    transport.add_argument("--hosted-http", action="store_true")
    parser.add_argument("--port", type=int, default=8744)
    arguments = parser.parse_args()
    if arguments.hosted_http:
        from hosted_http import run_hosted_http
        run_hosted_http(mcp, arguments.port)
    elif arguments.http:
        from local_http import run_local_http
        run_local_http(mcp, arguments.port)
    else:
        mcp.run(transport='stdio')
