# EchoMuse Spotify

**Ask Home Assistant to play music by name on a Spotify Connect speaker.**

One media player entity per Spotify Connect device on your account, and it
advertises the one feature that makes *"play Bohemian Rhapsody on the bedroom
echo"* work.

Built for [EchoMuse](https://github.com/wilbowes/EchoMuse) Echoes running
librespot, but it is not specific to them: any Spotify Connect speaker on the
account gets an entity.

> Spotify **Premium** is required. Playback control is Premium-only on
> Spotify's side.

## Why this exists

Home Assistant's `HassMediaSearchAndPlay` intent — the one behind "play
*something* on *a speaker*" — only matches a media player that advertises
**both** `SEARCH_MEDIA` and `PLAY_MEDIA`.

- Home Assistant's own **Spotify** integration is a media player but does not
  advertise `SEARCH_MEDIA`, so Assist has nothing to resolve a name with.
- An **EchoMuse Echo**'s entity cannot advertise it either, and that is a
  protocol limit rather than a decision: the Echo is presented to HA as an
  ESPHome device, and the ESPHome API carries no search message at all.
  Advertising the feature with no transport behind it would be a control that
  appears to work.

This fills that gap and nothing else. It talks only to Spotify — no EchoMuse
controller endpoint, no extra credentials, nothing to keep in step with an
EchoMuse release.

## Install

**HACS** → Integrations → ⋮ → Custom repositories → add
`https://github.com/wilbowes/EchoMuse` as an **Integration**.

**By hand** → copy `custom_components/echomuse_spotify/` into your Home
Assistant `config/custom_components/` folder, then restart.

## Set up

1. Create an app at
   [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
   and tick **Web API**.
2. Under **Redirect URIs** add exactly:
   ```
   https://my.home-assistant.io/redirect/oauth
   ```
   This is the address Home Assistant uses while the *My Home Assistant*
   integration is enabled (it is, by default). It redirects back to your own
   instance, so nothing is exposed to the internet.
3. Under **User Management**, add the Spotify account you will play on.
   Development mode serves five listed accounts and nobody else.
4. Copy the **Client ID**. There is **no client secret** — this uses PKCE,
   so none is needed and none is stored.
5. Settings → Devices & Services → **Add Integration** → *EchoMuse Spotify*.

## What you get

| | |
|---|---|
| **Entities** | One media player per Spotify Connect device on the account. |
| **Voice** | "Play *X* on *speaker*", via HA's own search-and-play intent. |
| **Controls** | Play, pause, next, previous, seek, volume, `play_media` with a Spotify URI or share link. |
| **State** | Title, artist, album, artwork, position. |

A device absent from Spotify's registry shows as **unavailable** rather than
being removed — removing it would break automations that name it.

## How it chooses what to play

Spotify returns what *matched*; this decides what you *meant*. Three rules:

- **Popularity is never consulted.** It would make the same words resolve to
  different songs week to week, and a request that worked yesterday failing
  today is worse than one that never worked.
- **Deterministic.** The same words always give the same answer.
- **Live, karaoke, remix and sped-up versions are demoted, never dropped.**
  "creep" gets the studio take; "creep live" gets the live one; a song that
  only exists live still appears.

Accents and punctuation are ignored, so *Beyoncé* and *Beyonce* are one
request, as are *Don't Stop Me Now* and *dont stop me now*.

## What it deliberately does not do

- **No browsing.** It resolves a name to something playable and stops.
  Playlists, queues and a catalogue tree are a different feature.
- **No scopes beyond playback.** It asks for `user-read-private`,
  `user-read-playback-state` and `user-modify-playback-state`, because
  transferring playback is impossible without the last one.
- **Nothing leaves your Home Assistant.** Search results come from Spotify
  and go no further.

## Troubleshooting

**A speaker is missing.** Entities exist only for devices Spotify *currently
reports*. Cast to it once from the Spotify app and it appears within 30
seconds. An EchoMuse Echo additionally needs the engine installed, Spotify
Connect enabled, and its controller link up.

**"Spotify refused the authorisation."** Check the Client ID, and that the
redirect URI above is registered on the app exactly.

**Nothing found for a song you know exists.** Development mode serves only
the accounts on the app's User Management list, and search returns at most 10
results.

**Home Assistant cannot find a player.** The intent needs a player
advertising both search and play. If HA's own Spotify integration is also
installed, name the speaker explicitly.

## Licence

MIT, as EchoMuse. Playback on an Echo uses librespot, an unofficial client
Spotify's terms do not permit; use an account you can afford to lose.
