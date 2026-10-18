# Cypher queries and algorithm calls

Every query the product depends on, and why it exists (rule 10), grouped by the step of the system that runs it:

1. Building the graph: the schema, the loader, the load checks and the engine check.
2. The agents' tools: the network queries, and the scratch graphs the graph algorithms run on.
3. Each bank's memory: runs, decisions and liens; identifiers, recall of earlier cases, claims with leases,
   involvement, handoffs, lessons and the case chat.
4. Evaluation: the overlap rule's lookup and the load test's checks.
5. The live FalkorDB page: its counts and the query log.

## 1. Building the graph

### Schema (`graph/schema.py`)

| Query | Why |
| --- | --- |
| `CREATE INDEX FOR (n:<Label>) ON (n.id)` for Bank, Customer, Account, Device, Phone, IP, Complaint | Every loader and tool lookup is by ID |
| `CREATE INDEX FOR ()-[r:TRANSFERRED]-() ON (r.id)` | Find a transfer by ID (complaint fraud transfers, evidence checks) |
| `CREATE INDEX FOR ()-[r:TRANSFERRED]-() ON (r.ts)` | Time-window filters on transfers |
| `GRAPH.CONSTRAINT CREATE network UNIQUE NODE <Label> PROPERTIES 1 id` | Building it after the load proves no ID was loaded twice |

### Loader (`graph/load.py`, batches of 10,000 rows)

| Query | Why |
| --- | --- |
| `UNWIND $rows AS r CREATE (:Bank {id: r.id, name: r.name})` | Banks from IBM's accounts file |
| `UNWIND $rows AS r CREATE (:Customer {id: r.id, kind: r.kind, registered_merchant: r.rm = 1})` | Owners from IBM's accounts file |
| `UNWIND $rows AS r CREATE (:Account {id: r.id, bank_id: r.bank_id, opened_at: r.opened_at, opening_balance: r.opening_balance})` | Accounts with the overlay's opening date and balance |
| `UNWIND $rows AS r CREATE (:Device {id: r.id})`, same for `Phone` and `IP` | Overlay identifiers |
| `UNWIND $rows AS r CREATE (:Complaint {id: r.id, victim_account_id: r.victim, fraud_transfer_ids: r.fraud, disputed_amount: r.disputed, reported_at: r.reported})` | Complaints the Tracer starts from |
| `UNWIND $rows AS r MATCH (a:Account {id: r.a}), (b:Bank {id: r.b}) CREATE (a)-[:HELD_AT]->(b)` | Which bank holds each account |
| `UNWIND $rows AS r MATCH (c:Customer {id: r.c}), (a:Account {id: r.a}) CREATE (c)-[:OWNS]->(a)` | Ownership, for own-account transfers and phones |
| `UNWIND $rows AS r MATCH (a:Account {id: r.a}), (d:Device {id: r.d}) CREATE (a)-[:USED_DEVICE {first_seen: r.fs}]->(d)` | Shared-device signals |
| `UNWIND $rows AS r MATCH (a:Account {id: r.a}), (i:IP {id: r.i}) CREATE (a)-[:LOGGED_IN_FROM {first_seen: r.fs}]->(i)` | Shared-IP signals |
| `UNWIND $rows AS r MATCH (c:Customer {id: r.c}), (p:Phone {id: r.p}) CREATE (c)-[:HAS_PHONE]->(p)` | Shared-phone signals |
| `UNWIND $rows AS r MATCH (a:Account {id: r.f}), (b:Account {id: r.t}) CREATE (a)-[:TRANSFERRED {id: r.id, amount: r.amt, ts: r.ts, format: r.fmt, self_loop: r.sl = 1, own_account: r.oa = 1}]->(b)` | The money trail, in time order |

Every batch must create exactly one node or edge per row; a batch that creates fewer (a missing endpoint)
stops the load. Background saves are paused (`CONFIG SET save ""`, `auto-aof-rewrite-percentage 0`)
through the load and the checks, so the server never forks while the graph is changing: a fork copies
every page the server touches, and the first reads after a large load rewrite most of the matrices
(4.5 GB copied in testing). After the checks, with nothing else running, one `SAVE` writes a snapshot
without forking (it also settles the matrices, so the next fork copies far less), then one `BGREWRITEAOF`
compacts the log (the loader waits for it, about 0.6-0.8 GB copied), and the settings are put back.

