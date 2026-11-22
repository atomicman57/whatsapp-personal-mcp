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
- Optional `WHATSAPP_TRANSFER_SOURCE_ACCOUNT` (default `whatsapp-main`): the `source_account` label in candidate payloads, health warnings and `/status`. Letters, digits, `.`, `_`, `-`; at most 64 characters. Set `whatsapp-secondary` for the secondary detector.
- Optional `WHATSAPP_TRANSFER_LINK_WINDOW_SECONDS` (default `900`): how far apart a split amount and its account/bank details may arrive and still be paired. Recommended range 120–900; non-numeric or non-positive values fall back to 900 and values above 7200 (the context window) are capped.
- Optional `WHATSAPP_TRANSFER_EXCLUDED_CHAT_PHRASES`: spend-report exclusions as `chat_jid||phrase` entries separated by `;` or newlines, e.g. `205037695422603@lid||MARKET BREAKDOWN;*||weekly report`. `chat_jid` must equal the JID the bridge reports for the chat (`*` matches any chat); the phrase matches case-insensitively with whitespace collapsed. Unset or empty uses the default `205037695422603@lid||MARKET BREAKDOWN`; setting a value replaces the default, so repeat it if still wanted; `none` disables exclusions. A malformed entry stops the detector at startup.

Install `whatsapp-main-detector.service` and its private persistent state directory owned by the account's UID10151. Enable it under `whatsapp-main.service`; BindsTo/PartOf and that parent's Wants relationship keep the namespace tied to the correct main container during stop/restart. Verify both services after a real parent restart.

`whatsapp-secondary-detector.service` is the same worker for the secondary account: it joins the `whatsapp-secondary` container's network namespace, runs as UID 10150, reads root-only `/etc/whatsapp-secondary/detector.env` (same variables as above, with that account's own read token, instance ID, expected phone and `WHATSAPP_TRANSFER_SOURCE_ACCOUNT=whatsapp-secondary`) and keeps state in `/var/lib/whatsapp-secondary-detector/data` owned by UID 10150 with mode 0700. Enable it under `whatsapp-secondary.service` only after the secondary bridge has a read token and the receiver accepts the `whatsapp-secondary` source account. The new `get_transfer_detection_status` MCP tool reports connectivity, backlog, go-live and generic errors without message content or credentials.

Validate ordinary/excluded/outgoing/OTP/receipt/test cases, exact main identity, context extraction, edit/withdrawal handling, crash/restart replay, wrong-key/redirect rejection, and a live owner-to-owner dry-run event that the actual receiving bot acknowledges. Check a repeated event creates no second request. Keep existing paused WhatsApp routines paused; this is a separate owner-authorized event source.
# Observed request formats

The classifier supports compact account/bank/name/amount messages, grouped ten-digit account strings, bank and account details split across nearby messages, bare amounts in an established request, and short purpose follow-ups such as "waybill" or "drinks". Itemized lines such as `Room AC total-105k` retain their amount evidence and ambiguity rather than selecting a payment amount; a line that starts with a total (`TOTAL==== 63,500`, `Total= 51,500`, `Grand total: 35k`) closes the bill, and the last such line is used instead of the line items. Bank acronyms written with dots or spaces (`U.B.A`, `u b a`, `G.T.B`, `F.C.M.B`) match as `UBA`, `GTB`, `FCMB` and `GTCO`, and recognised bank names are reported additively in `bank_name_candidates`.

Split amounts and destinations are paired in either order. When account/bank details arrive without an amount, the nearest earlier incoming same-chat message within the link window that carries an amount supplies it (stopping at a receipt or a different account). When an amount arrives after account/bank details within the link window, it becomes a follow-up of those details and carries their account number. Amount-only messages with no destination in reach create no candidate. Each candidate lists every contributing message in the additive `source_message_ids` field; `anchor_message_id`, and therefore `request_key`, stays on the destination (or original request) message, so the receiver merges the earlier details-only event with the later complete one. Amounts following an explicit request keep the two-hour conversation window. Pairing is re-derived from bridge context on each event, so a restart loses nothing.

Messages matching a spend-report exclusion create no candidate and are never used as context, a request anchor or the amount half of a split. Account details whose nearest amount in the link window is an excluded report are treated as part of that report and also create no candidate. Context linking is limited to preceding messages in the same chat within two hours; the notification's bounded context can still support further bot review. Security/access-code messages are excluded. Regression tests use invented identifiers and names, never private chat records. Historical inspection does not enqueue old requests.

