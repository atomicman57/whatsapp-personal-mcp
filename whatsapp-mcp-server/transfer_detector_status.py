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
            'gap_detected','delivery_semantics','payment_execution'}
        return {key:value for key,value in result.items() if key in allowed}
    except (requests.RequestException,ValueError):
        return {'enabled':True,'code':'detector_status_unavailable','payment_execution':False}
