# Hosted second-account deployment

Run the bridge and MCP together on an always-on Linux Docker host. The bridge remains on container loopback; publish only the authenticated MCP at host loopback. A dedicated Cloudflare Tunnel provides HTTPS without opening a server firewall port. This deployment does not depend on a laptop tunnel.

The hosted HTTP mode requires `WHATSAPP_MCP_PUBLIC_URL` (a canonical HTTPS origin) and a distinct random `WHATSAPP_MCP_TOKEN` with at least 32 characters. Without OAuth mode, every MCP HTTP request uses that private bearer credential. With OAuth enabled, agent clients should use the discovery/PKCE flow described below instead of receiving the maintenance key. Keep credentials private and rotate on loss; no per-message approval is imposed by the MCP.

Set the bridge token separately using `WHATSAPP_BRIDGE_TOKEN`. In production, generate both via the owner's encrypted credential store, then send the minimum runtime secrets to root-owned `/etc/whatsapp-secondary/runtime.env` over verified SSH. The tunnel credential is a separate root-directory file mounted read-only for cloudflared. Service units and client examples must never contain literal tokens. Cloudflare API administration credentials are not installed on the server.

Build `deploy/Dockerfile` from the repository root. Python dependencies are hash-locked in `requirements.lock`. `/data` must be a persistent private host directory owned by UID/GID 10150. Use the provided hardened service units and an `images.env` file containing `WHATSAPP_IMAGE` (the versioned local image) and `TUNNEL_IMAGE` (the official cloudflared image pinned by digest). Neither unit grants capabilities or publishes the bridge. Application access logs are disabled; do not enable message-body logging.

Before moving a linked account, stop its local bridge and MCP services. Transfer its private store using verified encrypted SSH, including all SQLite state and the durable outbound journal. Never run two bridges with copies of the same login database. Preserve the local stopped copy for rollback; do not auto-start it after migrating. Start the hosted service, verify exact account identity and journal replay, then start its tunnel. Do not mark an unpaired/disconnected bridge healthy merely because the HTTP process is running.

The send allowlist and do-not-contact list are server-side runtime configuration. Start with a self-only allowlist until account routing and the current exclusion list have been verified. Account mutations remain a separate capability. Persist all state under the host volume; a container's ephemeral root filesystem is not a database backup.

Before enabling broader use, test unauthenticated/incorrect-token rejection, authenticated MCP initialization, exact own identity, a self-chat text/reply/attachment, duplicate replay across a service restart and server-to-WhatsApp connectivity while laptop services are stopped. Distinguish provider acceptance from delivery and read receipts.

## OAuth connections for agent clients

Set `WHATSAPP_MCP_OAUTH_ENABLED=true` to enable the MCP SDK's OAuth authorization-server and protected-resource metadata, dynamic registration, PKCE code flow, refresh and revocation endpoints. Clients should register the HTTPS `/mcp` URL without a literal Authorization header and follow the OAuth connect card.

The owner consent page is served only by this MCP's HTTPS origin. It shows the requesting client and exact callback address, explains access, and requires the existing high-entropy owner connection key in a masked field. Enter that key only on the verified first-party page; never give it to an assistant chat. It remains the maintenance bearer credential as well. The assistant client receives separately issued expiring OAuth credentials and never needs the owner key. No per-message approvals are added by OAuth.

OAuth state is persisted in private `/data/oauth.sqlite`. Authorization codes expire after two minutes and can be exchanged only once; the SDK verifies registered redirects, client authentication and PKCE. Access tokens expire after one hour. Refresh tokens rotate, expire after 30 days, and replay revokes their whole grant family. Access/code/refresh values are stored only as digests; the SDK's dynamic client registration metadata, including any issued confidential-client secret, stays in the private database. The current recipient policy remains enforced independently of OAuth.

The HTTPS tunnel must pass the SDK discovery, registration, authorization, token, revocation and owner-consent paths to the authenticated MCP application. The application rejects unrelated paths and never proxies them to the WhatsApp bridge. All responses are marked no-store and HSTS. To revoke a client, use its authenticated OAuth revocation flow; owner-key rotation is a separate maintenance operation and does not by itself revoke previously approved client grants.

See [TRANSFER-DETECTOR.md](TRANSFER-DETECTOR.md) for the optional read-only event-driven request detector, private outbox and authenticated Grokbot wake. Main and secondary accounts must use separate login/state directories, credentials, OAuth origins and container identities.
## Saved-contact sending

`WHATSAPP_SEND_SAVED_CONTACTS=true` permits exact recipients with an owner-saved first/full name in the linked device directory, in addition to explicit `WHATSAPP_SEND_ALLOWLIST` exceptions. Profile names and business names alone cannot grant sending. Unmapped LIDs and groups fail closed unless explicitly allowed. `WHATSAPP_DO_NOT_CONTACT` is checked across verified PN/LID aliases; `WHATSAPP_DO_NOT_CONTACT_NAMES` blocks normalized seller names across saved, profile and business labels. Exclusions override saved status and explicit exceptions. Directory failures deny sending before dispatch. Contact create/rename writes are denied by the bridge in this mode even if other mutations are enabled, so an agent cannot save a new contact to authorize itself.

MCP performs a read-only recipient preflight before reserving an outbound request ID. The bridge independently enforces the same policy again for direct REST send/reply calls. The private authenticated GET `/api/recipient/policy?recipient=...` checks eligibility without sending a message or marking a chat read. There are no per-message approval tokens.