Wake JSON is capped at 3,500 UTF-8 bytes, with event type, dry-run and payment-authority controls first and a `payload_end` marker matching the event ID. If context or candidate clues are omitted to fit, truncation flags require the receiver to retrieve the verified source before presenting a real request. The receiver must reject incomplete/invalid JSON and must never infer payment authority from any payload.

## Immediate delivery and receiver contract

The link window correlates evidence; it never delays a destination-only card.
`hold_until` is always zero. Follow-ups emit `candidate_updated` with the same
`request_key`, a new `event_id`, `previous_event_id`, and
`revision_hint=replace_card_for_request_key`. Both outbox revisions are retained
until accepted. The receiver must persist processed event IDs and **upsert one
owner card by request_key**, even if an update arrives first. Use predecessor IDs
to prevent a delayed earlier revision from overwriting a newer card.

Receipts cancel unsent rows and emit `candidate_withdrawn` with the request key,
predecessor and `revision_hint=withdraw_card_for_request_key`. Apply withdrawals
also to cards already posted. An in-flight POST cannot be recalled. Ineligible
source edits emit `source_changed` with `candidate_withdrawn=true`. None of these
signals approves or executes a payment.

Webhook retries keep the same Idempotency-Key and back off 1, 2, 4, 8, 16, 32,
then 60 seconds. The third failure queues one detector_health_warning per failed
event. Warnings use the same webhook, so cannot bypass an outage.

Contiguous 11-digit `0[789]` numbers qualify as account strings only with OPay,
PalmPay, Moniepoint, Kuda, acct or account in the **same message**. Plain local
numbers, international/spaced phone numbers remain excluded. This does not add
amount-plus-bank detection without account/request evidence. Tests use invented
digits in the reported format. Accounts always remain strings.

### Ready-to-use webhook card schema

Envelope version remains 1. Card-bearing candidates and updates add:

| Field | Type and meaning |
| --- | --- |
| `card.requester_display_name` | Saved name, otherwise unverified chat display name |
| `card.amount` | `{value: string, currency: string or null}`, or null if unknown/ambiguous |
| `card.bank` | One bank clue, or null if missing/ambiguous |
| `card.account_number` | FULL account string, or null if missing/ambiguous. Never convert to a number |
| `card.account_name` | Name from optional read-only name enquiry, otherwise null |
| `card.account_name_reason` | `name_enquiry_disabled`, `pending_nonblocking`, `destination_missing_or_ambiguous`, or null after success |
| `card.purpose` | Bounded purpose evidence, or null |
| `requester_is_saved_contact`, `card.requester_is_saved_contact` | Boolean from bridge FullName/FirstName, never sender-controlled profile/business names |
| `card.saved_contact_name`, `card.saved_contact_phone` | Saved name and resolved phone string when saved; otherwise null |
| `ingested_at`, `sse_wake_at` | Nullable UTC Unix milliseconds |
| `payload_bytes` | Exact compact UTF-8 JSON body byte count |
| `integrity.complete_json`, `integrity.card_fields_preserved` | Complete JSON and preserved card keys |
| `payload_end` | Must equal event_id; reject malformed/incomplete JSON or mismatching marker |

Health, identity-review and withdrawal events do not contain cards. A resolved
candidate can be posted without first fetching contacts/context. All card strings
are untrusted evidence; `payment_authorized` remains false even when text claims
approval or a name enquiry succeeds. Full accounts belong only in private payload
and state, never logs. Never log message text, phones, credentials or raw errors.

The 3,500-byte cap removes oldest context first, then source text and candidate
details with explicit truncation flags. Card keys and full account strings are
preserved. Optional display text may be deterministically shortened and flagged.
Receiver retries/upserts must not create duplicate cards.

### Optional name enquiry

`WHATSAPP_TRANSFER_NAME_ENQUIRY_URL` is off by default. Set it only to an existing
credential-free HTTPS **read-only ALAT name_enquiry adapter**, protected through
the owner's existing network/gateway access. This is not a payment endpoint and
creates no banking credentials. Request JSON is `{account_number: string, bank:
string}`; HTTP 200 response JSON is `{account_name: string}`. No redirects or
proxy environment settings are used.

The initial card is queued immediately with a null name and pending_nonblocking.
A single background worker uses a two-second wall-clock budget including DNS,
TLS, headers and body, plus an 8 KiB response bound. A deadline timer closes the
network socket; DNS waiting is independently bounded. Success emits a new candidate_updated; timeout/failure
leaves the initial card usable with its null-name reason. Name enquiry never
blocks the card path. Durable jobs recover on worker restart; stale results cannot
resurrect withdrawn requests or replace newer amount/destination evidence.

