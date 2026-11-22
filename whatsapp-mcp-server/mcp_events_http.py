"""Small authenticated MCP 2.0 event/JSON-RPC adapter beside the existing SDK.

Legacy initialize/session traffic continues through SDK 1.28 unchanged. MCP 2.0
discovery and stateless tools/events share /mcp and the SAME OAuth provider.
"""
import asyncio
import json

from mcp import types
from mcp_events import EVENT_NAME, PROTOCOL, EventError

REFERENCE_TOOL = {'name': 'get_transfer_event_reference',
    'description': 'Read source message identifiers for an event delivered to this OAuth grant. '
                   'Use authorized WhatsApp read tools to verify context; never payment authority.',
    'inputSchema': {'type': 'object', 'properties': {'event_id': {'type': 'string', 'minLength': 1, 'maxLength': 128}},
                    'required': ['event_id'], 'additionalProperties': False},
    'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}}


class EventRPC:
    def __init__(self, mcp, broker, provider):
        self.mcp, self.broker, self.provider = mcp, broker, provider

    @staticmethod
    def recognizes(body, headers):
        try:
            request = json.loads(body)
        except (ValueError, UnicodeError):
            return False
        if not isinstance(request, dict):
            return headers.get(b'mcp-protocol-version') == [PROTOCOL.encode()]
        method = request.get('method', '')
        params = request.get('params')
        meta = params.get('_meta', {}) if isinstance(params, dict) else {}
        version = meta.get('io.modelcontextprotocol/protocolVersion') if isinstance(meta, dict) else None
        return (method == 'server/discover' or isinstance(method, str) and method.startswith('events/')
                or version == PROTOCOL or headers.get(b'mcp-protocol-version') == [PROTOCOL.encode()])

    async def handle(self, body, authorization):
        if len(authorization) != 1 or not authorization[0].startswith(b'Bearer '):
            return 401, None
        try:
            token = authorization[0][7:].decode('ascii')
            access = await self.provider.load_access_token(token)
            authorized = access and access.subject == 'owner' and 'whatsapp' in access.scopes
            owner = await self.provider.event_owner(token)
        except Exception:
            authorized, owner = False, None
        if not authorized:
            return 401, None
        event_access = bool(owner and self.provider.event_owner_active(owner))
        request_id = None
        try:
            request = json.loads(body)
            if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or 'id' not in request:
                raise EventError('invalid_jsonrpc_request', -32600)
            request_id = request['id']
            if type(request_id) not in (str, int):
                request_id = None
                raise EventError('invalid_request_id', -32600)
            method, params = request.get('method'), request.get('params', {})
            if not isinstance(params, dict):
                raise EventError('invalid_params')
            # MCP 2 supplies per-request metadata in params, not session state.
            params = {key: value for key, value in params.items() if key != '_meta'}
            if ((isinstance(method, str) and method.startswith('events/')) or
                    (method == 'tools/call' and params.get('name') == REFERENCE_TOOL['name'])) and not event_access:
                return 401, None
            if method == 'server/discover':
                result = {'resultType': 'complete', 'supportedVersions': [PROTOCOL],
                          'serverInfo': {'name': 'whatsapp', 'version': '0.1.0'},
                          'capabilities': {'tools': {}, **({'events': {}} if event_access else {})}}
            elif method == 'events/list':
                if params:
                    raise EventError('invalid_params')
                result = {'events': [self.broker.definition()]}
            elif method == 'events/subscribe':
                result = await asyncio.to_thread(self.broker.subscribe, owner, params)
            elif method == 'events/unsubscribe':
                result = self.broker.unsubscribe(owner, params)
            elif method == 'tools/list':
                if params:
                    raise EventError('invalid_params')
                tools = await self.mcp.list_tools()
                result = {'tools': [t.model_dump(mode='json', by_alias=True, exclude_none=True) for t in tools]
                          + ([REFERENCE_TOOL] if event_access else [])}
            elif method == 'tools/call':
                if params.get('name') == REFERENCE_TOOL['name']:
                    args = params.get('arguments', {})
                    if (not isinstance(args, dict) or set(args) != {'event_id'}
                            or not isinstance(args['event_id'], str) or not 0 < len(args['event_id']) <= 128):
                        raise EventError('invalid_event_reference')
                    value = self.broker.store.reference(owner, args['event_id'])
                    result = {'content': [{'type': 'text', 'text': json.dumps(value)}],
                              'structuredContent': value, 'isError': False}
                else:
                    # Reuse SDK tool parsing, input/output validation and error
                    # normalization rather than weakening existing tool guards.
                    parsed = types.CallToolRequest.model_validate(request)
                    handler = self.mcp._mcp_server.request_handlers[types.CallToolRequest]
                    from mcp.server.auth.middleware.auth_context import auth_context_var
                    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
                    context = auth_context_var.set(AuthenticatedUser(access))
                    try:
                        result = (await handler(parsed)).model_dump(mode='json', by_alias=True, exclude_none=True)
                    finally:
                        auth_context_var.reset(context)
            else:
                raise EventError('method_not_found', -32601)
            result.setdefault('resultType', 'complete')
            return 200, {'jsonrpc': '2.0', 'id': request_id, 'result': result}
        except EventError as exc:
            return 200, {'jsonrpc': '2.0', 'id': request_id, 'error': {
                'code': exc.code, 'message': 'CallbackEndpointError' if exc.code == -32015 else 'Request refused',
                'data': {'reason': exc.reason}}}
        except Exception:
            return 200, {'jsonrpc': '2.0', 'id': request_id,
                         'error': {'code': -32603, 'message': 'Event request failed'}}
