---
name: fpl-oracle
description: A weekly FPL decision printed as a pools coupon — paper, print ink, one spot colour, biro for your marks.
colors:
  paper: "#f4f1ea"
  paper-deep: "#e9e4d8"
  ink: "#161513"
  ink-soft: "#55524b"
  rule: "color-mix(in oklab, #161513 28%, #f4f1ea)"
  rule-strong: "color-mix(in oklab, #161513 55%, #f4f1ea)"
  spot: "#1f4fb4"
  spot-deep: "#173d8c"
  on-spot: "#f4f1ea"
  biro: "#2a5bd7"
  stamp: "#c8102e"
typography:
  display:
    fontFamily: "Archivo Variable, Archivo, Helvetica Neue, Arial, sans-serif"
    fontSize: "1.375rem"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "0.025em"
    fontVariation: "'wdth' 78"
  figure:
    fontFamily: "Archivo Variable, Archivo, Helvetica Neue, Arial, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "0.01em"
    fontVariation: "'wdth' 78"
    fontFeature: "tabular-nums"
  head:
    fontFamily: "Archivo Variable, Archivo, Helvetica Neue, Arial, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "0.08em"
    fontVariation: "'wdth' 78"
  tab:
    fontFamily: "Archivo Variable, Archivo, Helvetica Neue, Arial, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "0.06em"
    fontVariation: "'wdth' 78"
  row:
    fontFamily: "Archivo Variable, Archivo, Helvetica Neue, Arial, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "normal"
    fontVariation: "'wdth' 78"
    fontFeature: "tabular-nums"
  body:
    fontFamily: "Archivo Variable, Archivo, Helvetica Neue, Arial, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "normal"
    fontVariation: "'wdth' 100"
  pinned:
    fontFamily: "Archivo Variable, Archivo, Helvetica Neue, Arial, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "normal"
    fontVariation: "'wdth' 100"
rounded:
  none: "0"
  mark: "50%"
spacing:
  s-1: "0.25rem"
  s-2: "0.5rem"
  s-3: "0.75rem"
  s-4: "1rem"
  s-6: "1.5rem"
  s-8: "2rem"
  s-12: "3rem"
components:
  stub-action:
    backgroundColor: "{colors.spot}"
    textColor: "{colors.on-spot}"
    typography: "{typography.tab}"
    rounded: "{rounded.none}"
    padding: "0.75rem 1.5rem"
  stub-action-hover:
    backgroundColor: "{colors.spot-deep}"
    textColor: "{colors.on-spot}"
  textbtn:
    backgroundColor: "transparent"
    textColor: "{colors.spot}"
    typography: "{typography.tab}"
    rounded: "{rounded.none}"
    padding: "0.25rem 0"
  textbtn-hover:
    textColor: "{colors.spot-deep}"
  tab:
    backgroundColor: "transparent"
    textColor: "{colors.ink-soft}"
    typography: "{typography.tab}"
    rounded: "{rounded.none}"
    padding: "0.75rem 1rem"
  tab-hover:
    textColor: "{colors.ink}"
  tab-selected:
    textColor: "{colors.spot}"
  box:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.biro}"
    rounded: "{rounded.none}"
    size: "1.75rem"
  box-hover:
    backgroundColor: "color-mix(in oklab, #2a5bd7 8%, #f4f1ea)"
  stamp:
    backgroundColor: "transparent"
    textColor: "{colors.stamp}"
    typography: "{typography.head}"
    rounded: "{rounded.none}"
    padding: "0.25rem 0.75rem"
  stamp-on-spot:
    textColor: "{colors.on-spot}"
  strip:
    backgroundColor: "{colors.spot}"
    textColor: "{colors.on-spot}"
    rounded: "{rounded.none}"
    padding: "0.75rem 1rem"
  coupon-head:
    backgroundColor: "{colors.paper-deep}"
    textColor: "{colors.ink}"
    typography: "{typography.head}"
    rounded: "{rounded.none}"
    padding: "0.5rem 0.5rem"
  coupon-row:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.row}"
    rounded: "{rounded.none}"
    padding: "0.5rem 0.5rem"
  coupon-row-hover:
    backgroundColor: "color-mix(in oklab, #1f4fb4 6%, #f4f1ea)"
  bench-band:
    backgroundColor: "{colors.paper-deep}"
    textColor: "{colors.ink}"
    typography: "{typography.head}"
    rounded: "{rounded.none}"
    padding: "0.25rem 0.5rem"
  mark-vice:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.mark}"
    size: "1.25rem"
  mark-captain:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    rounded: "{rounded.mark}"
    size: "1.25rem"
  unprinted:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink-soft}"
    typography: "{typography.body}"
    rounded: "{rounded.none}"
    padding: "2rem 1.5rem"
    width: "60ch"
