---
version: 1
slug: "web-src-app-tsx"
primary_target: "web/src/App.tsx"
related_targets: ["web/src/tabs/MySquad.tsx"]
---

# Surface brief — My squad tab (planning state), app shell

Scope: the app shell (header, four tabs) and the My squad tab in its
planning state, built as the reference surface. Visitor mode: Operate.
The other three tabs and the live state are the owner's to build against
this system; they inherit it.

Audience and job: the owner, on a phone at the deadline or a desktop in
the week, deciding this gameweek's changes. Task: read the recommended
action, tick it off as done in the FPL app, see the fifteen with
availability, backers and price. Content: the latest run record (real
GW4 data as fixture). Constraints: read-only; never writes to FPL;
provisional/predicted/untimed are labelled; no predicted points.

Memorable moment: marking a checklist box leaves a biro X; the stub at
the foot is the hand-off.

Unresolved: whether the pitch panel or the coupon rows is the default on
desktop (spec says pitch by default with a table toggle; the coupon's
rows ARE the table — decide in build, record in DESIGN.md).

## Direction contract

THESIS: The squad is this week's pools coupon — numbered rows, boxes to
mark, a stamped cut-off, a stub to hand in. It refuses the category's
arrangement: a dark pitch of shirt icons over neon-accented stat cards.

OWN-WORLD: Coupon paper ground (warm off-white), black print ink, one
spot colour (coupon blue) for rules, column heads, the header strip and
the stub; biro blue for the user's marks; red only for the rubber stamp
and unavailable flags. One condensed grotesque family with tabular
figures, two weights, fixed rem scale. Hairline rules and numbered rows;
boxes with square corners; no shadows, gradients, or rounded cards.
Backers rendered as a filled-box tally — the only drawn element.

STORY: The visitor sees the cut-off first, then what to do (the checklist
with after-state previews), then who's in and how backed they are; they
mark what they've done and hand in at FPL via the stub.

FIRST VIEWPORT: Spot-blue header strip: fpl-oracle · GAMEWEEK N · COUPON
CLOSES <day time UTC>, countdown as a rubber stamp at the right. Perm line
beneath: BANK · FREE TRANSFERS · CHIPS LEFT as printed column heads with
values. Desktop: checklist column left (4/12), the fifteen as numbered
coupon rows right (8/12) with pinned column heads; stub bottom-left,
pitch toggle bottom-right. Phone: header, perm line, checklist, rows,
stub, stacked. Primary action: the stub "Hand in at FPL" — the only
element at full spot colour and full weight.

FORM: The Pools Coupon; my ordered list position 4 (the roll); seed key
9bd095db. Raises kept from the declined hand: after-state preview before
marking (darkroom); one item outranks all (cracktro); one graphic owns
the field (Factory sleeve); column heads pinned (rain garden); every
figure pinned to its moment (deep dive).

FINISH: unreviewed and undocumented is unfinished; this build ends with
the finish review, the verdict, DESIGN.md, and every shipping raster
carrying its provenance.
