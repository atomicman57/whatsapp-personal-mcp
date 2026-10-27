# Event-driven transfer-request intake

The optional detector reads live message events from the private Go bridge and wakes an authenticated owner-configured Grokbot webhook only for likely money requests. It does not run an AI polling routine, send WhatsApp messages, validate beneficiaries or execute payments.

`WHATSAPP_BRIDGE_READ_TOKEN` must be a separate random credential. The bridge accepts it for GET/HEAD only; all writes remain denied regardless of sending/mutation configuration. The detector needs no maintenance MCP key, WhatsApp login database or banking credential. It shares the main container's network namespace to read the existing loopback API; no new public bridge/status port is opened.

SQLite message-change triggers retain a bounded log of 100,000 insertion/edit keys. The event stream wakes the consumer immediately; a cheap server-local catch-up heartbeat and the durable sequence handle reconnects and restarts. Identical history re-syncs and receipt-only updates do not create changes. The initial cursor starts at the current sequence, so old requests are not replayed. A retention/restored-cursor gap stops intake and queues an operational warning instead of silently advancing.

Rules identify possible requests using explicit money/request wording, 10-digit account candidates with bank or current-request context, amount follow-ups and related attachments. They are heuristics: a candidate is not a verified bank account or payment instruction. The receiver must review context and ask the owner for approval. Images/documents are referenced for later extraction; no automatic OCR or transcription is provided. Private direct chats are supported; groups/status broadcasts are skipped.

Exact excluded phone numbers and contact-name aliases are checked before context is fetched. PN/LID resolution uses the device's actual mapping, never equal-looking digits. An unresolved identity produces metadata-only review without context. OTP/password messages are filtered, including from surrounding context. Context is bounded to recent same-chat messages, explicitly marked if truncated/incomplete. The receiving bot can use the MCP to retrieve more after verifying the identity.

Each message/content revision gets a stable event ID and an associated request key. The private durable outbox retries with the same event ID and an Idempotency-Key header. **Delivery is at least once:** the receiver must persist event/source IDs to prevent duplicate cards and merge follow-ups. Source changes that no longer qualify produce a withdrawal/review notification without leaking newly sensitive content. No webhook flag or incoming text counts as owner approval. After HTTP acceptance the outbox clears its extra context copy and retains dedupe/status metadata. HTTP acceptance alone does not prove bot processing.

Keep the webhook URL and exact authorization header/value in the owner's encrypted credential store and root-only `/etc/whatsapp-main/detector.env`; never print them, put them in Git, or pass them in command arguments. Only HTTPS, certificate-verified, non-redirecting POSTs are used. Configure:

- `WHATSAPP_BRIDGE_URL=http://127.0.0.1:8753/api`
- `WHATSAPP_BRIDGE_READ_TOKEN` (also set on the main bridge/MCP)
- `WHATSAPP_BRIDGE_INSTANCE_ID` and `WHATSAPP_TRANSFER_EXPECTED_PHONE`
- `WHATSAPP_TRANSFER_WEBHOOK_URL`
- `WHATSAPP_TRANSFER_WEBHOOK_AUTH_HEADER` and `WHATSAPP_TRANSFER_WEBHOOK_AUTH_VALUE` exactly as the first-party panel specifies
- `WHATSAPP_TRANSFER_EXCLUDED_PHONES`, `WHATSAPP_TRANSFER_EXCLUDED_NAMES`
- `WHATSAPP_TRANSFER_STATE_DIR=/state`
- Optional `WHATSAPP_TRANSFER_TEST_SENDER`: exact verified owner secondary number, allowing only that account's `TRANSFER_DETECTOR_TEST:` messages to travel as explicit dry runs. All other synthetic/test messages are ignored.

Install `whatsapp-main-detector.service` and its private persistent state directory owned by the account's UID10151. Enable it under `whatsapp-main.service`; BindsTo/PartOf and that parent's Wants relationship keep the namespace tied to the correct main container during stop/restart. Verify both services after a real parent restart. The new `get_transfer_detection_status` MCP tool reports connectivity, backlog, go-live and generic errors without message content or credentials.

Validate ordinary/excluded/outgoing/OTP/receipt/test cases, exact main identity, context extraction, edit/withdrawal handling, crash/restart replay, wrong-key/redirect rejection, and a live owner-to-owner dry-run event that the actual receiving bot acknowledges. Check a repeated event creates no second request. Keep existing paused WhatsApp routines paused; this is a separate owner-authorized event source.
