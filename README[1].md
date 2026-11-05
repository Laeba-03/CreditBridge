# CreditBridge — static demo

Single-file app, no build step, no backend. Edits, publishes and signatures persist in the browser (localStorage) so the full loop demos end to end.

## Deploy
- **Vercel / Netlify:** drag this folder in, or `npx vercel --prod`.
- **GitHub Pages:** push, enable Pages on the branch root.
- **Anything else:** serve `index.html` as a static file.

## Routes (hash-based, so no server config needed)
- `#/deals` — officer queue
- `#/deals/nw-2041` — Deal Card (draft, demo editing + publish gating)
- `#/deals/al-2039` — live deal
- `#/deals/pc-2031` — signed deal (locked)
- `#/offer/<id>` — borrower page (only works once a deal is published)
- `#/audit` — audit log

## Demo script (≈90s)
1. Click **Claude not connected** → Copy → watch "Claude is linked" spring in.
2. Open Northwind → Publish is disabled (unreviewed fields). Edit the approved limit to ₹55,00,000 → reason prompt → strikethrough + amber provenance.
3. **Confirm all Claude values** (⌘⏎) → Publish → stepper fills to Live.
4. **Open offer page** → move the slider → type name, tick, sign → Signed. Back on the deal, the stepper is locked at Signed and the audit log has the row.
5. **Reset demo** in the top bar restores seed data.

## Wiring to the real MCP
Replace the simulated handshake in `openConnect()` with a poll of your `/w/<workspace>/status` endpoint, and replace `SEED.deals` with `GET /deals`. Field shape is documented inline in `SEED`.
