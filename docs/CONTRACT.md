# TraceHold contracts

Change these deliberately: the rest of the system builds on them. The data contract is enforced in code by
`data/contract.py`; the tools are served by `mcp_server/server.py` and follow the tool contract, including the
overlap rule (section 5).

## 1. Data contract (what the graph is built from)

The graph loader (`graph/load.py`) reads only these files and only these columns. Any other column in
those files is ignored, and any column whose name contains a ground-truth word stops the load.

**Ground-truth words** (`data/contract.py`): `label`, `laundering`, `attempt`, `pattern`, `depth`, `role`,
`ring`. Ground truth lives only in `eval/labels/` and IBM's raw files, which the loader never reads.

| File | Columns the loader reads | Becomes |
| --- | --- | --- |
| `HI-Small_accounts.csv` (IBM) | Bank Name, Bank ID, Account Number, Entity ID, Entity Name | `Bank`, `Customer` (kind from the owner type), `OWNS`, `HELD_AT` |
| `work/clean/transfers.parquet` | transfer_id, from_account_id, to_account_id, amount_paise, ts, format, self_loop, own_account | `TRANSFERRED` |
| `synthetic/output/account_overlay.csv` | account_id, opened_at, opening_balance_paise | `Account` |
| `synthetic/output/registered_merchants.csv` | entity_id | `Customer.registered_merchant` |
| `synthetic/output/used_device.csv` | account_id, device_id, first_seen | `Device`, `USED_DEVICE` |
| `synthetic/output/has_phone.csv` | entity_id, phone_id | `Phone`, `HAS_PHONE` |
| `synthetic/output/logged_in_from.csv` | account_id, ip_id, first_seen | `IP`, `LOGGED_IN_FROM` |
| `synthetic/output/complaints.csv` | complaint_id, victim_account_id, fraud_transfer_ids, disputed_amount_paise, reported_at | `Complaint` |

**IDs and units**

| Item | Rule |
| --- | --- |
| Account ID | `<bank code without leading zeros>-<account number>`, e.g. `21174-800737690` |
| Transfer ID | `T` + the zero-based data row of `HI-Small_Trans.csv`, 9 digits, e.g. `T000137520` |
| Customer ID | IBM's `Entity ID` |
| Complaint ID | `CMP-` + the attempt's position in `HI-Small_Patterns.txt`, 4 digits |
| Money | integer paise; one amount per transfer, from the amount paid, with `synthetic/fx_rates.csv` |
| Time | integer UTC epoch seconds; IBM's times are read as IST wall-clock |

**Graph model**

```
(:Bank {id, name})
(:Customer {id, kind, registered_merchant})
(:Account {id, bank_id, opened_at, opening_balance})
(:Device {id}) (:Phone {id}) (:IP {id})
(:Complaint {id, victim_account_id, fraud_transfer_ids, disputed_amount, reported_at})
(Customer)-[:OWNS]->(Account)-[:HELD_AT]->(Bank)
(Account)-[:TRANSFERRED {id, amount, ts, format, self_loop, own_account}]->(Account)
(Account)-[:USED_DEVICE {first_seen}]->(Device)
(Account)-[:LOGGED_IN_FROM {first_seen}]->(IP)
(Customer)-[:HAS_PHONE]->(Phone)
```

Indexes on `id` for every label and on `TRANSFERRED.id` and `TRANSFERRED.ts`; unique constraints on
every label's `id`.

## 2. Tool contract (what the agents can call)

Eleven tools on MCP (served by `mcp_server/server.py`) plus two that no agent can call. `execute_lien`: only the
Verifier's gate code reaches it, through the handoff API's gate route. `undo_action`: only the UI reaches
it, through the handoff API's undo route (`POST /handoff/actions/undo`); it is on no MCP path. Every tool takes
`bank_id` and `investigation_id` (filled in by code, never by a model) and returns either its data or
`{"error": {"code", "message"}}`, which the agent records rather than ignores. Read tools run through
`GRAPH.RO_QUERY` (the engine refuses writes); act tools write only through deterministic code. Amounts are
integer paise. Exact Cypher is in `QUERIES.md`.

