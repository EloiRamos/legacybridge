# Engineering decisions

## ADR-1 — Java 8, embedded Tomcat, no WAR

The legacy system targets the platform it would plausibly have been written for: Java 8 servlets, JDBC and SQLite. It starts embedded Tomcat 9 (Servlet 4.0, `javax.servlet`) from `main()`.
- The JUnit contract tests start exactly the same bootstrap, so servlet registration cannot drift between tests and runtime.
- There is one artifact and no application-server administration.

**Rejected:** Spring/Quarkus, a WAR in an application server, Jetty 10+ (which needs Java 11), and newer Java levels.

## ADR-2 — Frozen contract and two build phases

The project was built in two phases:
1. The **first phase** built the Java system, its contract tests and HTTP acceptance checks.
2. The Java system was then frozen by a file manifest and its image ID, and accepted independently.
3. The **second phase** built the Python integration against that baseline and never edited, rebuilt or retagged it.

If the integration had found Java behavior that contradicted the contract, the defect would have been fixed in a new, separately verified build of the Java system. It would not have been patched from the Python side.

## ADR-3 — One approval, one order POST

> **ONE APPROVAL, ONE ORDER POST.** One explicit human approval authorizes at most one E8 request (`POST /halden/orders`) handed to the HTTP transport. This is a wire-level guarantee, not a statement about successful business operations. Before that request, the integration may perform authentication preparation and validation reads. Once the E8 request has been handed to the transport, the approval is consumed and cannot authorize another E8, whatever the response: no re-login-and-resend, no wait-and-resend, no retry. A definitive pre-handler rejection is recorded as NOT_APPLIED and a further attempt requires a fresh explicit human approval. A timeout, lost response or ambiguous server error is never answered with another E8; only read-only reconciliation is allowed. Simulator fault-control state is never evidence: the integration does not read it. Read requests keep their own bounded retries.

**Implementation:**
- `httpx.Client(follow_redirects=False)` with `httpx.HTTPTransport(retries=0)`, and no hook or wrapper that could replay a request.
- `prepare_write` performs reads only. `post_order_once` hands exactly one request to the transport.
- The approval is consumed under the draft-store lock before any I/O.

**Tests:**
- The order POST is counted at the transport, below every client layer.
- Races are repeated 20 times.

## ADR-4 — A missing order is not "not applied"

`NOT_APPLIED` arises only from positive evidence that HALDEN did not process the request:
- the pre-send preparation failed;
- no connection could be made;
- a pre-handler refusal came back (401/403/440/429, which the legacy contract guarantees happen before any business logic).

A timeout, a 5xx response or an unreadable reply is uncertain. An order that is not yet visible is `UNKNOWN`, never `NOT_APPLIED`, and `UNKNOWN` permits only a read-only re-check.

## ADR-5 — The reference is not an idempotency key

HALDEN's only free-text reference field is 12 characters wide, not unique, and never used to deduplicate. LEGACYBRIDGE writes its draft id there (`LB-` plus 8 hex digits) as an application-level correlation key for reconciliation. The consequences are stated plainly:
- No exactly-once claim is made.
- A coincidental historical match is a documented residual risk.
- A customer PO cannot be carried on a bridge order.

## ADR-6 — Complete-snapshot identity and strict stock evidence

**Identity.** Every product view is built from the complete item master: all pages, no server-side filter, grouped by canonical SKU before filtering. A search for "legacy item" therefore returns the same product, with the same price conflict, as a direct SKU lookup.

**Stock.** Availability always comes from one complete stock extract read without `item`. A SKU-filtered read could hide a row whose missing identity must invalidate the whole extract. The extract is validated in full:
- A structural or identity defect withholds availability for **every** SKU.
- An attributable defect, such as a negative allocated quantity, withholds it for that SKU only.

**Trade-off.**
- Each operation reads the full item master, an O(catalogue) cost. That is acceptable for 12 records and chosen for correctness.
- One malformed stock row withholds the whole catalogue's availability, so that no confident verdict is ever built on incomplete evidence.
- At production scale, an ERP-side group-aware query or a synchronized replica would need a separate design decision. Neither is in scope.

## ADR-7 — Trusted single-operator local demo

The console binds to `127.0.0.1` and has no user authentication.
- An approval is a click by whoever operates the local console. It is not an authenticated individual approval, and no per-user approval audit exists.
- Drafts are in memory and shared by all browser sessions of one console process.
- Adding an identity provider or a persistence service is out of scope.

## ADR-8 — XML trust assumption

The item master is parsed with the standard library's `xml.etree.ElementTree`. Its input comes only from the internal HALDEN service on the private Compose network, which is treated as trusted. Untrusted XML would require a hardened parser.

## ADR-9 — Deterministic demo first, optional model second

**Mode A** is the default and needs no key. It runs seven guided scenarios against the real Java ERP and never constructs a chat model.

**Mode B** uses LangChain `create_agent` with six read/draft tools and no submit tool. It passes no sampling parameters, forced tool choice, checkpointer or middleware, so the provider's defaults apply. Real-model behavior has not been evaluated.

## ADR-10 — Bounded implementation decisions recorded during the integration phase

- The sidebar names the legacy stack "Java 8 · Tomcat 9 · JDBC/SQLite". The boundary test therefore forbids the literal connection-string token (`jdbc` in lower case, `jdbc:` in any case) rather than the word "JDBC" in any case.
- The "Reset demo data" control is shown only when the control plane answers a read-only probe. The probe's answer is never used as order evidence.
- The Order Desk line editor starts with one example line (BW-1001 × 10) so that a first draft takes one click.
