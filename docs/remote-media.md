# Files on remote MCP clients

The WhatsApp bridge reads files on its own server. For files created on a
Grokbot computer, call `create_media_upload` with a safe filename, byte count,
SHA-256 and stable request ID. Upload the exact bytes with HTTP PUT using the
returned URL and headers, then use `media_path` with `send_file` or
`send_audio_message`. A completed upload can be recovered with the same ID
without uploading again. Uploading does not send a WhatsApp message.

For inbound files, call `download_media`, then `get_media_download` with its
server path. Fetch the returned URL using its headers and verify the reported
byte count and SHA-256 before opening it.

These URLs use private, per-file, single-use authorization headers valid for
five minutes. Never display the headers in chat or put them in URL queries.
They do not grant MCP or WhatsApp bridge access. Different accounts have
independent capabilities and storage. Only supported attachment extensions
are accepted, up to 16 MiB per file. Staged uploads expire after 24 hours and
are pruned during subsequent media operations. Quotas bound pending uploads,
stored bytes and download grants. The normal MCP request limit remains 1 MiB;
binary uploads stream through the separately authenticated media route.

Sending still uses the existing recipient exclusions and durable request ID
journal. Reuse the original send ID after an uncertain send result; never
create a fresh ID just to retry it.
