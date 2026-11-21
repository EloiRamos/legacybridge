# Demo guide

Start the stack with `docker compose up --build` and open http://127.0.0.1:8501. Use **Reset demo data** in the sidebar (under "Simulation control"; confirm it with the toggle) before each walk-through, so the results are reproducible.

## 1. Featured case: the booking whose reply was lost

This walk-through repeats the recorded failure-recovery run shown in the README, by hand in the console.

1. **Simulation setup (not visible to the integration).** Delay the next order POST for longer than the console's 5 s timeout:
   ```bash
   curl -s -X POST http://127.0.0.1:8090/_sim/faults -d method=POST -d path_prefix=/halden/orders -d mode=delay -d count=1 -d delay_ms=8000
   ```
   (The recorded trace used a 1.0 s client timeout with a 2500 ms delay.)
2. Open **Order Desk** and keep the defaults: customer 00417 Nordkap Marine, BW-1001 × 10. Click **Prepare draft**. The draft is READY, with an estimate of €48.50.
3. Click **Approve attempt 1 and send to HALDEN**. After about 5 s, the outcome is **UNKNOWN / NO_MATCH_AFTER_UNCERTAIN**. The panel shows "E8 requests for this attempt: 1" and **Re-check outcome** is the only action. The unresolved-draft banner appears for the customer.
4. Wait a few seconds, then click **Re-check outcome**. The outcome becomes **CONFIRMED / RECONCILED** with `SO-240111`, the history shows a RECHECK entry with 0 order POSTs, and the attempt still shows 1.
5. Check **Customers & Orders** for 00417: SO-240111 carries the draft reference.

These values were observed in the recorded trace:

| Step | Observation |
|---|---|
| Draft | `LB-07C49519`, READY, estimate €48.50 |
| Order POSTs | 1 (ReadTimeout after 1.0 s) |
| First result | UNKNOWN / NO_MATCH_AFTER_UNCERTAIN; Re-check only |
| Re-check after 3 s | CONFIRMED / RECONCILED SO-240111; still 1 order POST |
| ERP evidence | E7 SO-240111 (417, ref LB-07C49519, BW-1001 × 10, 48.50); E5 BW-1001 DUS allocated 10; E6 exactly one order with that reference |

## 2. Guided scenarios (Assistant → Guided scenarios)

Every scenario is deterministic: no AI model is involved, and it calls the real service against the Java ERP. Scenarios never submit. A pending draft is approved only with the console's own approve button.

| ID | Scenario | Expected on fresh seed data |
|---|---|---|
| S1 | Customer 360 — Nordkap Marine | 1 match; 3 orders; outstanding SO-240102 (SHIPPED) and SO-240103 (OPEN) |
| S2 | Ambiguous account — Alder & Finch | 2 matches, 10871 on credit hold; "Selection required — the console never auto-picks." |
| S3 | Stock truth check | BW-1002 OUT_OF_STOCK + FLAG_MISMATCH; BW-1009 UNDETERMINED (duplicate SKU); BW-1005 UNDETERMINED (known minimum 45); BW-1006 IN_STOCK 1700 (raw `1.200`) |
| S4 | Order with human approval | READY draft, estimate €48.50; approving gives CONFIRMED / READ_BACK SO-240111 |
| S5 | Pre-checks block unsafe orders | Both drafts BLOCKED: credit hold; discontinued; no price; availability undetermined; insufficient stock (requested 5, available 2) |
| S6 | Stale draft rejected by the ERP | The draft is READY while 2 × BW-1010 are free. *Simulation setup:* a counter-sales terminal books 2 × BW-1010 (EXT-000001). Approving gives REJECTED / LEGACY_REJECTION `INSUFFICIENT_STOCK` (`SHORT:BW-1010:0`) |
| S7 | Unreliable ERP write | *Simulation setup:* the next order POST commits and then fails with HTTP 500. Approving gives CONFIRMED / RECONCILED, with exactly one order carrying the reference |

S6 and S7 need simulation control. When it is disabled, they show "Requires simulator control" and no failed steps.

## 3. Optional AI assistant (Mode B, unevaluated)

Mode B is enabled only with a provider key in `.env` (`ANTHROPIC_API_KEY=…`; never commit it). The assistant can read data and prepare drafts, but it cannot submit them. Tool calls are shown as evidence. **Real-model behavior has not been evaluated.** These three fixed prompts are recorded for a later, separate evaluation. They have **not** been run against a real model:

| Workflow | Prompt (unevaluated) |
|---|---|
| A | "Which account should I use for Alder & Finch, and what is outstanding for it?" |
| B | "Can we offer 20 hydraulic hoses (BW-1009) today? Explain the stock evidence." |
| C | "Prepare an order for Nordkap Marine: 10 × BW-1001 and 2 × BW-1004." |
