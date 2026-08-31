# tibbers

## Register

**Product.** A tool with one job. Design serves the task; there is no audience
to persuade and nothing to market.

## What it is

A macOS skin picker for League of Legends. It reads the champion you hover
from the client's own API, shows that champion's skins, and applies the one you
pick to the running game.

It never modifies the League installation. That is the defining constraint, not
a detail: Riot's client verifies its own files and repairs anything changed, so
every approach that touches the install was ruled out by experiment. Only the
running game process is hooked.

## Users & purpose

One user: the person who built it. Plays on a MacBook Pro with an external
monitor.

**The job:** choose a skin during champ select without it becoming a task.

**The window:** champ select runs 30-60 seconds and the decision competes with
runes, summoner spells, and lobby chat. The picker is not the main event; it
gets a few seconds of attention at most.

**Original context, now partly solved:** League was in exclusive fullscreen, so
using the picker meant alt-tabbing under time pressure. Switching League to
borderless on the external display means the picker can live permanently on the
built-in screen, visible without stealing focus. Design for glanceable and
peripheral, but keep every action reachable fast enough to survive the
alt-tab case.

## Primary task per screen

One screen, one decision: **which skin, and which chroma.** Everything else
(download progress, patcher state, elevation mode) is status that must be
legible without being asked for, and must never occupy the space the decision
needs.

## States that actually occur

- League not running (most of the time the app is open)
- Client up, not in champ select
- Hovering a champion, mods still downloading
- Champion locked, skins ready
- Skin queued, patcher watching
- Game running, skin applied
- Failure: no mod for that skin, patcher error, elevation refused

The download and patcher states are the ones with real duration (about two
seconds and about one second), so they need honest progress rather than a
spinner.

## Brand personality

Precise, quiet, unbranded. It is a personal instrument, not a product with a
logo. It should feel like it belongs beside the game without pretending to be
part of it.

## Anti-references

- **Generic dark SaaS dashboard.** Rounded cards, muted grays, sidebar, Inter
  everywhere. The first version was close to this; moving away is explicit.
- **Skin-mod tool aesthetic.** Discord-adjacent purple, badge clutter,
  exclamation marks.
- **Game overlay.** Semi-transparent HUD panels, glows, angular sci-fi frames.
  It is a desktop app, not a HUD.

## Design principles

1. **The art is the information.** Players recognise skins by their splash, not
   their name. Imagery is the primary index; text confirms.
2. **Status is ambient.** Downloads, patcher state and elevation are always
   visible, never modal, never in the way of the grid.
3. **Unavailable is visible, not hidden.** A skin with no mod stays on screen,
   dimmed, so absence is legible rather than mysterious.
4. **Nothing irreversible is one click away.** The only privileged action is
   the patcher, and what it will run is printed before it runs.

## Accessibility

Single user, no mandate, but: contrast at or above 4.5:1 for anything read,
full keyboard reachability for the pick flow, and `prefers-reduced-motion`
honoured. Chroma choice must never be conveyed by colour alone, since several
chromas are near-identical hues.
