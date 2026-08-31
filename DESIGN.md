# DESIGN.md

Visual decisions for tibbers, and why. See PRODUCT.md for who it is for.

Everything here is what `tibbers/static/index.html` actually ships. When the
two disagree the code is right and this file is stale; say so rather than
changing the code to match a document.

## Theme

Dark, committed. The scene that settles it: one person at a desk at night, a
game filling the external monitor, this window on the laptop screen beside it.
A light UI beside a dark game is a flashlight in the eye. Dark is not the tool
default here, it is the ambient condition.

## Colour

**Strategy: committed.** Splash art carries 50-70% of the skin page, so the
palette's job is to stay out of its way and stay legible over anything. The
chrome is near-black with a single gold accent; the art supplies every other
colour on that page. The build and counter pages sit over the same splash,
dimmed and blurred, because they are mostly small numbers and the art would
only compete with them.

Neutrals are tinted blue (hue 265), the accent is warm (hue 85). The two are
not on the same hue: the chrome reads as cold and the gold as the one warm
thing in the window, which is what keeps a 10% accent legible at 10px.

```
--void    oklch(0.13 0.010 265)   ground
--raised  oklch(0.22 0.016 265)   raised chrome: frames, tile backs
--rule-d  oklch(0.28 0.022 80)    hairlines between regions
--hair    oklch(0.165 0.010 264)  hairlines between rows inside a table

--gold    oklch(0.80 0.095 85)    accent: selection, focus, progress
--gold-b  oklch(0.90 0.070 88)    the brighter gold, for text on dark
--gold-d  oklch(0.52 0.060 85)    the dim gold, for resting borders

--ink     oklch(0.97 0.005 85)    primary text
--ink-mid oklch(0.80 0.010 85)    secondary
--ink-low oklch(0.64 0.012 85)    labels and column headings

--live    oklch(0.74 0.13 155)    healthy status
--warn    oklch(0.66 0.17 30)     failure, and a sample too thin to trust

--ctl     oklch(0.16 0.012 264)   every small control at rest
--ctl-on  oklch(0.20 0.020 90)    the same control chosen: gold-lit, not gold

--silver  oklch(0.72 0.020 255)   augment rarity
--gld     oklch(0.79 0.100 85)
--prism   oklch(0.76 0.100 320)
```

Gold is used at under 10% of surface: selection frames, focus rings, the tab
underline, progress, and the top tier band. Never as a fill behind text,
never as gradient text.

Rarity is the one place hue carries meaning of its own, and it is never the
sole carrier: it rides a hairline on the augment's icon, every row names its
rarity in its title attribute, and the filter chips say it in words.

`--rift` is a radial gradient, not a colour. The chroma icons the client
ships are transparent RGBA cutouts of the champion model, so every surface
showing one needs a ground or it composites onto black. It is a lit haze
rather than a flat fill, tinted 45% by the chroma's own colour so that
near-identical models stay distinguishable without disappearing behind a
block of colour.

## Typography

Two families on a contrast axis:

- **Beaufort for LoL** for champion names, skin names, and anything that is
  prose. Riot's own face, which is what makes the surface read as adjacent to
  the game rather than as a mod tool. Proprietary: fine locally, not
  redistributable.
- **ui-monospace** for every number, id, status line, and column heading.
  Data that should look like data. Always `font-variant-numeric: tabular-nums`
  where figures sit in a column, so the digits line up.

The scale is not a ratio ladder. It is two bands with a gap between them,
which is the honest description of a window this dense:

| | |
|---|---|
| `clamp(26px, 3.4vw, 42px)` | skin name, the only display-scale element |
| 15px | body default |
| 18px | the empty-state headline |
| 11-13px | row names, item names, champion names in a table |
| 8-10.5px | every mono figure, label, column heading and status line |

Nothing sits between 18px and 26px, and nothing between 15px and 18px. The
compact breakpoint (`max-height: 620px`, `max-width: 780px`) drops the skin
name to `clamp(19px, 4.6vw, 26px)` and shrinks the rail, because the picker
window is small on purpose and must not simply be the full layout squeezed.

Every number is labelled where it sits. The percent sign travels with its
figure, drawn smaller beside it, rather than living in a column heading; the
game count sits under the rate it qualifies. A column of figures still reads
as a column, and no figure depends on remembering a legend at the top.

## Motion

Two easings, both tokens:

```
--out-quart cubic-bezier(0.165, 0.84, 0.44, 1)   entrances, anything arriving
--inout     cubic-bezier(0.645, 0.045, 0.355, 1) things that travel: the tab
                                                 underline, the carousel
```

Durations cluster at 160ms for hover and colour changes, 180-220ms for
something appearing, 260-340ms for something moving across the window. The
outliers are deliberate: the splash crossfade is 420ms because it is the one
moment worth covering, and its 1400ms scale settle is a drift rather than a
move. Nothing else is above 500ms.

Where motion is spent:

| Moment | Treatment |
|---|---|
| Splash changes | Two stacked layers crossfade, 420ms; scale 1.035 to 1 over 1400ms |
| A view is opened | `viewIn`, 170ms, and the sections inside it stagger 30/65/95ms |
| A fresh rail | tiles rise 260ms, staggered 18ms each, capped at 300ms |
| Tab changes | one underline travels, 220ms; the labels never move |
| Tile hover | `scale(1.02)` and full saturation, 160ms, gated to `hover: hover` |
| Chroma preview | the panel grows 280ms from its own bottom-left corner |
| Status | the lamp breathes while working; progress is a width transition |