### Load checks (`graph/load.py`, `python -m graph.load --verify-only`)

| Query | Why |
| --- | --- |
| `MATCH (n:<Label>) RETURN count(n)`, `MATCH ()-[r:<TYPE>]->() RETURN count(r)` | Counts equal the cleaned slice |
| `MATCH (a:Account {id: $f})-[t:TRANSFERRED]->(b:Account) WHERE t.id = $id RETURN ...` | Three random transfers match the cleaned table |
| `MATCH (c:Customer)-[:OWNS]->(a:Account {id: $id})-[:HELD_AT]->(b:Bank) RETURN ...` | Three random accounts match their owner, bank and overlay |
| `MATCH (a:Account) OPTIONAL MATCH (a)-[h:HELD_AT]->(:Bank) WITH a, count(h) AS n WHERE n <> 1 RETURN count(a)` | One bank per account (same for one owner) |
| `UNWIND $rows AS r MATCH (c:Complaint {id: r.c}) WHERE ... RETURN count(c)` | Complaint nodes match `complaints.csv` |
| `UNWIND $rows AS r MATCH (a:Account)-[t:TRANSFERRED {id: r.t}]->() RETURN r.c, r.t, a.id, t.amount` | Each complaint's fraud transfers leave its victim and add up to the disputed amount |
| `CALL db.propertyKeys()`, `CALL db.labels()`, `CALL db.relationshipTypes()` | No ground-truth word anywhere in the graph |
| `CALL db.indexes()`, `CALL db.constraints()` | Indexes and constraints are in place |

**Server limits on the pinned image.** The FalkorDB image starts with `FALKORDB_ARGS` set to
`MAX_QUEUED_QUERIES 25 TIMEOUT 1000 RESULTSET_SIZE 10000`: read queries stop after 1 second, and a
result longer than 10,000 rows is cut without an error. The load checks, the snapshot and the tests scan
the whole graph, so they pass their own 10-minute timeout per query (`client.LONG_QUERY_MS`). The agents'
tools keep the 1-second default and must aggregate or page instead of returning more than 10,000 rows.
`graph/engine_check.py` records these limits in `engine_check.json`.

**Planner findings (every tool query follows them).** Check every tool query with `GRAPH.EXPLAIN`.

1. Look a transfer up by its own ID. `MATCH (v:Account {id: r.v})-[t:TRANSFERRED]->() WHERE t.id IN r.f`
   after an `UNWIND` makes FalkorDB 6.0.2 traverse every `TRANSFERRED` edge and filter afterwards (a
   full scan per row). `MATCH (a)-[t:TRANSFERRED {id: r.t}]->()` uses the edge index (`Edge By Index Scan`).
2. Compare times only after a `WITH`. For a time-respecting hop, `MATCH (v)-[f {id: tid}]->(m)
   MATCH (m)-[n:TRANSFERRED]->(x) WHERE n.ts >= f.ts` loses the first index (77 s for one complaint's 16
   fraud transfers); with `WITH f, m` between the matches the planner range-scans the `TRANSFERRED.ts`
   index instead (6.5 s). Moving the time filter behind a second `WITH` makes it read only the account's
   own transfers (`Conditional Traverse`), in 2 ms:

   ```
   UNWIND $ids AS tid
   MATCH (v:Account)-[f:TRANSFERRED {id: tid}]->(m:Account) WITH f, m
   MATCH (m)-[n:TRANSFERRED]->(x:Account) WITH f, n WHERE n.ts >= f.ts
   RETURN count(n)
   ```

   `tests/test_network_graph.py` checks this plan on the loaded graph.
3. After an `UNWIND`, look the account up on its own before traversing. `UNWIND $ids AS id MATCH
   (x:Account {id: id})-[t:TRANSFERRED]->(y)` walks every `TRANSFERRED` edge and filters (3.4 s for two
   accounts); `UNWIND $ids AS id MATCH (x:Account {id: id}) WITH x MATCH (x)-[t:TRANSFERRED]->(y)` uses
   the index (`Node By Index Scan`, 4 ms). Every tool query follows this shape.
