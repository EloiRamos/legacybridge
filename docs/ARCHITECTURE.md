# Architecture

## Two systems, one HTTP boundary

```
legacy-erp container (Java 8)                         console container (Python 3.12)
┌────────────────────────────────────┐   HTTP only   ┌──────────────────────────────────────────┐
│ GatewayFilter → Servlets E1–E8     │◀──────────────│ bridge/client.py  LegacyClient (E1–E8)   │
│ OrderBook (1 JDBC tx, 1 write lock)│               │ bridge/normalize.py (pure) · models.py   │
│ SQLite file inside the container   │               │ service.py BridgeService · DraftStore    │
│ /_sim/* control plane (demo/tests) │◀── C1–C4 ─────│ SimControlClient (reset, S6/S7, tests)   │
└────────────────────────────────────┘               │ assistant/ scenarios · tools · agent     │
                                                     │ ui/ Streamlit: 5 views                   │
                                                     └──────────────────────────────────────────┘
```

**Boundary rules.** These rules are enforced by `tests/test_boundaries.py`:
- No shared volume, database or code. `src/legacybridge` never imports `sqlite3`, never opens `*.db` files and contains no JDBC URL.
- `httpx` is imported only in `bridge/client.py`. Legacy wire formats are parsed only there, and raw values are interpreted only in `bridge/normalize.py`.
- Import direction: `ui → service, assistant`; `assistant → service`; `service → bridge`.
- `LegacyClient` reaches E1–E8 only. The submission and re-check code never references the simulator and never reads fault state.
- Compose has no `volumes:`, and both published ports bind to `127.0.0.1`.

## Ownership

| Java (HALDEN) | Python (LEGACYBRIDGE) |
|---|---|
| Records, schema, seed; validation; numbering; allocation | HTTP, tickets, read retries, pagination |
| Wire formats, codes, auth, rate limit, faults | Normalization, workflows, approvals, evidence, UI, tools |

## Data uncertainty

- **One identity.** Every product view is built from one complete catalogue snapshot: all E4 pages without `q`/`cat`, grouped by canonical SKU before any filter. A duplicate SKU or a price conflict is therefore visible everywhere.
- **Strict stock evidence.** Availability always comes from one complete E5 extract read **without** `item`, and the extract is validated in full before any SKU is selected:
  - A wrong header, a wrong field count, an empty SKU or an invalid warehouse makes the whole extract malformed, so no verdict is given for any SKU.
  - A negative allocated quantity makes that SKU `UNDETERMINED`.
  - An affected SKU never receives `IN_STOCK` or `OUT_OF_STOCK`, and an affected draft is `BLOCKED`.

## Evidence taxonomy

| Outcome | Evidence | Meaning |
|---|---|---|
| CONFIRMED | READ_BACK | E8 acknowledged an order and E7 matches the approved account, reference and lines |
| CONFIRMED | ACKNOWLEDGED | E8 acknowledged (`rc 0` is sent only after commit), but the read-back obtained no answer |
| CONFIRMED | RECONCILED | After an uncertain E8, exactly one order carries the reference and matches |
| REJECTED | LEGACY_REJECTION | HTTP 200 with `rc ≠ 0`, or 409 `SHORT` (nothing changed) |
| NOT_APPLIED | NOT_SENT | Pre-send preparation failed, or no connection could be made |
| NOT_APPLIED | PRE_HANDLER_REJECTION | E8 answered 401/403/440/429 before any business logic |
| UNKNOWN | NO_MATCH_AFTER_UNCERTAIN | No order with the reference is visible yet. This does not prove it was not created |
| UNKNOWN | AMBIGUOUS_MATCH | Two or more references, or a sole candidate that does not match |
| UNKNOWN | READBACK_MISMATCH | The acknowledged order is missing or differs |
| UNKNOWN | RECONCILIATION_FAILED | The search or detail read failed |

Classification takes two phases:

1. **Phase 1** classifies the single E8 observation: not delivered, pre-handler refusal, rejection, SHORT, acknowledged, or uncertain.
2. **Phase 2** depends on Phase 1:
   - **2a** reads back the acknowledged order.
   - **2b** reconciles an uncertain request by reference.

A null `Ref` is a non-match and is tested before trimming. Price, total, date and status are not compared: the booked total is reported, not verified.

## Draft lifecycle

```
             Approve (atomic consume)                     outcome
  READY ─────────────────────────────▶ SUBMITTING ──────────────────▶ CONFIRMED          (View order)
    │                                     │                    ├────▶ REJECTED_BY_ERP    (terminal)
    │ Reject                              │                    ├────▶ NOT_APPLIED ──Approve N+1──▶ SUBMITTING
    ▼                                     │                    │          └──Reject──▶ REJECTED_BY_USER
  REJECTED_BY_USER ◀── Discard ── BLOCKED │                    └────▶ UNKNOWN ──Re-check──▶ CONFIRMED | UNKNOWN
```

- `eligible` is fixed when the draft is prepared. The lifecycle changes only through these transitions.
- **Atomic approval.** Under one `DraftStore` lock, the service:
  1. checks the state is `READY` or `NOT_APPLIED`;
  2. checks `attempt_no == len(attempts) + 1`;
  3. appends the attempt, which consumes the approval;
  4. sets `SUBMITTING`.

  Only then does any I/O happen. A concurrent or stale approval fails with no I/O.
- `Approval` is constructed only in `ui/components.approve_draft`. The tools never create one.
- **Re-check anchoring.**
  - If an attempt was acknowledged as order X, every re-check reads only E7 for X. It never searches E6 and never confirms a different order Y in X's place.
  - A failed re-check never erases the anchor.
  - The reference search applies only to attempts that were uncertain at E8.

## The late-commit case on one timeline

```
operator      console (BridgeService)                     HALDEN
   │ Approve attempt 1 ─▶ consume approval (lock)
   │                      prepare_write: E2? + E3 acct ───▶ 200
   │                      E8 POST /halden/orders (1) ─────▶ [delay 2.5 s … then commit SO-240111]
   │                      ReadTimeout after 1.0 s  ◀─ ─ ─ ─ (no reply)
   │                      E6 acct=417: no ref LB-… ───────▶ 200  → UNKNOWN / NO_MATCH_AFTER_UNCERTAIN
   │ ◀── "Outcome unknown — Re-check only" (E8 count 1)
   │ Re-check (3 s later) E6 acct=417 (2 pages) ──────────▶ 200  one order with ref LB-…: SO-240111
   │                      E7 SO-240111 ───────────────────▶ 200  account, ref, lines match
   │ ◀── CONFIRMED / RECONCILED SO-240111 (E8 count still 1)
```

The observed values of a recorded run are shown in the README.

## Limits of the design

- Drafts and attempts live in process memory, and no persistence service is added. A crash while `SUBMITTING` leaves no record of whether the E8 was sent; the operator must then search HALDEN for the draft reference.
- Every operation reads the full item master (12 records here). At production catalogue sizes, a group-aware ERP query or a synchronized replica would need a separate design decision.
