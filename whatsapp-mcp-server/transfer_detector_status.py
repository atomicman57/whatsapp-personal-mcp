"""Read-only, non-content detector health for the owner's MCP clients."""
import os
import requests

def get_transfer_detection_status() -> dict:
    """Read the event detector's connectivity/backlog; never run a transfer."""
    token=os.environ.get('WHATSAPP_BRIDGE_READ_TOKEN','')
    if len(token)<32:return {'enabled':False,'code':'detector_not_configured'}
    try:
        response=requests.get('http://127.0.0.1:8755/status',headers={'Authorization':'Bearer '+token},
                              timeout=3,allow_redirects=False)
        response.raise_for_status()
        result=response.json()
        if not isinstance(result,dict):raise ValueError()
        allowed={'enabled','source_account','queued_notifications','accepted_notifications','go_live',
            'stream_connected','last_event','last_webhook_accepted','read_error','delivery_error',
            'gap_detected','delivery_semantics','payment_execution','last_24h_p90_ms','last_24h_latency'}
        return {key:value for key,value in result.items() if key in allowed}
    except (requests.RequestException,ValueError):
        return {'enabled':True,'code':'detector_status_unavailable','payment_execution':False}


def _owner_intake(path, payload=None):
    from mcp.server.auth.middleware.auth_context import get_access_token
    access = get_access_token()
    if not access or getattr(access, 'subject', None) != 'owner' or 'whatsapp' not in access.scopes:
        return {'success': False, 'code': 'owner_auth_required'}
    token = os.environ.get('WHATSAPP_BRIDGE_READ_TOKEN', '')
    if len(token) < 32:
        return {'success': False, 'code': 'detector_not_configured'}
    try:
        response = requests.request('POST' if payload is not None else 'GET',
            'http://127.0.0.1:8755/' + path, json=payload,
            headers={'Authorization': 'Bearer ' + token}, timeout=3, allow_redirects=False)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        return {'success': False, 'code': 'detector_intake_unavailable'}


def record_intake_stage(event_id: str, stage: str) -> dict:
    """Owner-only append-only timestamp. Stages: intake_received, card_created, owner_alerted.

    Record card_created only after upserting the owner card by request_key. This is
    telemetry, never payment approval. No text, account numbers or timestamps accepted.
    """
    from transfer_telemetry import STAGES
    if not isinstance(event_id, str) or not 0 < len(event_id) <= 128 or stage not in STAGES:
        return {'success': False, 'code': 'invalid_intake_stage'}
    return _owner_intake('stages', {'event_id': event_id, 'stage': stage})


def get_transfer_latency_report(days: int = 14) -> dict:
    """Owner-only per-hop median/p90/max milliseconds and event IDs above five seconds.

    Missing owner stages are reported separately, never counted as successful cards.
    """
    if type(days) is not int or not 1 <= days <= 90:
        return {'success': False, 'code': 'invalid_days'}
    return _owner_intake('latency?days=' + str(days))
