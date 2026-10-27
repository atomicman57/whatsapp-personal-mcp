# Personal WhatsApp MCP

A local WhatsApp bridge and MCP server for personal chat workflows. This fork sends directly when configured send access permits it. There are no per-message human approval tokens or confirmation prompts.

This is an unofficial WhatsApp client using [whatsmeow](https://github.com/tulir/whatsmeow). WhatsApp may restrict unofficial clients. Account linking is required, and no implementation can promise a complete phone history or eliminate prompt-injection risks.

## Capabilities

- Contact/chat discovery, exact phone/JID lookup, group information and shared groups.
- Contact details: saved/profile names, business flag, about text and profile photo URL when WhatsApp privacy settings permit it.
- Contact create/rename through WhatsApp contact sync. This does not write an iCloud/macOS address-book entry.
- Cached history search, structured JSON export with stable IDs and cursor paging, and a new-since feed.
- Unread counts and archived state without marking a chat read.
- Direct text sends, quoted replies, images/documents with optional captions, and voice notes.
- Persistent request IDs for sends, including across MCP process restarts. Concurrent/repeated requests dispatch at most once through the MCP server. Uncertain outcomes never trigger an automatic retry.
- Delivery/read/playback receipts with observed timestamps. Group receipts describe individual recipients, not everyone in the group.
- Media downloads and optional local Whisper transcription.
- Optional group and chat mutations, enabled separately from sending.

WhatsApp Status publishing/view counts, reactions and full group receipt aggregation are not implemented. Existing cached history may lack forwarded metadata; unknown fields stay null. Historical deleted content, restricted profile information, and messages not supplied by WhatsApp are not reconstructed.

## Automatic controls

The bridge binds to loopback and rejects browser-originated requests. All HTTP routes require `WHATSAPP_BRIDGE_TOKEN` (at least 32 characters). Sending and other mutations are disabled until configured once by the owner:

| Setting | Meaning |
| --- | --- |
| `WHATSAPP_SEND_ENABLED=true` | Allow direct sends without per-message approval. |
| `WHATSAPP_MUTATIONS_ENABLED=true` | Allow contact, group, read-state, archive, mute and other state changes. |
| `WHATSAPP_STORE_DIR` | Dedicated private directory for this account's session, history, media and send journal. |
| `WHATSAPP_SEND_ALLOWLIST` | Optional comma-separated exact phone numbers/JIDs. Empty permits any otherwise permitted recipient. |
| `WHATSAPP_DO_NOT_CONTACT` | Comma-separated exact phone numbers/JIDs that must never receive sends. |

Phone numbers and their phone JIDs are normalized. If a deny list is configured, a LID or group needs an explicit allowlist entry because its membership/phone identity cannot be inferred safely. Keep those entries current. These controls do not protect against another program running as the owner that changes the code, configuration or session files.

Sending tools require a `request_id` of 8–128 letters, digits, dots, underscores, colons or hyphens. Use a new ID only for a new intended message. Retry with the same ID and content. `outcome_unknown` means the message may have been sent: do not generate a different ID to force another attempt. IDs, payload digests and protocol results are retained; outgoing text and attachment bytes are not stored in the send journal. WhatsApp history is stored separately.

Attachments must first be placed in `WHATSAPP_STORE_DIR/outbox`. Sending snapshots those bytes into a private temporary folder, preventing a changed source file from altering an in-progress send. The temporary folder is removed afterwards. Supported outbound extensions include images, PDF, MP4 and common audio formats; the outbound cap is 16 MiB.

Keep the bridge private. Do not publish its port or give the bridge credential to unrelated clients. The MCP's durable request journal is enforced at the MCP boundary; direct REST callers are authenticated but must manage their own request deduplication.

## Build and test

Requires Go 1.25 or newer, Python 3.11 or newer, a C compiler and [uv](https://docs.astral.sh/uv/). FFmpeg is needed for non-Opus voice notes. Whisper is optional.

```sh
cd whatsapp-mcp-server
uv sync --locked
uv run python -m unittest discover -s tests -q
cd ../whatsapp-bridge
gofmt -l .
go vet ./...
go test -race ./...
go build -o bin/whatsapp-bridge .
```

The tests use synthetic data and include a real MCP stdio client, HTTP transport, attachments, concurrent retries, process restart, read-only exports, recipient policies and receipt ordering. They do not prove a live account is linked or that WhatsApp delivered a real message.

## Local deployment

`scripts/personal_runtime.py` reads a private deployment JSON and obtains the bridge token from the owner's encrypted credential-store helper. The token is captured in memory and passed in the process environment, never written into the deployment file, command arguments, Grokbot config or launchd plist.

Deployment JSON keys:

- `repo`, `bridge`, `python`: absolute checkout, built bridge and virtual-environment interpreter paths.
- `store_dir`, `port`, `instance_id`: this account's private runtime directory and loopback identity.
- `credential_helper`, `token_credential`: credential-store helper path and the entry name holding the bridge token.
- `send_enabled`, `mutations_enabled`: persistent capability settings; no human prompts per operation.
- `send_allowlist`, `do_not_contact`: lists of exact recipient identifiers.

Runtime commands take `--config /absolute/path/to/deployment.json`:

- `bridge`: keep the API running; an unpaired account reports disconnected and does not generate pairing credentials.
- `pair`: interactive owner pairing. Never redirect pairing output into logs.

For phone-code linking, run `python3 scripts/personal_runtime.py pair --config /path/to/private-deployment.json --phone +COUNTRYCODEANDNUMBER` in an owner terminal. Enter its short-lived code in WhatsApp's Linked Devices → Link a Device → Link with phone number instead. Phone pairing uses a recognized browser/OS display because WhatsApp rejects custom application labels. Use a separate store, instance ID and ports when testing another account.
- `mcp`: serve MCP over stdio.
- `health`: show sanitized connectivity and coverage information.

For a desktop client that supports device-local HTTP MCP servers, the runtime
also supports `http` mode with `mcp_port` in the deployment JSON. This binds only
to `127.0.0.1` and rejects browser origins, forwarded requests and unexpected
Host headers. It trusts processes running as the local owner; do not forward
this endpoint through a public tunnel. A Grokbot HTTP custom server can request
`placement: "client"` for this device-local transport. Its actual registration
and available tools must be verified in the app. Pairing and real delivery tests
are separate from registration and tool discovery.

## Attribution and licence

Based on [DJNgoma/whatsapp-mcp](https://github.com/DJNgoma/whatsapp-mcp), a maintained fork of [Luke Harries' original](https://github.com/lharries/whatsapp-mcp). The original MIT notice is preserved in LICENSE. Dependency licences still apply to linked binaries; review the whatsmeow/libsignal dependency licences before redistribution.

## Always-on hosted use

The separate `--hosted-http` transport requires a canonical HTTPS origin and a distinct bearer credential on every request. Deploy the bridge and MCP together on an always-on Linux host with persistent private storage, following [deploy/README.md](deploy/README.md). The local `--http` mode remains loopback-only and must not be exposed through a tunnel. Hosted mode is explicitly configured and authenticated; it does not add per-message approvals. Its account state and duplicate journal must move together, with the old bridge stopped before the hosted one starts.

For agent clients that cannot privately bind a configured bearer header, hosted OAuth mode provides the SDK's normal connect-card flow. See the OAuth section in [deploy/README.md](deploy/README.md). The owner key is entered only on this server's HTTPS consent page; clients receive their own rotating credentials.
# Contact-name discovery

Contact search merges message-cache chat labels with the linked device's local saved names, profile names and business names. This works on the hosted Linux service without macOS Contacts. Results distinguish address-book entries from existing chats, keep ambiguous matches, and use verified PN/LID mappings rather than treating opaque LID digits as phone numbers. Directory lookup does not fetch profiles from the network, read messages or mark chats read.
