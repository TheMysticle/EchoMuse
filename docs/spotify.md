# Spotify on your Echo

Your Echo can be a **Spotify Connect speaker**: it appears in the Spotify app
alongside your phone and your TV, and when you pick it the Echo fetches the
music itself. There is no Music Assistant in the middle and nothing streams
through the controller.

A second, optional piece — a small Home Assistant integration — lets you ask
by voice: *"play Caroline Polachek on the bedroom echo"*.

> **Early Access, and it uses an unofficial client.** Playback is
> [librespot](https://github.com/librespot-org/librespot), which Spotify's
> terms do not permit. Use an account you can afford to lose. Spotify
> **Premium** is required.

**Requirements**

- An Echo running **emOS**. Not FireOS — see [why](#why-emos-only) below.
- Spotify Premium.
- Firmware v2.18.0-ea.1 or newer, and a controller from the same era.

---

## Part 1 — Make the Echo a Spotify Connect speaker

### Build the engine

The playback engine is a separate binary, not part of the firmware. It is
~4 MB and would double the size of every firmware update across both A/B
slots whether or not Spotify changed, which is the same reason the wake word
models are not in there either.

```sh
cd device/spotify && ./compile.sh
```

That produces `device/spotify/build/em-spotify`. It is checked as it is
built: static, `ET_EXEC`, ARM. Each of those fails silently on the hardware
if it is wrong, so the build refuses rather than handing you something that
will not run.

### Install it

1. Dashboard → **Integrations** → upload `em-spotify`.
2. Each Echo's row shows whether it can run it and what it currently has.
   Press **Install**.
3. Device → **Config** → stage **08 Spotify** → turn on **Spotify Connect
   speaker**, and Save.

The engine starts as soon as it is installed — no reboot, no toggling. If you
upload a *newer* engine later, the running one is replaced automatically.

The toggle is greyed out on an Echo that cannot run it, and says which of the
two reasons applies rather than simply refusing.

### Play to it

Open Spotify on your phone, tap the device picker, choose the Echo. It is
listed under whatever the Echo is called in EchoMuse.

That is the whole of it. From then on:

- **A voice turn ducks the music** rather than pausing it, and it comes back
  at the same point. The duck happens on the Echo itself, so it lands with
  the listening ring instead of a network round trip later.
- **Volume is one number.** The Spotify app's slider, Home Assistant, the
  physical buttons and the LED ring all show the same thing.
- **Home Assistant's own music still wins.** If HA plays something to the
  Echo, Spotify stands aside until it finishes.

### Settings

| Setting | What it does |
|---|---|
| **Spotify Connect speaker** | Runs the engine. Off by default: it opens a listening port and an mDNS record, and needs your own Premium account. |
| **Show the ring on a Spotify volume change** | Lights the volume arc when the *phone* moves the volume. On by default. Home Assistant and controller volume changes stay silent — nobody is at the device, and a ring lighting unprompted reads as a glitch. |

---

## Part 2 — Playing by voice

Asking for music **by name** needs the Spotify Web API, because something has
to turn "Caroline Polachek" into a thing to play. That is what the Home
Assistant integration in `custom_components/echomuse_spotify/` does.

It is optional. Casting from the Spotify app needs none of it.

### Why a separate integration

Home Assistant has an intent for this — `HassMediaSearchAndPlay` — and it
matches a media player only if it advertises **both** `SEARCH_MEDIA` and
`PLAY_MEDIA`.

- **HA's own Spotify integration** is a media player, but does not advertise
  `SEARCH_MEDIA`. Assist has nothing to resolve a name with.
- **The Echo's EchoMuse entity cannot either**, and that is a protocol limit
  rather than a choice. The controller presents each Echo to HA as an ESPHome
  device, and the ESPHome API has no search message at all — 443 messages in
  the protocol, none of them search-shaped. Advertising the feature with
  nothing behind it would be a control that appears to work.

So the integration fills exactly that gap, and nothing else.

### What it is

One Home Assistant media player per **Spotify Connect device on your
account** — your Echoes, and anything else Spotify can see. It plays by
transferring playback to the device, exactly as your phone does.

It talks **only to Spotify**. No controller endpoint, no EchoMuse
credentials, nothing to keep in step with a controller release.

### Set it up

**1. Create a Spotify app** at
[developer.spotify.com/dashboard](https://developer.spotify.com/dashboard).

- Tick **Web API**.
- Under **Redirect URIs** add exactly:

  ```
  https://my.home-assistant.io/redirect/oauth
  ```

  That is the address Home Assistant uses while the *My Home Assistant*
  integration is enabled, which it is by default. It redirects back to your
  own instance, so nothing has to be exposed to the internet.

- Under **User Management**, add the Spotify account you will play on.
  Development mode serves **five** listed users and nobody else.
- Copy the **Client ID**. There is **no client secret** — this uses PKCE, so
  none is needed and none is stored anywhere.

**2. Install the integration.** Copy `custom_components/echomuse_spotify/`
into your Home Assistant `config/custom_components/` folder and restart HA.

**3. Add it.** Settings → Devices & Services → **Add Integration** →
*EchoMuse Spotify* → paste the Client ID → approve on Spotify's page.

**4. Ask for something.** The sentences Home Assistant recognises for this
are specific, and `in` and `on` are not interchangeable:

| Say | Matches |
|---|---|
| "play *X* **on** bedroom echo" | the **entity** called that |
| "play *X* **in** bedroom" | an **area** called Bedroom |
| "play the album *X* on bedroom echo" | as above, restricted to albums |
| "play *X*" | the area the Echo you spoke to is in |

So **"play music in bedroom" only works if Bedroom is a Home Assistant
area**. If Bedroom is the device's name, say "play music **on** bedroom". A
mismatch here is reported as *"I did not catch that"* — the sentence matched
no intent at all, which looks like the integration failing and is not.

Assigning each player to an area is worth doing anyway: it makes "play *X*"
work in the room you are standing in.

### An Echo only appears once Spotify has seen it

The integration can only create an entity for a device **Spotify currently
reports**. So the Echo must have the engine installed, the setting on, and
have been cast to at least once so it has registered with Spotify. If an Echo
is missing, cast to it from the phone once and it will appear within 30
seconds.

An Echo that goes offline shows as unavailable rather than disappearing —
removing the entity would break any automation naming it.

### How it chooses what to play

Spotify returns what *matched*; the integration decides what you *meant*.
Three rules, and each is there because the obvious alternative is worse:

- **Popularity is never consulted.** Using it would make the same words
  resolve to different songs week to week, and a request that worked
  yesterday failing today is worse than one that never worked.
- **The same words always give the same answer.** Every comparison is
  explicit and the sort is stable.
- **Live, karaoke, remixes and sped-up versions are pushed down, never
  removed.** Asking for "creep" gets the studio recording; asking for "creep
  live" gets the live one; and if a song only exists as a live recording you
  still get it.

Accents and punctuation are ignored, so "Beyoncé" and "Beyonce" are the same
request, and so are "Don't Stop Me Now" and "dont stop me now".

### What it does not do

- **No catalogue tree.** The media player card's media button opens a root
  with a **search box** — that is how you search from the UI. It does not
  list your playlists or saved albums; that needs two more read scopes and a
  re-authorisation, so it is not done by default. Queues are not managed
  here either.
- **Nothing is sent anywhere else.** Search results come from Spotify to your
  Home Assistant and stop. No catalogue data is sent to any other service.

---

## Why emOS only

The engine is built against **musl**, not Android's bionic, and that decision
is forced by DNS.

bionic resolves names through Android's property service, which emOS does not
provide — so an Android-toolchain build has no working DNS on an emOS Echo.
emOS writes `/etc/resolv.conf` at boot and musl reads it. On FireOS the
reverse is true: there is no such file and `/etc` is a symlink into read-only
`/system`.

Rather than ship two builds, the feature is gated on the Echo reporting
`emos`. A FireOS Echo keeps playing music the controller feeds it, which
works fine — it simply is not a Spotify Connect speaker.

---

## When something is wrong

**The Echo is not in the Spotify app's device list.**
Check the Integrations tab: the Echo's row says what it has and what state the
engine is in. The engine only runs while the controller link is up, so an
Echo whose controller is down disappears from Spotify.

**"Engine not installed on this Echo."**
Upload `em-spotify` on the Integrations tab and press Install on that Echo.

**"Spotify Premium is required."**
A free account cannot be a Connect target. The engine reports this and stops
rather than retrying.

**The integration found nothing for a song you know exists.**
Development mode serves only the five accounts on the app's User Management
list, and search is limited to 10 results. Check the account you are playing
on is listed.

**"I did not catch that."**
The sentence matched no intent — nothing to do with the integration. Almost
always `in` versus `on`: "play music **in** bedroom" needs Bedroom to be an
**area**, while "play music **on** bedroom" targets an entity by name. See
the table above.

**Home Assistant says it cannot find a player.**
The intent needs a player advertising both search and play, and it needs
exactly ONE match. If you have both this integration and HA's own Spotify
integration, or two players in the same area, name the one you mean.

**You cannot browse from the media player card.**
There is nothing to browse: this resolves a name to something playable rather
than holding a catalogue. The card's media button opens a root with a
**search box**, which is the way in. Listing the account's own playlists and
saved albums would need two more read scopes and a re-authorisation; it is
not done by default.

**Checking what is actually running on an Echo**, over the dashboard console:

```sh
md5sum /proc/$(pidof em-spotify)/exe
```

That is the engine the Echo is *executing*, which is the only one that
matters. If `readlink /proc/$(pidof em-spotify)/exe` ends in `(deleted)`, a
newer engine was installed and the old process is still running — restart it
by toggling the Spotify setting off and on.

---

## Where the parts live

| Path | What |
|---|---|
| `device/spotify/` | The engine: librespot wrapped so the firmware drives it. Rust. |
| `device/cmd/spotify.go`, `device/internal/spotify/` | The firmware side: engine lifecycle, the shared music slot, status. |
| `controller/em_spotify_assets.py` | Storing and installing the engine binary. |
| `controller/em_spotify.py` | Which commands and URIs may reach a device. |
| `custom_components/echomuse_spotify/` | The Home Assistant integration. |

Technical detail for each is in `device/CLAUDE.md` (the engine's six
load-bearing constraints) and `controller/CLAUDE.md`.