4. Never write `MATCH ... WHERE ... MATCH ... WHERE ...` with no `WITH` between: it can return no rows at all
   (found on the server, 17 Oct). `MATCH (e:EntityRef) WHERE e.id IN $ids MATCH (l:Lesson)-[:APPLIES_TO]->(e)
   WHERE l.active = true` gave nothing although the same query without the second `WHERE` gave both matching
   rows and `l.active` was `true`. The plan shows why: FalkorDB 6.0.2 joins the two `MATCH` clauses, scans
   `Lesson` and runs the whole filter, `e.id IN $ids` included, before `e` is bound (`Node By Label Scan`,
   `Filter`, then `Conditional Traverse`). The shape of finding 3 is correct and fast:
   `UNWIND $ids AS eid MATCH (e:EntityRef {id: eid}) WITH e MATCH (l:Lesson)-[:APPLIES_TO]->(e) WHERE ...`.
   No other query in the code has the broken shape.

### Engine check (`graph/engine_check.py`)

| Call | Why |
| --- | --- |
| `CALL dbms.procedures() YIELD name` | The four algorithms exist on the pinned image |
| `CALL algo.WCC({nodeLabels: ['Account'], relationshipTypes: ['TRANSFERRED']}) YIELD node, componentId` | Mule communities (the `mule_community` tool) |
| `CALL algo.labelPropagation({nodeLabels: ['Account'], relationshipTypes: ['TRANSFERRED'], maxIterations: 10}) YIELD node, communityId` | Mule communities |
| `CALL algo.betweenness({nodeLabels: ['Account'], relationshipTypes: ['TRANSFERRED'], samplingSize: 100, samplingSeed: 42}) YIELD node, score` | Hub accounts (the `key_accounts` tool); a fixed seed gives repeatable scores |
| `CALL algo.SSpaths({sourceNode: x, relTypes: ['TRANSFERRED'], relDirection: 'outgoing', maxLen: 3, pathCount: 10}) YIELD path` | Candidate paths; it **ignores time order**, so every path is re-checked |
| `MATCH (x)-[r1:TRANSFERRED]->()-[r2:TRANSFERRED]->(z) WHERE r2.ts >= r1.ts RETURN z` | The time-respecting hop `trace_funds` uses instead |
| `GRAPH.RO_QUERY`, `GRAPH.MEMORY USAGE`, `DUMP`/`RESTORE`, `GRAPH.COPY` | Read-only tools, memory sizing, snapshots, the tools' scratch graphs |

## 2. The agents' tools (`mcp_server/network.py`, `mcp_server/tools.py`)

All read tools go through `GRAPH.RO_QUERY` with a 20-second timeout (the server default is 1 second).
Whole-history reads are paged under the 10,000-row cap and checked against the account's degree.

