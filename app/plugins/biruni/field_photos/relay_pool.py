"""Relay pool definitions and deterministic rendezvous-hashing subset selection."""
import hashlib

LEGACY_NOSTR = (
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net",
    "wss://nostr.mom",
    "wss://relay.nostr.band",
)

# The pool every new link uses (scripts/probe_relays.py on 2026-11-18: each passed a publish -> second-socket
# receive). damus, nostr.band and cdn.satellite.earth failed that probe: they stay only as legacy anchors, which
# old links and PCs still use. Probe again before changing this list.
NOSTR = (
    "wss://nos.lol",
    "wss://relay.primal.net",
    "wss://nostr.mom",
    "wss://relay.snort.social",
    "wss://relay.nostr.net",
    "wss://offchain.pub",
    "wss://nostr.bitcoiner.social",
    "wss://yabu.me",
    "wss://nostr.red5d.dev",
    "wss://nostr.rblb.it",
)

NOSTR_CANDIDATES = (   # failed the 2026-11-18 probe (timeout, refused or full); not used
    "wss://relay.damus.io",
    "wss://relay.nostr.band",
    "wss://cdn.satellite.earth",
    "wss://nostr.oxtr.dev",
    "wss://nostr-pub.wellorder.net",
    "wss://nostr.chaima.info",
)

LEGACY_MQTT = (
    "wss://broker.hivemq.com:8884/mqtt",
    "wss://broker.emqx.io:8084/mqtt",
    "wss://test.mosquitto.org:8081/mqtt",
)

MQTT = LEGACY_MQTT + ("wss://broker-cn.emqx.io:8084/mqtt",)   # passed the 2026-11-18 probe

MQTT_CANDIDATES = (
    "wss://mqtt.eclipseprojects.io/mqtt",   # timed out on 2026-11-18
)

K = {
    "nostr": {"phone": 3, "host": 5, "scan": 8},
    "mqtt": {"phone": 2, "host": 3, "scan": 5},
}


def ranked(topic, urls):
    """Return `urls` sorted by SHA-256(topic + '|' + url) hex, descending."""
    return sorted(
        urls,
        key=lambda u: hashlib.sha256(f"{topic}|{u}".encode("utf-8")).hexdigest(),
        reverse=True,
    )


def subset(topic, urls, k):
    """Return the top `k` ranked URLs for `topic`."""
    return ranked(topic, urls)[:k]


def format_hosts(hosts):
    """Format up to 4 relay hostnames followed by +n when more are connected."""
    items = [str(h) for h in (hosts or ()) if h]
    if not items:
        return ""
    shown = ", ".join(items[:4])
    return f"{shown} +{len(items) - 4}" if len(items) > 4 else shown