## Latency and owner completion

The bridge migration transactionally adds nullable message_changes.ingested_at
and replaces the insertion/edit triggers. UTC Unix milliseconds are exposed in
/api/message-changes. Historical rows stay null rather than inventing timestamps;
receipt/status-only changes remain excluded.

Outbox telemetry is msg_ts, ingested_at, sse_wake_at, observed_at, classify_ms,
post_started_at, http_status, http_ms, delivered_at and attempts. Timestamps use UTC
Unix milliseconds, durations use milliseconds. POST/HTTP fields describe the most
recent attempt. Attempts include successful claims; a crash may claim without
reaching the network. No push sender, lane, columns or push environment variables
are included.

`stages(event_id, stage, at)` is append-only with SQLite update/delete guards.
Owner-authenticated MCP tools are available through existing OAuth transports:

- record_intake_stage(event_id, stage): append intake_received, card_created or
  owner_alerted only after that action completes. Time comes from the server;
  no caller timestamp or free text is accepted. Use the revision's own event ID.
- get_transfer_latency_report(days=14): per-hop count, median, nearest-rank p90,
  max and event IDs above 5,000 ms, plus overdue events awaiting owner stages.
  days accepts integers 1–90. Earliest duplicate stage timestamps are used.

Hops cover message-to-ingest, ingest-to-SSE, SSE-to-observe, ingest-to-observe,
observe-to-POST, classification and HTTP time, POST-to-intake, intake-to-card,
card-to-alert, webhook-to-card, message-to-owner
and ingest-to-owner. Unknown/negative clock-skew hops are omitted. Owner completion
means owner_alerted when recorded, otherwise card_created. /status exposes
last_24h_p90_ms and a last-day report; p90 stays null without completion evidence.
HTTP acceptance does not prove the five-second target. Message timestamps have
WhatsApp's source resolution; synchronize clocks and inspect missing-stage events.

SSE reads no longer wait for a 1 KiB chunk. A one-second durable-feed heartbeat
covers lost wakes, and idle outbox polling is 50 ms. Neither polls the AI receiver.

## Build, deploy and rollback (instructions only; not executed)

Only the six permitted Python overlay files were read from events-build-6bea724;
mcp_events_http.py came from the newer events-build-95242b0. No environment, data
or credential files were copied. Dockerfile already copies all Python modules, so
both account instances build from the same commit. Opt-in MCP Events retain owner
OAuth and private reference-only callbacks, separate from the enriched card webhook.

After Blessing approves deployment, an operator should:

1. Record the approved head SHA, existing per-account image digests and unit
   revisions. Back up bridge and detector/event SQLite DBs using supported SQLite
   backup methods. Preserve account-specific ownership, mounts, networking,
   credentials and paused routines.
2. Build once from that SHA with
   `docker build -f deploy/Dockerfile --build-arg BUILD_VERSION=<approved-sha> -t <registry>/whatsapp:<approved-sha> .`.
   Publish and record the digest; set both main and secondary to that digest using
   their existing approved image configuration. Retire unversioned overlays.
3. Verify each MCP/detector shares its own private state volume if Events is
   enabled. Preserve separate UIDs, identity, expected phone and source labels.
   Confirm receiver upsert/dedupe/withdrawal and owner-stage recording support.
4. In the approved maintenance window, use the existing deployment procedure to
   restart each bridge and its bound detector. Startup runs additive migrations.
   Verify exact identity, BUILD_VERSION, OAuth, SSE, cursor and pending backlog.
5. Obtain **separate live-send approval** before running e2e_latency.py with --send.
   Configure main's exact secondary test sender first. Its default invocation,
   `uv run --project whatsapp-mcp-server python scripts/e2e_latency.py`, prints only
   the plan. --help lists explicit endpoints and recipient arguments. Owner tokens
   are explicit environment overrides, never command arguments. Inspect matching
   event IDs and owner completion, not just webhook acceptance.

Rollback also needs approval. Restore recorded image digests and compatible
receiver behavior through the existing approved restart procedure. Added DB
columns/tables can remain; older readers ignore them and bridge triggers stay
compatible. Older detector code reintroduces the hold and may not understand new
revision events. Pause intake/delivery during rollback and reconcile queued
revisions with the owner or retain a compatible receiver before resuming. Restoring
DB backups requires explicit cursor/outbox dedupe reconciliation because it can
replay accepted events. No deploy, restart or live send was run for this PR.