| Tool | Kind | Inputs (besides bank_id, investigation_id) | Returns |
| --- | --- | --- | --- |
| `recall_context` | Read | `account_ids`, `device_ids`, `phone_ids`, `ip_ids` (up to 200 IDs in all) | `cases` (earlier investigations in this bank's memory that touched the entities, directly or through a device, phone or IP that a given account uses), `matches` (per entity and case: `earlier_account`, `role_then`, and `how`, the given accounts that link to it, each with `via` account, device, phone, ip or given), `decisions` (with the gate result and who decided), `actions` (not undone), `lessons` (active lessons whose scope touches the entities, each with `matched` and `for_accounts`), `bank_lessons` (the bank's 10 most trusted active lessons), `looked_up` |
| `trace_funds` | Read | `start_account_ids` (up to 100), `after_ts`, `depth` (1-3) | per hop and account: later outflows (count, total) and the 25 largest, each at or after the transfer that brought the money |
| `compute_taint` | Read | `account_ids` reached so far (up to 400; empty = just the first layer). Once the trace handoff exists, the engine runs over the whole traced trail plus these, and empty = every traced account | per account: tainted amount, balance now, tainted share, lien amount, layer, tainted senders and inflow transfer IDs, transfer IDs that carried taint; accounts reached but not followed; unrecoverable amount; conservation |
| `account_signals` | Read | `account_id` | the signal record: pass_through_ratio, median_minutes_to_forward, distinct_payers, distinct_payees, account_age_days, registered_merchant, owner_kind, inflow_count, outflow_count, tainted_share, accounts_sharing_device/phone/ip, devices_shared_with_known_mules, phones_shared_with_known_mules (a known mule is an account with an approved mule decision at this bank), related_cases (earlier investigations at this bank that involved the account or one of its devices, phones or IPs), prior_decisions |
| `shared_attribute_cluster` | Read | `account_id` | accounts sharing a device, a phone (through owners) or an IP, grouped by what is shared |
| `mule_community` | Read | `account_ids` (the investigation's) | community (label propagation over shared identifiers) and component (WCC) per account |
| `key_accounts` | Read | `community_id` | the community's accounts ranked by betweenness (fixed seed) |
| `open_review` | Act, autonomous | `account_id`, `question` | review case ID (Action node; reversible). One review per account per investigation: asking again returns the same case |
| `request_info` | Act, autonomous | `account_id`, `what` (sent to the account's bank) | request ID and action ID (mock bank API; Action node; reversible). The action ID is the request's idempotency key at the bank (`request_key`), so asking again returns the first request instead of sending another |
| `flag_entity` | Act, autonomous | `entity_id` (device, phone or IP), `reason` | flag ID (Action node; reversible). One flag per entity per investigation |
| `propose_lien` | Act, gated | `account_id`, `decision_id`, `label`, `rationale` | decision (proposal) ID and the code-calculated lien amount (`lien_cap`: the remaining tainted amount, capped by the balance less other investigations' active liens, section 5); writes a Decision with `approved` unset. `nothing_to_lien` when the account holds no tainted money or earlier liens hold its whole balance |
| `execute_lien` | Act, gated | `decision_id` | lien ID, or a refusal from the guard (`safety/guard.py`); places a lien (own bank) or sends a lien request (other banks). It sends the account's engine balance as `balance_paise` and records the amount the bank actually placed, which can be lower than the decision's |
| `undo_action` | Act, UI only | `action_id`, `reason` (required) | the action with status `undone` (the Action node keeps its history). An information request is also withdrawn at the bank (`POST /info-requests/{id}/withdraw`); a review or flag is only marked undone, so it leaves the open review queue and `recall_context`. Undoing twice returns `already_undone`. Liens are not automatic actions and cannot be undone here |

Error codes include `bad_input`, `not_found`, `forbidden`, `too_large`, `not_ready`, `nothing_to_lien`,
`incomplete_read`, `taint_error`, `timeout`, `bank_api`, `internal` and `refused_<reason>` for the guard.

`recall_context` caps its lists: 20 cases, 100 matches, 50 decisions, 50 actions, 20 lessons. More than 200 IDs in
one call is `too_large`.

Scopes (each agent's MCP path lists only these): Tracer `recall_context`, `trace_funds`, `compute_taint`.
Adjudicator `recall_context` (it is the agent that must cite lessons),
`account_signals`, `shared_attribute_cluster`, `mule_community`, `key_accounts`, `open_review`, `request_info`,
`flag_entity`, `propose_lien`. Verifier (read only) `recall_context`, `compute_taint`, `account_signals`,
`shared_attribute_cluster`.

## 3. Handoff contract (how the agents pass work on)

The four handoffs in `handoff/contracts.py` (trace, proposal, check-failure note, verdict record) are the only
way the agents talk to each other: Pydantic models with unknown fields forbidden, size limits and rationales
of at most two sentences, stored as Handoff nodes by the handoff API (`handoff/api.py`), which also runs the
seven deterministic checks on every proposal and holds the run's state machine. Amounts in a handoff are
always copied by code from tool results.

Lessons travel in two fields: the proposal's `lessons_applied` (at most 10) and `LienVerdict.lessons_cited`, an
optional list of at most 10 lesson IDs the Verifier's verdict relied on (empty by default). Code drops any cited ID
the bank's memory does not hold.

The state machine lives in `handoff/state.py`. `Investigation.status` moves only along its transitions, and
`store.move` raises `IllegalTransition` (HTTP 409) for anything else:

```
new -> tracing -> traced -> adjudicating -> proposed -> verifying -> at_gate -> executed (liens placed) or closed (none)
adjudicating -> checks_failed -> adjudicating     (first check failure: one retry by the Adjudicator)
tracing, adjudicating, verifying -> themselves    (a crashed worker's claim taken back)
any status except executed, closed, failed -> failed (a run guard stopped it) or closed (an analyst closed it)
```

Routes on the handoff API for steps, claims, the case chat and the UI (JSON, all under `/handoff/`; the full list is
in the docstring of `handoff/api.py`). Which sandbox may call which route is in `SANDBOX.md`.

| Caller | Route | Purpose |
| --- | --- | --- |
| Agents | `POST step` | One Step after an agent node; also renews the claim's lease |
| Agents | `POST claim` | Takes `resume` (take back this agent's unfinished work) and a named `investigation_id` (the Adjudicator's own retry) |
| Verifier | `POST chat/claim` | The next waiting case-chat question, with a context that code reads from the graphs |
| Verifier | `POST chat/answer` | An answer; the server checks every ID in it and answers 422 with `unknown_ids` if any does not exist (`final` stores it as rejected) |
| UI | `POST actions/undo` | Reverse an automatic action (`undo_action`) |
| UI | `GET reviews`, `POST reviews/resolve` | The review queue, and resolving a review as `cleared` or `escalated`. Resolving also scores the lessons on the reviewed account (`learn_at_review`); the response adds `lessons`, one entry per lesson changed (empty when the review was already resolved) |
| UI | `GET memory` | Lessons, decisions, cases and node counts of one bank |
| UI | `GET trail/{bank}/{inv}` | The trail as nodes and edges, for the graph |
| UI | `POST chat/ask`, `GET chat/{bank}/{inv}` | Ask a question about a case; read the conversation |
| UI | `POST close` | An analyst closes a case (a reason is required) |
| UI | `GET banks` | Bank names for the selector |

Three more routes serve the React UI's cost and FalkorDB views (all `GET`, JSON, read only):

| Caller | Route | Purpose |
| --- | --- | --- |
| UI | `GET spend/calls` | The spend ledger's rows, newest first, one per model call (`handoff/spend.py`, `calls`): `id`, `ts`, `agent`, `investigation_id`, `model`, `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_tokens`, `usd`. Parameters: `investigation_id` (only that investigation's calls) and `limit` (default 5000, held between 1 and 20000) |
| UI | `GET graph/activity` | The tool server's in-memory log of its FalkorDB queries (`graph/client.py`, `record` and `activity`). Parameters: `since` (only entries with a larger `seq`, default 0), `limit` (default 200, held between 1 and 1000), `source` (`tool`, `agent`, `ui` or `other`) and `investigation_id`. Returns `entries` (the newest matches, oldest first), `last_seq`, `totals` (per graph: `queries`, `ms`, `errors`, counted since the tool server started) and `started_ms`. The log keeps 2,000 queries, plus 1,000 tool queries in a separate list so that polling cannot push them out (`source=tool` reads that list); a restart empties it. Each entry has `seq`, `ts_ms`, `graph`, `kind` (`read` or `read-write`), `query` (one line, cut at 240 characters), `params` (the first six, shortened), `ms`, `engine_ms`, `rows`, `ok`, `source`, `name`, `bank` and `investigation_id`. `source` says who ran the query, through the `CALLER` context variable: `tool` (an agent's tool, `name` is the tool), `agent` (an agent's handoff call, `name` is the route), `ui` (a route the UI calls) or `other` (nothing tagged it) |
| UI | `GET graph/stats` | Counts of the `network` graph and the banks' memory graphs: `network` maps every node label and relationship type to its count, `memory` maps each bank in `banks` to its memory graph's node counts by label, `graphs` lists the graph names on the server and `at` is when the counts were taken (epoch seconds). Parameter `banks`: bank IDs separated by commas, at most 10. The answer is cached for 10 minutes and recomputed at once when `banks` differs from the cached call; each count query has a 30-second timeout |

The browser never calls the tool server directly. `ui_server/app.py` forwards `/api/h/<path>` to `/handoff/<path>` only when the method and path match its `ALLOW` list (the UI's reads, plus `POST` of investigations, `gate/decide`, `actions/undo`, `reviews/resolve`, `chat/ask` and `close`) and answers 403 `forbidden` for the rest, including every route the agents' workers use.

## 4. Memory model (each bank's memory graph)

One `memory_<bank>` graph per bank (`graph/memory_graph.py`), with a unique `id` on every label: investigations with their
handoffs, steps, claims, decisions, actions, liens, case-chat messages and lessons, and the `INVOLVES` edges a stored
trace writes. Properties as `memory/store.py` writes them:

```
(:Investigation {id, complaint_id, status, trace_id, started_at, claimed_by, lease_until, reclaims, gate_mode, ...})
(:Handoff {id, kind, from_agent, to_agent, payload, n}) (:Step {id, n, agent, node, tool, tokens, usd, status, ...})
(:Claim {id, account_id, label, proposed_action, transfer_ids, signals, lessons_applied, decision_id, attempt})
(:Decision {id, account_id, label, lien_amount, approved, decided_by, gate_mode, gate_reason})
(:Action {id, kind, status, external_id}) (:Lien {id, amount, status}) (:EntityRef {id, kind})
(:Message {id, n, role, text, status, cited_ids, reply_to})
(:Lesson {id, text, stance, label, applies_to, confidence, times_confirmed, times_contradicted, active, source, history})
(Investigation)-[:HAS_HANDOFF]->(Handoff)   (Investigation)-[:HAS_STEP]->(Step)   (Investigation)-[:HAS_MESSAGE]->(Message)
(Investigation)-[:PRODUCED]->(Claim)-[:ABOUT]->(EntityRef)
(Investigation)-[:HAS_DECISION]->(Decision)-[:ABOUT]->(EntityRef)   (Decision)-[:EXECUTED_AS]->(Lien)
(Investigation)-[:TOOK]->(Action)-[:ON]->(EntityRef)
(Investigation)-[:INVOLVES {role, via, layer, tainted}]->(EntityRef)
(Decision)-[:CREATED]->(Lesson)   (Lesson)-[:APPLIES_TO]->(EntityRef)   (Lesson)-[:SUPERSEDED_BY]->(Lesson)
```

- FalkorDB cannot join across graphs, so memory points at network entities by ID through `EntityRef` (kind account,
  device, phone or ip).
- When a trace is stored, `INVOLVES` is written for the victim (`role` victim), every traced account (`role`
  traced, with `layer` and `tainted` in paise) and each of those accounts' devices, phones and IPs (`role`
  identifier, `via` the account that uses it). This is what lets `recall_context` find an earlier case through a
  shared identifier. Decisions and actions add a plain `INVOLVES` to their entity.
- Every writer MERGEs on a deterministic ID, so running it twice leaves one node. `Message` is the exception: IDs
  are `MSG-<investigation>-<n>`, a question asked twice is two messages, and an answer is stored once per question.
- `Lesson.stance` is `release` ("do not lien accounts like this") or `hold` ("lien accounts like this"), `source` is
  `human` or `verifier_agent`, and `applies_to` lists `account:`, `device:`, `phone:` and `label:` entries. The
  rules for making and changing lessons are in `memory/lessons.py`; the settings are `lessons` in `agents/config.yaml`.
  A gate decision confirms or contradicts a lesson only when the decided account has the lesson's label (`scored_by`).
  A resolved review does the same for the lessons on its account, its devices and phones, and those its proposal
  cited, when it comes from a later case (another investigation, opened after the lesson was made) and the account
  has the lesson's label (`review_counts`): `cleared` counts as a rejected lien, `escalated` as an approved one. The
  history entry carries the review's `action_id`. A review never makes a lesson.
- `Claim.id` is `CLM-<investigation>-a<attempt>-<account>`; `Step.id` is `STEP-<investigation>-<agent>-<run>-<n>`.

## 5. Overlapping complaints

Two complaints can reach the same account. The rule is earlier report first: a lien already placed keeps its amount,
and a later complaint can lien only what the account's balance leaves after the active liens of other
investigations, never below zero. A reversed lien holds nothing; placed liens and lien requests both count.

| Where | Contract |
| --- | --- |
| `held_by_others` (`mcp_server/tools.py`) | The sum of `amount_paise` over the `GET /liens?account_id=` rows whose status is not `reversed` and whose `investigation_id` is another investigation's |
| `lien_cap` (`mcp_server/tools.py`) | `max(0, min(t.lien_amount, t.balance - held_by_others))`, where `t` is the account's engine result |
| `propose_lien` | The Decision's amount is `lien_cap`. At 0 the tool returns `nothing_to_lien`, and the Adjudicator opens a review instead |
| Deterministic check 2 (`safety/checks.py`) | Compares the proposal's amount and the Decision's amount against the same `lien_cap`. It fails for the account if the bank cannot say what is held |
| Mock bank `POST /liens`, `POST /lien-requests` | Optional field `balance_paise` (integer paise, 0 to 10^15). When given, the amount placed is `min(amount_paise, balance_paise - the account's active liens from other investigations)`, worked out under the bank's lock; 409 when nothing is left. A replay of a decision that already has a lien returns the first lien. It is accepted when the stored (capped) amount is at most the amount asked, and refused with 409 when it is more, or when the account differs |
| Mock bank `GET /liens` | Optional filter `account_id`, beside `investigation_id` |
| `execute_lien` | Sends the account's engine balance as `balance_paise`; the Lien node stores the amount the bank returns |

The evaluation applies the same rule to every account that two or more complaints' code-only trails reach, in
`reported_at` order (`allocate` and `overlap_check` in `eval/methods.py`), and checks that an account's liens stay
within its balance and a complaint's liens within its disputed amount.
