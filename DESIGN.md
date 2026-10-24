# CeRebel Playground design system (draft, 2026-10-24)

Direction: a golfer's yardage book, not a sci-fi dashboard. Cool paper, pine-green ink, and one red flag that marks the single fix. The thing people remember is their own swing drawn as a skeleton with the number on it. Everything around it stays quiet.

This draft is not based on reference sites (UXKIN isn't connected). It comes from the product's real output (OpenSim skeleton, joint angles, one main fix) and the vocabulary of golf: the range, the pin flag, the scorecard.

## Color

| Token | Value | Role |
|---|---|---|
| `--paper` | `#F1F3EF` | Page background (cool, slightly green; not warm cream) |
| `--paper-2` | `#E4E8E2` | Sunken areas: scrubber track, inputs |
| `--ink` | `#16241D` | Text, skeleton lines, primary button fill |
| `--ink-2` | `#4C5A52` | Secondary text (5.9:1 on paper) |
| `--line` | `#C8D0C9` | Rules and borders, the ground line under the skeleton |
| `--turf` | `#2F6B45` | Links, focus ring, active phase on the scrubber |
| `--flag` | `#D9402A` | Only the fix: the angle arc, the fix metric, error text. Never decoration |
| `--lime` | `#D8F25A` | Brand mark only (logo dot). Not used in UI |

No dark mode on the consumer page for now (people use it outdoors on phones). `?dev=1` keeps its existing dark theme.

## Typography

One family: **Archivo** (Google Fonts, variable width 62–125, weights 400–800).

| Step | Size / line height | Use |
|---|---|---|
| Display | clamp(44px, 7vw, 96px) / 0.95, wdth 112, weight 800, tracking -0.02em | Hero headline only |
| H2 | 32px / 1.1, wdth 100, 700 | Section titles |
| H3 | 22px / 1.25, 650 | Fix title, plan names |
| Lead | 20px / 1.5, 400 | Hero sub |
| Body | 17px / 1.55, 400 | Everything else |
| Small | 14px / 1.45, 500 | Captions, legal |
| Figure | tabular-nums, wdth 75, 700 | Every measured number (57°, 12 cm) |

Sentence case everywhere. No all-caps labels, no eyebrows, no monospace.

## Spacing and shape

4px base: 4, 8, 12, 16, 24, 32, 48, 72, 112. Sections are separated by 112px on desktop and 72px on phones. Content width 1160px; text columns at most 62ch.
Radius: 6px on inputs and buttons, 0 on the stage. No shadows; depth comes from the ground line and `--paper-2`.

## Components

- Primary button: `--ink` fill, paper text, 48px tall. Hover lightens to `#24382D`. Focus is a 2px `--turf` ring with a 2px offset. Disabled is 40% opacity.
- Secondary: a text link in `--turf`, underlined on hover.
- Scrubber: the swing phases (Address to Follow-through) as a row of words above a thin track. The active phase is `--turf`. It is keyboard operable with the arrow keys.
- Fix note: a title, the figure in `--flag`, then why, the feel cue and the drill as plain paragraphs. No card box.
- Navigation: the ink logo on the left, Sign in on the right. That's all.

## Imagery and motion

- The only illustration is real data: the skeleton from a processed swing, drawn as 2.5px `--ink` lines with round joints, plus the shadow on the ground.
- Motion happens once: on load, the swing plays from address to the top, the elbow arc draws in `--flag`, and the number appears. After that it only moves when the person drags the scrubber or presses Replay. With `prefers-reduced-motion`, the page shows the top of the backswing as a still frame.
- Icons: none needed.
