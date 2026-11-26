# UI mockups: drop your HTML files here

This folder is the hand-off point for the front-end design. Put the HTML files that show how the app
should look here. Claude reads them, maps them onto the real app (`templates/`, `static/`) and builds the UI
with the `/ui-from-mockup` skill.

## How to hand over a design

1. **Copy the files in.** One HTML file per screen or state, named with a two-digit order and a short name:

   ```
   design/mockups/
     01-upload.html            the start page, no file chosen yet
     02-progress.html          decoding a file, steps ticking
     03-result.html            workbook ready, preview grid, downloads
     04-error.html             (optional) failed upload / failed job
     assets/                   images, icons, fonts the mockups use
     01-upload.notes.md        (optional) anything the HTML can't show
   ```

   One file with every screen in it is fine too: say so in a notes file.
   Screenshots (`01-upload.png`) help when a mockup relied on a tool that isn't in the HTML.

2. **Add notes where the HTML can't speak** (optional, `NN-name.notes.md`): hover and click behaviour,
   what's must-have vs. nice-to-have, which parts are placeholders, brand rules, screens that are not
   designed yet.

3. **Tell Claude to start.** In Claude Code, type:

   ```
   /ui-from-mockup
   ```

   or `/ui-from-mockup 03-result.html` to work on one screen. Claude first writes a plan to
   `design/ui-plan.md` (screen map, design tokens, gaps, questions) and waits for your OK before changing
   the app.

## Rules for mockup files

- **Placeholder data only.** No real client names, table names, measures or numbers from a client file:
  this folder is committed to git. If a mockup has real data, put it in `design/mockups/private/`
  (ignored by git) instead.
- External links (CDN scripts, Google Fonts, images on the web) are fine in the mockup, but the real app
  runs offline and loads nothing from the internet: Claude copies what's needed into `static/` or swaps it
  for a local equivalent, and lists it in the plan.
- Scripts inside mockups are read, never run.
