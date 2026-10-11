"""Constants for the EchoMuse Spotify integration."""

DOMAIN = "echomuse_spotify"

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

# What this integration asks the account for.
#
# `user-read-private` identifies the account and its product type (Premium is
# required for any playback control at all). The two playback scopes are what
# make this integration possible: it plays by TRANSFERRING playback to a
# Spotify Connect device, exactly as a phone does, so it cannot work without
# permission to do that.
#
# This is deliberately WIDER than the controller's own Web API connection
# (`em_spotify_web.SCOPES`, read-only), and the difference is the whole point
# of the two existing separately: the controller's token cannot control a
# player, and this one must. Keep them apart — widening the controller's
# scopes to share one token would hand playback control to every code path
# that only needed to resolve a name.
SCOPES = [
    "user-read-private",
    "user-read-playback-state",
    "user-modify-playback-state",
]

# Spotify's device registry is a cloud resource and the only way to learn
# that an Echo has gone away, so it is polled. 30s is a compromise: fast
# enough that a device appearing is noticed before somebody gives up, slow
# enough to stay well inside the rate limit with several entities.
SCAN_INTERVAL_SECONDS = 30

CONF_CLIENT_ID = "client_id"
