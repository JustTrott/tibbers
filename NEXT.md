# Next

What is actually open. The two workstreams this file used to describe are
both shipped; the log at the bottom says where they went.

---

## Open

### The window is 620x470 and the tallest page only just fits

Rift fills the sheet exactly, and it only fits because the item options ride
alongside the start-and-core row instead of under it. Anything added to that
page pushes something off the bottom. The choice is still the one the old
version of this file named and never made: either the window grows, or the
Rift page gives something up. Worth deciding before the next thing is added
to it, not during.

### Nothing exercises the pages in champ select

Every screen can be reached from the mock client, which is what made the
rare states designable. Nothing checks that they still render: a typo in a
renderer is found by opening the page and looking. A handful of assertions
driven through `/api/mock` -- hover, lock, each queue, each tab, no console
errors -- would cover the cases that actually break.

### The picker polls while nobody is looking at it

`/api/state` every 900ms for as long as the app is open, including while the
picker window is closed and there is no League running. A poll that changes
nothing now costs one string compare rather than a full render, so this is no
longer expensive, but it is still a request a second forever. Pausing on
`document.hidden` is the obvious move; the reason it has not been done is
that a hidden pywebview window may not report itself hidden, and a picker
that stops watching champ select would be worse than the poll.

### Nothing says tibbers is running until lock-in

Between launch and lock-in the app is a tray icon and nothing else. Someone
who has just installed it, or whose picker is set to open on lock, has no
way to tell whether it is watching champ select or not there at all. Worth
deciding when a window should be on screen before the lock and what it says
-- the phase line the settings page already has, the champion being hovered,
or just "watching" -- without it being one more thing over the client.

### Skin sharing: tibbers users in one lobby seeing each other's skins

Today a chosen skin exists only on the machine that chose it. Two tibbers
users in the same lobby could see each other's picks if their apps agreed on
a channel and each built the other's skin into its own overlay. Open
questions before any of it is built: how the apps find each other (the lobby
chat, a rendezvous server, or nothing), what is sent (a skin id, never a
file -- the other side builds from its own install), and the consent and
ban-risk story, since it widens what the injector loads.

### A minimise button on the pick screen

The picker is frameless, so it has no minimise control: it can be closed to
the tray or left floating over the client, nothing in between. A minimise
button beside the close would let it drop to the taskbar and come back with
the champion still selected. On Windows this touches the parked-position
fix (a minimised window must not be remembered as its position), so the two
have to be done together.

### `--demo` and `--mock` overlap

`dev.sh --demo <id>` fills the picker with one champion's real skins;
`--mock` replays the whole client. `--demo` predates the mock client and is
now a narrow special case of it, kept because it needs no interaction.
Whether it earns its own flag is worth asking next time either is touched.

---

## Done

**The build and counters pages, redesigned** -- augments are a ranked table
with names and a derived tier rather than anonymous icons, every figure is
labelled where it sits, Arena gets three pages of its own, and each mode
gets only the tabs it has data for.

`405a84c` Arena builds from op.gg · `c894c1d` tabs per mode ·
`ec50ee2` augments ranked by placement · `e527c0b` one set of row primitives ·
`5a78219` the sheet's blur, and your own rank in the counters list

**Development while the user is playing** -- a dev instance that cannot touch
the live one, a patcher that survives a restart, a phase check that says
whether this is a good moment, and a UI-only deploy that restarts nothing.

`1854813` phase.sh · `72f010d` the patcher outlives the app ·
`76a9d95` deploy without taking the screen · `f80ebdd` dev.sh as the default ·
`260f4f6` reload without restarting · `b4fc63c` the bundle is a frozen snapshot

**The mock client moved out of the picker** into its own window at `/mock`,
so the developer tool is no longer drawn over the interface being judged.
