# Speaker groups

Play the same Spotify on several Echoes at once, in sync, as mono or as a
left/right pair. Set it up in the dashboard under **Settings → Speaker groups**.

**What you do:** make a group, add the Echoes, pick Mono, Left or Right for
each, then choose the Echo the dashboard names in Spotify. Every Echo in the
group plays.

## What it needs

- The Echo that leads must be on **emOS** with Spotify **linked**
  (Settings → Integrations). The others can be any Echo with updated firmware,
  including FireOS ones.
- Echoes on the same network as the controller.
- Nothing else. Music Assistant is not involved.

## How it works

One Echo of the group leads. It plays its own Spotify and sends the same audio,
stamped for a moment about two seconds ahead, to the others over the Sendspin
protocol the Echoes already speak. Each Echo plays a sample at the instant its
stamp says, against a clock it keeps matched to the leader's.

- The controller picks the leader. It is the first Echo in the group that is
  online with Spotify linked. A pinned preference wins. If the leader goes
  away, another takes over after 30 seconds, and the dashboard shows the new
  name to choose.
- Choose the **leader's name** in Spotify. Choosing another Echo of the group
  plays on that Echo alone.
- Pause, skip and stop reach every Echo at once. Starting a song adds about half
  a second. A skip or seek made on the phone reaches the other Echoes about two
  seconds later, because that audio was already on its way.
- The leader's volume is the group's: the others follow it. Changing a
  member's volume changes only that Echo.
- If no other Echo is reachable, the leader plays alone, exactly as it does
  without a group.

## When something else plays on a group's Echo

A Sendspin server you point at an Echo yourself, such as a TV streamer or
Music Assistant, takes that Echo from the group, including the Echo that
leads it.

- On the leader, Spotify pauses, and the group stops with it.
- The other Echoes leave the group and play the TV. The dashboard shows them
  as playing something else.
- When the TV disconnects, each Echo rejoins the group within about five
  seconds. Spotify stays paused until you press play.

An Echo that is playing its own Spotify, chosen by itself in Spotify, is not
taken by the group. Home Assistant's own media player still wins over all of
these.

This needs the updated firmware on every Echo in the group. With older
firmware on either side, the group and the TV take the Echo from each other.

## Security

The controller makes a random secret for each leader and member pair and gives
it to both. The secret is not logged and the controller does not keep it. The
link between them is Noise `KKpsk2`, the one Sendspin uses. A member that
loses its secret is given a new one.

## When the group is not ready

The dashboard says why for each Echo: offline, needs a firmware update, not
reachable by the leader, or playing something else (it rejoins when free).

## Not measured

Written without Echo hardware. Sync accuracy, CPU use and behaviour on a lossy
WiFi link have not been measured, and the repo's UAT on two or more Echoes is
still owed.
