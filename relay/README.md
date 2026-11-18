# Biruni Relay (`relay.biruni-gis.com`)

Minimal Cloudflare Worker + Durable Object WebSocket relay for the Biruni bridge fallback ladder.

## Free-tier note
- One hub object (`HubDO` named `"hub"`) serves all rooms in memory only (no storage writes).
- At the limit (rooms, sockets, rate, or Workers free-tier limits), connections fail or close with `1013`, the client backs off for up to 10 minutes, and the other levels of the ladder carry on.
