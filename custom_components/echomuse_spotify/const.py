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
    # Browsing the account's own library. Read-only, and the reason they are
    # here rather than omitted: a browse root with nothing in it renders as
    # "No items", which is worse than no browse button at all.
    "playlist-read-private",
    "user-library-read",
]

# Spotify's device registry is a cloud resource with no push, so it is
# polled. Two requests per tick, ONCE for every entity — the coordinator
# fetches and they all read it — so this is 12 requests a minute whether
# there is one Echo or six.
#
# 10s rather than 30s because the poll is the only thing that notices a
# change made ELSEWHERE: the volume moved on the Echo's own buttons, or in
# the Spotify app. At 30s that lagged visibly. Our own commands do not wait
# for it — they are shown at once and the poll confirms.
SCAN_INTERVAL_SECONDS = 10

CONF_CLIENT_ID = "client_id"