---

# Design System: fpl-oracle

## Overview

**Creative North Star: "The Pools Coupon"**

The site is this week's football pools coupon: a sheet of warm paper, set in
black print ink, ruled with hairlines, numbered down the left, with boxes you
mark in biro and a stub you hand in. One spot colour — coupon blue — is what
the printer had on the press that week, so it appears on the header strip, the
stub button, the open tab, the filled tally cells and the pitch drawing, and
nowhere else. Red is not a palette colour; it is the rubber stamp the clerk
banged on the sheet, and it says two things only: when the coupon closes, and
who cannot play.

Density is printed density, not app density. Hairline rules do all the work
that a card and a shadow would do elsewhere: a `rule-strong` line under a
section head, a `rule` line under every coupon row, a dashed tear line above
the stub. There is no elevation anywhere on the page, no gradient fill, no
rounded container, no shadow. A figure never floats; it sits in a column under
a printed head with its moment pinned underneath it ("As of run 12 Sept 04:11
UTC").

The world is deliberately the opposite of the category. FPL tools are dark
pitches of shirt badges over neon stat cards; this is a paper document that
tells you what to do, shows you what it read, and admits what it does not
know. Nothing on the page is predicted; every number is a thing that was true
at a stated time.

**Key Characteristics:**
- Warm paper ground, black print ink, no dark mode.
- One spot colour, used structurally, never decoratively.
- Biro blue for everything the user marks; red for the stamp only.
- One variable type family, printed condensed (78% width) or set at text
  width (100%), on a fixed rem scale.
- Zero radius, zero shadow, zero gradient fill. Hairline rules instead.
- Tabular numerals everywhere; every figure pinned to its moment.

## Colors

A printed palette: one paper, one ink, one press colour, one pen, one stamp.

### Primary
- **Coupon Blue** (`spot`): the single colour the coupon was printed in. It
  carries the header strip, the open tab's underline and label, the "Hand in
  at FPL" stub, text buttons and links, filled tally cells, the pitch line
  drawing, and the text selection highlight. Its darker sibling **Press Blue**
  (`spot-deep`) exists only as the hover/pressed state of those spot surfaces.
  **Paper on Spot** (`on-spot`) is the only text colour permitted on a spot
  ground.

### Secondary
- **Biro Blue** (`biro`): what *you* wrote, never what the printer set. It is
  the drawn X in a coupon box, the caret, the focus ring, and the faint wash a
  box takes on hover. If the user did not put it there, it is not biro.

### Tertiary
- **Stamp Red** (`stamp`): the clerk's rubber stamp. Two jobs, both about
  being unable to act: the countdown/CLOSED stamp in the header, and a player
  who cannot play (Injured / Suspended / Out, and the strike-through rule on
  their name). It is never a chart colour, never a "bad number" colour, never
  a price fall.

### Neutral
- **Coupon Paper** (`paper`): the page ground and the ground of every row,
  box and panel. There is no white anywhere.
- **Second Stock** (`paper-deep`): the darker band of the same stock. Column
  heads, the "Bench, in order" band, and any zone that is structurally a head
  rather than content.
- **Print Ink** (`ink`): all primary text, the coupon box border, the tally
  outline, the captain/vice marks.
- **Soft Ink** (`ink-soft`): secondary print — row numbers, pinned notes,
  price change, closed tab labels, an unavailable row's body text. It is ink
  diluted, deliberately warm; it is never a neutral grey.
- **Hairline** (`rule`) and **Heavy Rule** (`rule-strong`): the coupon's only
  structure. Hairline separates rows; heavy rule separates sections, sits
  under column heads, and is the dashed tear line above the stub.

### Named Rules
**The Who-Printed-It Rule.** Spot blue is the printer's; biro blue is the
user's. A colour choice is settled by asking who put the mark on the paper. A
checkbox X is biro. A filled tally cell is spot, because the coupon was
printed with those backers already counted.

**The Stamp Rule.** Red appears only on the closing stamp and on a player who
cannot play. It never marks a falling price, a warning, a low score, or a
destructive button. A price change prints in soft ink and lets the sign (+ /
−) carry the direction.

**The No-Grey Rule.** Every neutral is mixed from ink and paper, so the page
stays warm. Never introduce a pure grey, a pure white, or a pure black.

## Typography

**Type:** Archivo Variable (self-hosted, width axis only), falling back to
Archivo, Helvetica Neue, Arial, sans-serif. There is one family. There is no
display face and no mono face; the variation comes from the width axis.

**Character:** A workmanlike grotesque run at two widths. At **78% width** it
is print setting: column heads, coupon rows, figures, tabs, buttons, the pitch
labels — anything that would have been set to fit a narrow column on a real
sheet. At **100% width** it is running text: body copy, checklist detail,
pinned notes. Weight does the emphasising (400 / 600 / 700); size is fixed to
a seven-step rem scale and is never fluid or clamped.

### Hierarchy
- **Display** (700, 1.375rem, condensed, uppercase, +0.025em): the product
  name in the header strip. One per page.
- **Figure** (600, 1.125rem, condensed, tabular): a printed number — bank,
  team value, points, the entry number. Always sits directly under a head.
- **Head** (600, 0.75rem, condensed, uppercase, +0.08em): the printed column
  head. Perm-line labels, section heads ("This week's changes", "The
  fifteen"), table column heads, the bench band, and inline run-in labels
  ("After" / "Marked").
- **Tab / Action** (600–700, 0.8125–1.125rem, condensed, uppercase, +0.06em):
  the tab bar, text buttons, the stub action, the stamp.
- **Row** (400, 0.9375rem, condensed, tabular): coupon row text. The player
  name inside the row goes to 600.
- **Body** (400, 0.9375rem, text width, line-height 1.4): checklist labels and
  prose. Capped at 60ch where it runs long.
- **Pinned** (400, 0.75rem, text width, soft ink): the moment a figure was
  true, or the caveat under an action. Never larger than this.

### Named Rules
**The Two-Widths Rule.** If it is printed on the coupon — a head, a row, a
figure, a button — it is condensed at 78%. If it is something to read — a
sentence, a detail, a caveat — it is at 100%. Nothing else on the width axis.

**The Pinned Figure Rule.** No figure appears without its moment. Every number
either carries a `pinned` note under it or sits in a block whose pinned line
states the run time and prices-as-of. Nothing predicted is printed at all.

**The Tabular Rule.** Tabular numerals are on globally. Do not turn them off
for "nicer" proportional figures; the coupon's columns must align down the
page.

## Layout

A single centred sheet: `max-width: 72rem` (max-w-6xl) with 1rem side padding,
the header strip and footer spanning full width behind it. The vertical rhythm
is the fixed spacing scale (0.25 / 0.5 / 0.75 / 1 / 1.5 / 2 / 3rem); do not
invent intermediate values.

Desktop (≥768px) is a 12-column grid: the checklist in columns 1–4, the
fifteen (rows or pitch) in columns 5–12 spanning both rows, the stub under the
checklist in column 1–4 of the second row. Gaps are 2.5rem across, 2rem down.
The perm line is a five-up definition list above the grid, separated by a
heavy rule.

Phone (<768px) is one column in story order: header strip, perm line (two-up),
checklist, the fifteen, stub. The recorded responsive trade-offs are rules,
not accidents:

- **Column heads pin on desktop only.** The table's `thead` is sticky so the
  heads stay while the fifteen scroll. On a phone the sheet scrolls as one
  document and nothing is sticky.
- **The phone folds columns under the name.** Club, price change and the
  backers tally move out of their own columns and print on a second line under
  the player's name; the row becomes two lines. Cell padding tightens to
  0.25rem and the tally cells narrow to 5×10px with 1px gaps.
- **The tab bar is one scrolling row on a phone**, never wrapped and never a
  select. A mask fade at the right edge is what says it scrolls.
- **The pitch redraws, it does not scale.** On a phone the drawing switches to
  a 360×520 viewBox (from 600×420) so the 11–12 unit labels land at roughly
  11–12px on screen instead of shrinking with the box.

### Named Rules
**The One Sheet Rule.** The page is one document, not a stack of panels. Do
not introduce a second scroll container, a sidebar that scrolls
independently, or a sticky footer bar. The only sticky element in the system
is the desktop column head.

## Elevation & Depth

**There is no elevation.** No `box-shadow` exists anywhere in the system, and
none may be added — not for hover, not for focus, not for a "raised" button,
not as a subtle ambient lift. Paper does not float above paper.

Depth is done three ways, all of them printed:
1. **Rule weight.** Hairline (`rule`) separates peers; heavy rule
   (`rule-strong`) separates sections and sits under heads; a 2px dashed
   heavy rule is a tear line.
2. **Second stock.** A band in `paper-deep` marks a zone that is structure
   rather than content (column heads, the bench band).
3. **Ink strength.** Primary ink for content, soft ink for secondary and for
   anything struck out or unavailable.

### Named Rules
**The Flat Paper Rule.** If a surface needs to feel separate, give it a rule
or a second-stock band. Never a shadow, never a border-radius, never a
gradient fill.

## Shapes

Radius is zero, globally and in the shadcn bridge (`--radius: 0rem`,
`--radius-sm/md/lg/xl: 0`). Every box, band, panel, button and input is a
square-cornered rectangle.

There is exactly one circle in the system: the **captain / vice mark**, a
1.25rem circled letter with a 1.5px ink stroke (filled with ink and reversed
to paper when it is the captain). It is a circle because a coupon's marks are
circled letters, not because roundness is available. The pitch drawing's
player discs (r=16) are the same device on the drawing.

Borders are the form language: 1px hairline rules, 1.5px on the coupon box and
the marks, 2px on the stamp (plus a 1px outline at 2px offset, which is the
double-ruled stamp edge), 2px dashed on the tear line and 1px dashed on an
unprinted section's frame.

### Named Rules
**The Square Corner Rule.** Radius is 0 everywhere. The only exception is a
printed circled letter (the C/V marks and the pitch discs). If a new component
wants a rounded corner, it wants to be a different product.

## Components

### Header strip
The only full-bleed spot-blue surface. Carries, left to right: the product
name (display, uppercase), the gameweek and closing time (condensed uppercase,
90% opacity), and the countdown stamp pushed right. Links inside it inherit
paper-on-spot. There is one strip per page.

### The stamp (countdown)
The signature. Condensed 700 uppercase in stamp red, a 2px red border plus a
1px red outline offset 2px (the double rule of a rubber stamp), rotated
−2.5°. On the spot strip it reverses to paper-on-spot (`.stamp.on-spot`). It
re-stamps once a minute in whole units (`3d 4h`, `4h 05m`, `12m`) and reads
`Closed` past the deadline. Use it only for the closing clock; do not stamp
anything else.

### Coupon box (checkbox)
A 1.75rem square, 1.5px ink border, paper ground, with a real `<input
type="checkbox">` underneath it at zero opacity so keyboard and screen readers
get the standard control; the visible label names it via `aria-labelledby`.
- **Hover:** an 8% biro wash on the paper.
- **Checked:** the biro X is *drawn*, two strokes, 220ms
  `cubic-bezier(0.2, 0.8, 0.2, 1)`, second stroke delayed 120ms. This is the
  only authored motion in the system.
- **Focus:** 2px biro outline, 2px offset.
- **Disabled:** dashed heavy-rule border, default cursor, no X.

### Checklist
An unstyled ordered list, one hairline-ruled row per decision: box on the
left, then a 600-weight label, a soft-ink detail line, and an after-state line
prefixed by a run-in head that reads **After** before marking and **Marked**
after. The after-state is a preview of what the coupon says once you have
done it; never omit it, and never show a predicted outcome in it.

### Coupon rows (the fifteen)
A collapsed-border table, condensed, one hairline under each row.
- **Column heads:** second stock, condensed uppercase 0.75rem, heavy rule
  under, sticky on desktop.
- **Row hover:** a 6% spot wash.
- **Row numbers and secondary cells:** soft ink at 0.8125rem.
- **Bench band:** a full-width second-stock row reading "Bench, in order",
  set as a head, heavy rule under.
- **Unavailable row (`.out`):** the whole row drops to soft ink and the name
  takes a line-through in stamp red.
- **Price change:** printed in the row's secondary ink with the sign carrying
  direction (`+0.1` / `−0.1`). Never red, never green.

### Tally
The only drawn element on the coupon: one 6×12px cell per contributing
creator, 1px ink outline, filled spot when that creator backs the player,
2px gaps, with a numeral beside it. It is a printed count, not a chart — do
not replace it with a bar, a percentage or a progress track, and never scale
it to a proportion. It carries an `aria-label` of "N of M creators back this
player".

### Tabs
Printed section headings, not a control surface. Condensed uppercase 0.8125rem
in soft ink, 3px transparent bottom border pulled up over the bar's heavy
rule; hover goes to full ink; the open tab goes spot with a spot underline.
150ms ease-out on colour and border only. One scrolling row on a phone with an
edge mask.

### Stub
The hand-off. A 2px dashed tear line across the top, then the one full-spot
action on the page: `Hand in at FPL`, condensed 700 uppercase with a 16px
inline SVG external-link icon, 0.75rem/1.5rem padding, no radius, hovering to
press blue. Under it a pinned caveat, then the ENTRY NO. block (head + figure
+ pinned note) that identifies the run the sheet was printed from. There is
one stub per page and it is the primary action.

### Text button
A spot-ink condensed uppercase underlined label (underline offset 0.2em) with
no border or background, hovering to press blue. Every secondary action in the
system is this — the pitch/rows toggle is the built example. There is no
secondary or ghost *filled* button; the spot fill belongs to the stub alone.

### Printed pitch
An alternate view of the fifteen, not a decoration: a spot-ink line drawing
(1.5px strokes, no fill) of the pitch, with each player as a paper disc with
an ink stroke carrying their coupon row number, the name printed beneath in
condensed 600. The captain's disc is filled ink with a paper numeral. The
bench sits below on a dashed rail labelled BENCH. Names wider than their slot
are squeezed with `textLength`/`lengthAdjust="spacingAndGlyphs"` — condensed
further, like tight print — and never clipped or truncated.

**Recorded decision:** coupon rows are the default view of the fifteen on both
desktop and phone; the pitch is the toggle. The rows *are* the table, and the
coupon's own form wins over the category's pitch.

### Unprinted (empty state)
A 1px dashed heavy-rule frame, 2rem/1.5rem padding, soft ink, capped at 60ch,
holding a head and one sentence. This is how a section that has not been built
yet appears. Do not use a spinner, a skeleton shimmer, or a placeholder card.

### Browser surfaces
The parts nobody draws still carry the design and must not be reset:
selection is paper-on-spot, the caret is biro, the focus ring is a 2px biro
outline at 2px offset, the scrollbar is heavy-rule on paper, links are spot
with a 1px underline at 0.18em offset, and numerals are tabular globally.

## Do's and Don'ts

### Do:
- **Do** print anything structural — head, row, figure, button, tab — in
  condensed setting (`wdth 78%`) and anything readable at text width (100%).
- **Do** give every figure a head above it and a pinned note beneath it
  stating when it was true.
- **Do** separate with a rule: hairline between peers, heavy rule between
  sections, 2px dashed for a tear line.
- **Do** use spot blue for what the printer set (strip, open tab, stub, tally
  fills, pitch lines, links) and biro blue for what the user marks.
- **Do** keep red for the closing stamp and for a player who cannot play, and
  let a sign (`+` / `−`) carry direction everywhere else.
- **Do** build new tabs from the existing vocabulary — head, figure, pinned,
  rule-b / rule-strong-b, coupon rows, tally, box, textbtn, unprinted —
  before inventing a class.
- **Do** inline any icon as SVG from lucide-react at 16px with
  `strokeWidth 2.25` and `aria-hidden`, and only where a word cannot do the
  job.
- **Do** label provisional, predicted or untimed data as such, and print
  nothing predicted at all.
- **Do** guard any motion with `prefers-reduced-motion: reduce`, resolving to
  the finished state.

### Don't:
- **Don't** add a shadow, an elevation, a glow, or a "lifted" hover. There are
  none in the system.
- **Don't** add a border-radius. Zero everywhere; the circled C/V mark and the
  pitch discs are the only circles.
- **Don't** use a gradient as a fill, a background, or an accent. The one
  gradient in the build is a mask on the phone tab row that fades the scroll
  edge; that is a mask, not a surface.
- **Don't** wrap content in a card. A section is a head, a rule, and rows.
- **Don't** introduce a second accent colour, a chart palette, a success
  green, or a warning amber. The palette is closed.
- **Don't** colour a price change, a delta or a score red or green.
- **Don't** use pure white, pure black or a neutral grey.
- **Don't** add a second type family, a second axis, a fluid/clamped size, or
  a size outside the seven-step rem scale.
- **Don't** use an icon font, an emoji or a glyph character as an icon, and
  don't put an icon where a printed word works.
- **Don't** write a kicker or eyebrow above a heading. Labels are printed
  column heads above a value or run-in before a line; they are never a
  decorative tag over a title.
- **Don't** animate anything but the biro X. No page transitions, no fades on
  mount, no skeleton shimmer, no number count-ups; state changes are 150ms
  ease-out on colour only.
- **Don't** add a dark mode. The world is a sheet of paper.