| Query | Used by | Why |
| --- | --- | --- |
| `MATCH (c:Complaint {id: $id}) RETURN c.victim_account_id, c.fraud_transfer_ids, c.disputed_amount, c.reported_at` | every tool | The investigation's complaint |
| `UNWIND $ids AS tid MATCH (a:Account)-[t:TRANSFERRED {id: tid}]->(b:Account) RETURN t.id, a.id, b.id, t.amount, t.ts, t.format, t.self_loop` | compute_taint, checks | Fraud and cited transfers by their own ID (edge index) |
| `UNWIND $ids AS id MATCH (a:Account {id: id}) WITH a MATCH (c:Customer)-[:OWNS]->(a)-[:HELD_AT]->(b:Bank) RETURN a.id, a.bank_id, a.opened_at, a.opening_balance, c.id, c.kind, c.registered_merchant, b.name, outdegree(a, 'TRANSFERRED'), indegree(a, 'TRANSFERRED')` | compute_taint, account_signals | Account facts and the transfer counts that plan the paging |
| `UNWIND $ids AS id MATCH (x:Account {id: id}) WITH x MATCH (x)-[t:TRANSFERRED]->(y:Account) RETURN x.id, t.id, ...` (and the incoming direction) | compute_taint, account_signals | Whole histories for accounts with up to 9,000 transfers, batched |
| `MATCH (x:Account {id: $id})-[t:TRANSFERRED]->(y:Account) RETURN ... ORDER BY t.id SKIP $s LIMIT 9000` | same | Pages for the 13 hub accounts with more than 9,000 transfers |
| `UNWIND $starts AS s MATCH (a:Account {id: s.id}) WITH s, a MATCH (a)-[t:TRANSFERRED]->(b:Account) WITH s, a, t, b WHERE t.ts >= s.after AND t.self_loop = false WITH a.id AS src, t, b ORDER BY t.amount DESC, t.id WITH src, count(t) AS n, sum(t.amount) AS total, collect([...]) AS ts RETURN src, n, total, ts[0..$top]` | trace_funds | One time-respecting hop: later outflows per account, the largest listed, one row per account |
| `MATCH (a:Account {id: $id})-[:USED_DEVICE]->(d:Device)<-[:USED_DEVICE]-(b:Account) WHERE b.id <> a.id RETURN d.id, b.id LIMIT 200` (and through owners' phones, and IPs) | shared_attribute_cluster, account_signals | Accounts sharing identifiers |
| `MATCH (n:Device {id: $id}) RETURN count(n)` (Phone, IP) | flag_entity | The flagged entity exists |

**Scratch graphs** (`scratch_<investigation>_<tool>_<n>`, deleted in a `finally`): the investigation's
accounts are copied with `TRANSFERRED` links (one per pair, with count and total) and `SHARES` links (both
directions, when two accounts use the same device, their owners share a phone, or they logged in from the
same IP). Shared identifiers become direct account links so label propagation can see a ring.

| Call | Used by | Why |
| --- | --- | --- |
| `CALL algo.labelPropagation({nodeLabels: ['Account'], relationshipTypes: ['SHARES'], maxIterations: 20}) YIELD node, communityId` | mule_community, key_accounts, check 6 | Mule communities: accounts tied by shared identifiers. Complaint 1's planted ring (8 accounts) comes out as one community; with `TRANSFERRED` included the whole trail merges into one |
| `CALL algo.WCC({nodeLabels: ['Account'], relationshipTypes: ['SHARES', 'TRANSFERRED']}) YIELD node, componentId` | mule_community | Connected components of the investigation |
| `CALL algo.betweenness({nodeLabels: ['Account'], relationshipTypes: ['SHARES', 'TRANSFERRED'], samplingSize: <community size>, samplingSeed: 42}) YIELD node, score` | key_accounts | Hub accounts of one community, repeatable |

Community and component IDs are named after their smallest account ID (`C-<account>`), so `key_accounts`
can rebuild the same community from the trace handoff.

## 3. Each bank's memory (`memory/store.py`, one `memory_<bank>` graph per bank)

### Runs, decisions and liens

Indexes and unique constraints on `id` for every memory label (`graph/memory_graph.py`). Writes are single
queries, so each is atomic. The ones that matter for safety:

| Query | Why |
| --- | --- |
| `MATCH (i:Investigation) WHERE i.status = $from WITH i ORDER BY i.started_at, i.id LIMIT 1 SET i.status = $to ...` | Claim work atomically: two workers never take the same investigation (with a lease, see `claim_next` below) |
| `MATCH (i:Investigation {id: $id}) WHERE i.status IN $from SET i.status = $to ...` | The run state machine moves only from an expected state |
| `MATCH (d:Decision {id: $id}) WHERE d.approved IS NULL SET d.approved = $ok, d.decided_by = $by, d.gate_mode = $mode, ...` | Only the gate sets `approved`, and only once |
| `MATCH (d:Decision {id: $dec}) WHERE NOT (d)-[:EXECUTED_AS]->(:Lien) CREATE (d)-[:EXECUTED_AS]->(:Lien {...})` | One lien per decision (execute_lien is idempotent) |

### Identifiers, recall, leases, handoffs, lessons and the case chat (`mcp_server/network.py`, `mcp_server/tools.py`, `memory/store.py`)

`store.move` (the state-machine row above) checks the transition against `handoff/state.py` before it sends the
query, so an illegal move never reaches the graph. Memory queries run on the bank's `memory_<bank>` graph; the one
`network` query is `identifiers`.

| Query | Used by | Why |
| --- | --- | --- |
| `UNWIND $ids AS id MATCH (a:Account {id: id}) WITH a MATCH (a)-[:USED_DEVICE]->(e:Device) RETURN a.id, e.id` | network.identifiers | An account's devices. The phone version replaces the second `MATCH` with `MATCH (a)<-[:OWNS]-(:Customer)-[:HAS_PHONE]->(e:Phone)` (phones belong to owners), the IP version with `MATCH (a)-[:LOGGED_IN_FROM]->(e:IP)`. Batches of 500 accounts. Same account-first shape as planner finding 3. Used when a trace is stored, in recall_context, in the account signals and when a lesson's scope is built at the gate |
| `UNWIND $ids AS eid MATCH (e:EntityRef {id: eid}) WITH e MATCH (i:Investigation)-[x:INVOLVES]->(e) WHERE i.id <> $inv RETURN e.id, e.kind, i.id, i.complaint_id, i.status, i.started_at, x.role, x.via ORDER BY i.started_at, i.id, e.id` | recall_context | Earlier cases that touched any of the looked-up IDs: the given accounts and the devices, phones and IPs they use. `x.via` names the earlier account behind an identifier, so a case reached through a shared phone says which account it came from |
| `MATCH (i:Investigation)-[:HAS_DECISION]->(d:Decision)-[:ABOUT]->(e:EntityRef) WHERE i.id IN $cases AND e.id IN $accs RETURN d, i.id, e.id ORDER BY d.ts` | recall_context | What those earlier cases decided about the accounts, with the gate result |
| `MATCH (i:Investigation)-[:TOOK]->(a:Action)-[:ON]->(e:EntityRef) WHERE i.id IN $cases AND (e.id IN $accs OR e.id IN $look) AND a.status <> 'undone' RETURN a, i.id, e.id ORDER BY a.ts` | recall_context | What those cases did (reviews, information requests, flags), leaving out what an analyst undid |
| `UNWIND $ids AS eid MATCH (e:EntityRef {id: eid}) WITH e MATCH (i:Investigation)-[:INVOLVES]->(e) WHERE i.id <> $inv RETURN count(DISTINCT i)` | account_signals | The `related_cases` signal: how many other investigations involved the account or one of its identifiers |
| `MATCH (i:Investigation) WHERE i.status = $from WITH i ORDER BY i.started_at, i.id LIMIT 1 SET i.status = $to, i.updated_at = $ts, i.claimed_by = $agent, i.claimed_at = $ts, i.lease_until = $lease RETURN i.id` | store.claim_next | Claim the oldest waiting case atomically and give the claim a lease |
| `MATCH (i:Investigation {id: $id}) WHERE i.status IN $from SET i.status = $to, i.updated_at = $ts, i.claimed_by = $agent, i.claimed_at = $ts, i.lease_until = $lease RETURN i.status` | store.claim_one | The Adjudicator takes its own retry (`checks_failed` back to `adjudicating`) by name, so it cannot take another case |
| `MATCH (i:Investigation) WHERE i.status = $s AND ($force OR coalesce(i.lease_until, 0) < $ts) WITH i ORDER BY i.started_at, i.id LIMIT 1 SET i.updated_at = $ts, i.claimed_by = $agent, i.claimed_at = $ts, i.lease_until = $lease, i.reclaims = coalesce(i.reclaims, 0) + 1 RETURN i.id` | store.claim_stale | Take back a case left in a working status: its lease ran out (the worker crashed), or `$force` is set because the agent's worker has just restarted and cannot still be working on it. Counts the takeover in `reclaims` |
| `MATCH (i:Investigation {id: $id}) WHERE i.status IN $working SET i.lease_until = $lease` | store.renew | Every Step extends the lease of a working case, so a slow but live worker is not taken over |
| `MATCH (i:Investigation {id: $inv}) UNWIND $rows AS r MERGE (e:EntityRef {id: r.id}) ON CREATE SET e.kind = r.kind MERGE (i)-[x:INVOLVES]->(e) ON CREATE SET x.role = r.role, x.via = r.via, x.layer = r.layer, x.tainted = r.tainted` | store.involve | Record the victim, every traced account and their devices, phones and IPs when a trace is stored. One row per entity (a repeated ID in one `MERGE` batch would break uniqueness), batches of 500, safe to run twice |
| `MATCH (i:Investigation {id: $inv}) OPTIONAL MATCH (i)-[:HAS_HANDOFF]->(o:Handoff) WITH i, count(o) AS n MERGE (h:Handoff {id: $id}) ON CREATE SET h.kind = $kind, h.from_agent = $f, h.to_agent = $t, h.schema_version = $v, h.payload = $p, h.ts = $ts, h.n = n + 1 MERGE (i)-[:HAS_HANDOFF]->(h)` | store.add_handoff | A handoff's ID is a digest of the case, kind and content, so the same submission twice is one node. `n` is its place in the case's order. `add_step` has the same shape, with the ID built from the agent run and step number |
| `MATCH (d:Decision {id: $dec}) MERGE (l:Lesson {id: $id}) ON CREATE SET l.text = $text, l.stance = $stance, l.source = $source, l.label = $label, l.applies_to = $applies, l.confidence = $conf, l.times_confirmed = 0, l.times_contradicted = 0, l.active = true, l.decision_id = $dec, l.created_ts = $ts, l.updated_ts = $ts, l.history = $hist MERGE (d)-[:CREATED]->(l)` | store.create_lesson | Make the lesson a decision taught. The ID comes from the decision, so there is one lesson per decision |
| `MATCH (l:Lesson {id: $id}) UNWIND $ents AS eid MERGE (e:EntityRef {id: eid}) MERGE (l)-[:APPLIES_TO]->(e)` | store.create_lesson | Link the lesson to the account, devices and phones it applies to (the label stays in `applies_to`) |
| `UNWIND $ids AS eid MATCH (e:EntityRef {id: eid}) WITH e MATCH (l:Lesson)-[:APPLIES_TO]->(e) WHERE NOT $active OR l.active = true WITH l, collect(DISTINCT e.id) AS via RETURN l, via ORDER BY l.confidence DESC, l.id` | store.lessons_touching | The lessons whose scope includes any of these accounts, devices or phones, most trusted first, with the entities that matched. Feeds recall_context and the gate's confirm and contradict step. Entity first, then `WITH` (planner finding 4: the two-`MATCH` form returned no rows) |
| `MATCH (o:Lesson {id: $old}), (n:Lesson {id: $new}) MERGE (o)-[:SUPERSEDED_BY]->(n) SET o.active = false, o.history = $hist, o.updated_ts = $ts` | store.supersede | A newer lesson with the opposite stance replaces the older one, which stops being active but stays in the graph |
| `MATCH (i:Investigation)-[:HAS_MESSAGE]->(m:Message {role: 'human'}) WHERE m.status = 'pending' OR (m.status = 'answering' AND coalesce(m.lease_until, 0) < $ts) WITH i, m ORDER BY m.ts, m.n, m.id LIMIT 1 SET m.status = 'answering', m.lease_until = $lease RETURN m.id, i.id` | store.claim_question | The Verifier worker claims the oldest waiting case-chat question atomically. A question whose answerer died (lease over) is offered again |

## 4. Evaluation, overlapping complaints and the load test (`mcp_server/tools.py`, `bank_api/app.py`, `eval/load_test.py`)

The evaluation adds no new query to the tools. The code-only trails call the same `trace_funds`, `compute_taint`,
`recall_context` and `account_signals` the agents call, in process (`eval/run_eval.py`), against `test_eval_memory_<bank>`
graphs that the run deletes at the end (`GRAPH.LIST` finds them, then `GRAPH.DELETE`), so the banks' real memory is
untouched.

The overlap rule reads the mock bank's database, not a graph (SQL, not Cypher):

| Query | Used by | Why |
| --- | --- | --- |
| `GET /liens?account_id=<id>`, which runs `SELECT * FROM liens WHERE account_id = ? ORDER BY created_at, id` | held_by_others, lien_cap | What liens and lien requests already exist on an account. The tool sums those of other investigations that are not reversed |
| `SELECT coalesce(sum(amount_paise), 0) FROM liens WHERE account_id = ? AND status != 'reversed' AND investigation_id != ?` | mock bank, POST /liens and /lien-requests | The same sum, under the bank's lock when a lien is placed, so two complaints approved at once cannot hold more than the balance |

The load test (`eval/load_test.py`, run by `tests/test_load.py` on the `test_memory_*` graphs) counts duplicates and
leaks with these read queries. Each count must be 0. They scan a whole memory graph, which is fine for a test graph
and is not used by the product:

| Query | Why |
| --- | --- |
| `MATCH (x:<Label>) WITH x.id AS id, count(*) AS c WHERE c > 1 RETURN count(*)`, for Investigation, Handoff, Step, Claim, Decision, Action, Lien, EntityRef, Lesson and Message | Duplicate IDs per label after 20 parallel runs (the unique constraints and the MERGE writers should make this 0) |
| `MATCH (i:Investigation)-[:TOOK]->(a:Action)-[:ON]->(e:EntityRef) WITH i, a.kind AS k, e, count(a) AS c WHERE c > 1 RETURN count(*)` | The same action twice on one entity in one investigation |
| `MATCH (d:Decision)-[:EXECUTED_AS]->(l:Lien) WITH d, count(l) AS c WHERE c > 1 RETURN count(*)` | Two liens for one decision |
| `MATCH (x) WHERE x.id IN $ids OR x.investigation_id IN $ids RETURN count(x)`, on banks 20 and 12 with the load test's investigation IDs | Cross-bank leaks: nothing from a bank 11 run appears in another bank's memory |

## 5. The live FalkorDB page (`handoff/api.py`, `graph/client.py`)

The FalkorDB page of the analyst app (Visualisation) shows what the server holds and what it is doing. It adds no
query to the agents' tools. Its glance strip comes from `graph_stats` (`GET /handoff/graph/stats?banks=...`), which
runs the read queries below and keeps the result for 10 minutes (sooner if the list of banks changes), because the
counts scan whole graphs. The page asks for the selected bank only. The counts are the same as the load checks'
(Load checks, above), here with the label and type names read first:

| Query | Why |
| --- | --- |
| `CALL db.labels()` and `CALL db.relationshipTypes()` on `network` | The names to count, so the strip needs no fixed list |
| `MATCH (n:<Label>) RETURN count(n)` for each label, `MATCH ()-[r:<TYPE>]->() RETURN count(r)` for each relationship type, on `network` (30-second timeout each) | Accounts, banks, customers, transfers, and the devices, phones and IPs, for the strip |
| `MATCH (n) RETURN labels(n)[0], count(n)` on `memory_<bank>`, for each bank in the request, at most 10 (`store.counts`) | The nodes in the selected bank's memory |
| `GRAPH.LIST` (`client.graph_names`) | How many graphs the server holds |

Every query that goes through `GraphHandle` (`query` and `ro_query` in `graph/client.py`) is also kept in an
in-memory log, `activity`: the graph, read or read-write, the query text on one line (cut at 240 characters), its
parameters shortened, the time in milliseconds (and FalkorDB's own run time), the row count, whether it failed, and who
ran it. Who comes from `CALLER`, a context variable set around each call: `tool` for an agent's tool (`run_tool` in
`mcp_server/tools.py`, with the tool's name, bank and investigation), `agent` for the handoff routes only the agents'
workers call (`AGENT_ROUTES` in `handoff/api.py`), `ui` for the other handoff routes, `other` when nothing is set. The
log holds the last 2,000 queries (agents' tool queries also have a list of their own, 1,000 long, so polling cannot
push them out) and totals per graph since the process started. Nothing is written to FalkorDB or to disk, the log is
lost when the tool server restarts, and reading it runs no query. `GET /handoff/graph/activity` returns the entries
after a sequence number, optionally for one source or one investigation; the page polls it every 2 seconds for its live
query feed and every 10 seconds for the "queries since the server started" figure.