The entrance animations are one-shots. A CSS animation restarts every time
its element comes back from `display: none`, so anything that lives inside a
view that is hidden when another tab is open must carry its entrance as a
class that is taken off again, not as a permanent style. Getting this wrong
looks exactly like the page being fetched a second time.

`prefers-reduced-motion: reduce` collapses every animation and transition to
0.01ms and undoes the start state of everything that animates in, so nothing
is left invisible waiting for an animation that will not play.

## Components

**Chroma strip.** Rotated squares with the artwork counter-rotated inside so
it stays upright. The **base swatch is always drawn**, even for a skin that
ships no chromas: it is how you get back to the champion's own look, and a
strip that disappears reads as a missing feature rather than an empty one.
Any skin with chromas ships its own chroma-style icon under its own id, so
the base swatch belongs to the same family as the chromas beside it.
Selection is a gold border **plus** scale, never colour alone, because
several chromas differ only in hue.

**Chroma preview.** A 96px panel beside the name, holding the chosen
variant's own icon. Hovering a disc previews that chroma without choosing it;
hovering the panel scales it 2.7x from its own bottom-left corner, growing up
and right into empty space rather than shoving the name or leaving the
window. The name fades to 25% while it is open. The panel is laid out at the
size it reaches when enlarged and scaled **down** to rest, never up: a
composited layer is rasterised once at its layout size, so growing a 96px
panel was stretching 96px worth of pixels.

**Skin tiles.** Splash art, no label. Unavailable skins stay in place, dimmed
and desaturated, so absence is visible rather than mysterious. No cards, no
nested surfaces.

**Table primitives.** One row shape (`.trow`, `.tic`, `.tnm`, `.tn`, `.tier`)
serves the augment table, the champion tier list, the Arena item lists and
the counters page. Every column heading sits directly over its own figures. A
new table that needs a new row shape is a sign the table is wrong, not that
the primitives are missing one.

**Segmented controls** (`.mode`) and **filter chips** (`.chip`) both carry
their chosen state in `aria-pressed`, and the switches in `aria-checked`.
The state is never only a class, and never only a colour: the switches also
draw a filled or hollow dot, and the chips carry live counts.

**Status.** One line, always present, never modal. Three lamps (client,
library, patcher) plus a message. Progress is a 1px rule, not a spinner,
because both waits have a known duration.

## Rendering rules

Learned by breaking them:

1. **Never rebuild the grid on a poll.** Re-rendering destroys every `<img>`
   and re-decodes it. Grid HTML is rebuilt only when its content signature
   changes.
2. **Selection is an attribute, not a re-render.** Changing the highlight
   toggles `aria-current` on existing nodes.
3. **A poll that changed nothing does no work at all.** The state body is
   compared as text before it is parsed; identical means there is nothing to
   draw. The per-renderer signatures are the second line of defence, not the
   first, because computing them is itself the expensive part.
4. **Anything a view switch has to put away, a view switch must put away.**
   Leaving it to the next poll is a visible lag, and once a poll that changed
   nothing renders nothing, it is not a lag but a stuck element.

Art is served with `Cache-Control: immutable` and memoised server-side.

## Layout

Fixed viewport, no page scroll: this is an app window. Only the guide and the
table bodies scroll, inside themselves.

**On the skin page the splash is the hero.** It fills the window at full
brightness, scrimmed only where type actually sits (bottom-left, top edge).
An earlier version put a large card in the middle; it competed with the art
and made the champion the subject instead of the skin.

- **Naming plate**, bottom-left over the splash: skin name at display scale,
  id and readiness in mono beneath, chroma strip below that.
- **Carousel** keeps the selection centred and slides as one object, arrows
  either side, rather than free-scrolling. The eye returns to the same place
  every time, which is what makes a pick fast.

**On the data pages the splash is a backdrop**, dropped to 35% opacity with a
3px blur under the sheet's own content. The blur sits below the content in
the sheet's stacking context, or it lands on the headings instead of the art.

**The tabs are a property of the mode**, never a constant. The server derives
which tabs a mode has earned from what data it actually has, and a tab that
could only ever render an empty state is not sent at all, because it would
teach that tabs are allowed to be dead. The patch select is chrome for the
build and counter pages and is hidden on the skin page, and in any mode with
no build source at all.

**Per mode, not one layout for all:**

- **Rift and its relatives** have roles, so they get a lane-opponent row, a
  general/matchup switch, and the tallest page in the app: runes and skills
  side by side, then items, with the slot options riding alongside the
  start-and-core row rather than under it.
- **ARAM** has no lanes and no matchup, and spends the room those would take
  on names for the fourth, fifth and sixth item options. An unnamed item is
  the same failure as an unnamed augment; Rift cannot afford the space and
  its six-item core is common knowledge, ARAM's is not.
- **Arena** gets three pages instead of one, because it asks three questions
  and none of them is the footer of another: is this champion worth picking
  (the tier list), which of the three augments on offer to take, and what to
  buy. Augments lead, because they are chosen three times a match and change
  how a champion works. Placement is the measure, not wins: one team of four
  takes first, so a 50% win rate would be absurd.
