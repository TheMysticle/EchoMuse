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

## Asking for music

Home Assistant's sentences for this are specific, and `in` and `on` are not
interchangeable:

- "play *X* **on** *entity name*"
- "play *X* **in** *area*"
- "play *X*" — the area you are speaking in
- "play the **album** *X* on *entity name*"

**"play music in bedroom" only works if Bedroom is an area.** If it is the
device's name, say "on bedroom". A mismatch is reported as *"I did not catch
that"*: the sentence matched no intent at all, which looks like this
integration failing and is not.

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

### Shuffle, repeat, and similar-song continuation (0.1.2)

The device's librespot engine already supports shuffle, repeat-one and
repeat-all. Companion 0.1.2 exposes HA's standard `shuffle_set` and `repeat_set`
services and shows the current modes for the active Connect player. Values
from playback on a different speaker are not shown on an idle speaker.
These controls change the mode without transferring playback, starting a
paused player, replacing its playlist, or changing its volume.

Update this integration and `custom_sentences/en/echomuse_spotify.yaml`, then
restart HA. With **Prefer handling commands locally**, say:

- “Shuffle” / “shuffle off” / “enable shuffle on Studio speaker”.
- “Repeat this song” / “repeat the playlist” / “turn off repeat”.
- “Repeat this album in the Kitchen”.

The commands require an unambiguous, exposed Spotify player with an active
or paused session in the requested area. Explicit player names use HA's
normal matching. Bare “repeat” and “repeat that” remain speech requests;
“play Repeat” and “play Shuffle” remain searches. No native handler is replaced.

**Autoplay** is the similar-song continuation after the original context ends.
librespot already resolves Spotify's autoplay context on the device and follows
the account's autoplay preference; EchoMuse leaves that preference unchanged.
Enable autoplay in Spotify's settings and turn repeat off to allow a playlist
to finish and continue into recommendations. There is no documented Spotify
Web API autoplay toggle, so this companion does not invent a private endpoint
or force autoplay on every account. Autoplay needs a valid album/playlist
context; normal EchoMuse track playback already retains the album context.
No `em-spotify` or `server` rebuild is required for this HA update.

The HA tests simulate the cloud API and verify mode changes, context retention,
failure handling and existing media/alarm commands. Actual recommendation
availability and playlist-end playback still require verification on a satellite
with a live Spotify account.

### Local voice control (0.1.1)

Copy `custom_sentences/en/echomuse_spotify.yaml` into HA's
`config/custom_sentences/en/`, replace this integration folder, and restart
Home Assistant. Keep your current conversation agent and enable **Prefer
handling commands locally**. Assign the satellite and players to HA areas;
use “on Studio speaker” for an explicit player or “in the Kitchen” for an area.

“Play music”, “play some music”, “continue”, and “put on music” are complete
sentence aliases for HA's native resume intent. They resume the existing
queue and retain HA's target selection and last-paused tracking. There is no
artist search or queue replacement. A direct `play_media` call with generic
text such as `music` also resumes on the selected Spotify device.

Named searches use a separate adapter because HA normally defers catalogue
searches to the LLM during its local-intent pass. The adapter uses HA's native
target matcher and native search/play handler. It handles an unambiguous,
exposed EchoMuse Spotify player locally; other targets retain native behavior
and the usual LLM fallback. No built-in intent handler is replaced.
After an account unloads, a dormant adapter keeps the installed sentences
delegating safely to HA until the account reloads. When uninstalling the
integration, remove its sentence file too.

Longer requests stay searches: “play Please Don't Stop the Music”, “play Music
by Madonna”, and “play the artist Music” do not become transport commands.
“Please don't stop the music” does not issue a stop or resume command.

The reproducible HA simulation is `tools/experiments/ha_command_regression.py`.
It compares commands before and after both sentence files are installed and
checks native media/appliance services, ringing alerts, multiple players,
LLM-local handling, and unloading the integration. Spotify's API is simulated;
microphone transcription and actual cloud ranking require a real device test.

### Search relevance

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

For multiword names, small connecting-word differences share the exact-match
tier: “Beauty on the Beat” can retain Spotify's earlier “Beauty And A Beat”
result instead of discarding it in favor of a later literal match. A small
transcription error in one long word is a fallback when the other meaningful
words match. Single-word titles are not fuzzy-matched, and explicit artist
and media-type requests remain significant. This uses Spotify's returned
candidates and order; it cannot reproduce the phone app's personalization or
select a song absent from the API response. It adds no search API calls.

## What it deliberately does not do

- **No catalogue tree.** The media button opens a root with a **search
  box**, which is the way in. It does not list your playlists or saved
  albums — that would need `playlist-read-private` and `user-library-read`
  and a re-authorisation, so it is not done by default. Queues are not
  managed here either.
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
