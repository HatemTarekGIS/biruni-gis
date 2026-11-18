# Biruni Relay (`relay.biruni-gis.com`)

Minimal Cloudflare Worker + Durable Object WebSocket relay for the Biruni bridge fallback ladder.

## Free-tier note and fair-use budget
- One hub object (`HubDO` named `"hub"`) serves all rooms in memory only (no storage writes, no logging, no per-IP state).
- `ROOM_PER_HOUR` (`600`): each room has a token bucket of capacity `600` refilling at `600` messages/hour; when empty, the room is blocked for 10 minutes and joins/messages close with `1013`.
- `DAY_BUDGET` (`1600000`): total accepted messages per UTC day across the hub; above the budget, every message and join closes with `1013` until the UTC day changes.
- At any limit, the client backs off for up to 10 minutes, and the other levels of the ladder carry on.
- Organizations that want an unbudgeted relay of their own can deploy the same bundle (`org_relay_worker.js`, no `vars`) from Biruni's one-click dialog; redeploy Biruni's own relay with `scripts\deploy_relay.ps1`.
