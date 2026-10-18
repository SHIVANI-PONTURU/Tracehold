# TraceHold evaluation set (ground truth)

Built 2026-10-14 13:12 IST by `build_eval_set.py` in 31 s from IBM's three files and the synthetic overlay. 10 of 10 checks pass. Never load these files into the graph: they are the answers the agents are scored against.

## Files

| File | Rows | Holds |
| --- | --- | --- |
| `labels/account_labels.csv` | 6,357 | Every account with a laundering-labelled transfer: 2,246 mules, 4,111 genuine. The other 512,224 accounts are genuine |
| `labels/complaints_truth.csv` | 370 | One row per complaint: IBM attempt, split, depth, depth bucket, trail size |
| `labels/trail_accounts.csv` | 2,195 | Per complaint, every account on the true money trail, with its layer and label |
| `labels/trail_transfers.csv` | 3,807 | Per complaint, every transfer on the true money trail |
| `labels/ring_devices.csv` | 370 | Written by the generator: the device and phone shared within each laundering attempt |
| `labels/complaint_overlaps.csv` | 1,536 | Pairs of complaints whose trails share accounts or devices (the Phase 4 runners do not read it; they find overlaps from the code-only trails) |
| `labels/demo_pair.csv` | 2 | The two complaints for the memory demo: the second recalls the first |

## Definitions

- **Mule:** more than 50% of the account's outgoing value is laundering-labelled in IBM's `Is Laundering` column. An account with no outgoing transfers is judged on its incoming value. Every other account is **genuine**. Victims keep their label but are flagged `is_victim` and left out of mule metrics.
- **True money trail:** starts at the complaint's fraud transfers and follows laundering-labelled transfers forward in time, each leaving an account the trail had already reached (same minute allowed). The victim's own transfers count only when they are the fraud transfers.
- **Layer:** the fewest transfers from the victim to the account along the trail (fraud transfers reach layer 1).
- **Depth:** the longest time-ordered chain of trail transfers, capped at 20. `depth_trail` follows every labelled transfer; `depth_attempt` only the attempt's own transfers in `HI-Small_Patterns.txt`.
- **Depth bucket:** by `depth_trail`: shallow 1 to 3, medium 4 to 6, deep 7 or more.
- **Split:** `dev` if the IBM attempt ends within the first 5 days (before 2022-09-06 00:00 IST), otherwise `test`. Tune thresholds on `dev` only.
- **Scoring:** a lien on a `mule` on the trail is correct; a lien on a `genuine` account counts as innocent money frozen; a `mule` on the trail with no lien or review is a miss.

## What the ground truth contains

| Measure | Value |
| --- | --- |
| Complaints | 370 (87 dev, 283 test) |
| Depth bucket: shallow | 277 complaints (62 dev, 215 test) |
| Depth bucket: medium | 37 complaints (9 dev, 28 test) |
| Depth bucket: deep | 56 complaints (16 dev, 40 test) |
| Accounts labelled mule | 2,246 (at 30%: 2,561; at 70%: 1,997) |
| Trail accounts per complaint (excluding the victim) | median 2, max 29 |
| Trail accounts that are genuine | 1,007 of 1,825 |
| Trail transfers inside the IBM attempt | 1,973 of 3,807 |
| Complaints whose victim banks with a demo bank | 15 |

## Demo pair

Complaint 1 is at least 4 layers deep, passes through a registered merchant's account and disputes at most 10,000,000 rupees. Complaint 2 is reported later, disputes at most the same, and its trail uses a device that complaint 1's trail uses. Both trails reach accounts at the investigating demo bank, which is complaint 1's victim bank when possible, so that bank's memory can recall complaint 1. Ranked by shared devices, then complaint 1's depth.

| Order | Complaint | Pattern | Split | Depth | Disputed (₹) | Shared devices | Investigating bank | Meets every criterion |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | CMP-0011 | RANDOM | dev | 8 | 867,196.01 | 3 | 11 | yes |
| 2 | CMP-0137 | FAN-OUT | test | 6 | 5,746,465.11 | 3 | 11 | yes |

1,536 pairs of complaints share trail accounts or devices (956 share accounts, 1,536 share devices).

## Depth by pattern

| Pattern | Complaints | Median depth (trail) | Max depth (trail) | Median depth (attempt only) |
| --- | --- | --- | --- | --- |
| BIPARTITE | 49 | 1 | 12 | 1 |
| CYCLE | 54 | 4 | 17 | 3 |
| FAN-IN | 40 | 1 | 17 | 1 |
| FAN-OUT | 48 | 1 | 14 | 1 |
| GATHER-SCATTER | 51 | 2 | 15 | 2 |
| RANDOM | 41 | 5 | 14 | 3 |
| SCATTER-GATHER | 44 | 2 | 13 | 2 |
| STACK | 43 | 2 | 13 | 2 |

## Phase 4 evaluation groups

Added by hand after the build: `build_eval_set.py` does not write this section, so a rebuild removes it. The Phase 4 runners (`eval/methods.py`, `eval/truth.py`) put every CYCLE complaint in a fourth group, `held_out`, whatever its depth, because CYCLE is an IBM pattern the prompts and thresholds were never developed on. Every other complaint keeps its depth bucket.

| Group | Dev complaints | Test complaints | Dev trail mules | Test trail mules |
| --- | --- | --- | --- | --- |
| shallow (depth 1 to 3) | 53 | 199 | 60 | 404 |
| medium (4 to 6) | 7 | 19 | 13 | 62 |
| deep (7 or more) | 12 | 26 | 24 | 129 |
| held_out (CYCLE) | 15 | 39 | 34 | 92 |
| total | 87 | 283 | 131 | 687 |

- Trail mules are the accounts labelled mule on a complaint's true money trail, victim excluded (`labels/trail_accounts.csv`). They total 818, which is the 1,825 trail accounts less the 1,007 genuine ones in the table above.
- The depth bucket table above counts the CYCLE complaints in their depth bucket. The test split has 16 CYCLE complaints in shallow, 9 in medium and 14 in deep (dev: 9, 2 and 4), so the 215 shallow test complaints there are 199 here.
- TraceHold's live sample is drawn from the test split with seed 20261017 (`SEED` in `eval/methods.py`): 12 complaints from each of shallow, medium and deep (CYCLE excluded) and 10 CYCLE, 46 in all. They hold 132 trail mules, and 40 of the 46 have at least one. The code-only baselines run on all 283 test complaints.
- The rules-only baseline's thresholds are chosen on the 72 dev complaints outside CYCLE (87 less 15).

## How the synthetic data maps onto IBM's data

| Synthetic file | Key | Real file and key | Matched | Note |
| --- | --- | --- | --- | --- |
| `output/account_overlay.csv` | `account_id` | HI-Small_accounts.csv: Bank ID + Account Number | 518,581 of 518,581 | covers 518,581 of 518,581 IBM accounts |
| `output/used_device.csv` | `account_id` | HI-Small_accounts.csv | 677,036 of 677,036 |  |
| `output/logged_in_from.csv` | `account_id` | HI-Small_accounts.csv | 1,050,790 of 1,050,790 |  |
| `output/has_phone.csv` | `entity_id` | HI-Small_accounts.csv: Entity ID | 168,966 of 168,966 | covers 166,207 of 166,207 IBM owners |
| `output/registered_merchants.csv` | `entity_id` | HI-Small_accounts.csv: Entity ID | 1,108 of 1,108 |  |
| `output/demo_banks.csv` | `bank_id` | HI-Small_accounts.csv: Bank ID | 3 of 3 |  |
| `output/complaints.csv` | `complaint_id` | HI-Small_Patterns.txt: attempt (block) number | 370 of 370 | 370 of 370 attempts have a complaint |
| `output/complaints.csv` | `victim_account_id` | HI-Small_accounts.csv | 370 of 370 |  |
| `output/complaints.csv` | `fraud_transfer_ids` | HI-Small_Trans.csv: data row number | 933 of 933 | each is laundering-labelled, leaves the victim and belongs to the complaint's attempt |
| `labels/ring_devices.csv` | `ring_device_id` | output/used_device.csv | 327 of 327 |  |
| `labels/ring_devices.csv` | `attempt_id` | HI-Small_Patterns.txt | 370 of 370 |  |

Transfer IDs are `T` + the zero-based data row of `HI-Small_Trans.csv`; account IDs are `<Bank ID>-<Account Number>`; complaint `CMP-0007` is the 7th attempt in `HI-Small_Patterns.txt`.

## Checks

| Check | Result | Detail |
| --- | --- | --- |
| Every synthetic key matches a real IBM record | PASS | 11 of 11 mappings complete |
| One truth row per complaint and per IBM attempt | PASS | 370 complaints |
| Every trail transfer is laundering-labelled | PASS | 3,807 trail transfers |
| Every fraud transfer is on its trail at layer 1 | PASS |  |
| Trail transfers only leave accounts the money had already reached | PASS | 0 violations |
| Every trail account has a label | PASS | 0 missing |
| Every complaint victim is flagged in account_labels | PASS |  |
| No account is both mule and genuine | PASS |  |
| A demo pair meets every criterion | PASS | CMP-0011 then CMP-0137 at bank 11 |
| Complaint overlaps list each pair once | PASS | 1,536 pairs |

## Limits

- IBM's labels mark laundering money in general, not which complaint it came from, so a trail can pick up labelled money from another attempt that passes through the same mule. `in_attempt` marks what belongs to the complaint's own attempt.
- Tainted amounts are not ground truth here: the pro-rata engine calculates them. `labelled_inflow_paise` is the laundering-labelled money each trail account received, an upper bound.
- Generator v2 (injected deep chains and an injected held-out pattern) was not built. The Phase 4 held-out pattern is IBM's CYCLE (54 complaints, 15 dev and 39 test), and no chains deeper than IBM's are added.

## Inputs

| File | SHA-256 |
| --- | --- |
| `HI-Small_Trans.csv` | `b19d39f515523373f991b689c07e11e7b0b95c17a2c27a87d91584ae16c5b040` |
| `HI-Small_Patterns.txt` | `7c5029d7ff0baaf01dba98d9ac2c51c98de288bc37317f4ee5edc44eeef3cb37` |
| `HI-Small_accounts.csv` | `786808526e33cfc441212dd6fccda7edfc24172149bed59c6ef59b186836b014` |

## File fingerprints

| File | Rows | SHA-256 |
| --- | --- | --- |
| `labels/account_labels.csv` | 6,357 | `91173465ab599dd3bcd79732ec939b9a4562104f67f29c1722276003a71ac9c9` |
| `labels/complaints_truth.csv` | 370 | `0aadc60dfeaa3a84e59f1899eb0626c7bdcda6a0a4fd4e7526ea7759a5ca0c06` |
| `labels/trail_accounts.csv` | 2,195 | `37f164b1afd0c52c672a8050d3b0ae50edac1b6ea90df5663d0193b67abc780f` |
| `labels/trail_transfers.csv` | 3,807 | `2c889286f47d4edf7a68c8e095992e6f4361369fa4871ed4a02595e537fac991` |
| `labels/complaint_overlaps.csv` | 1,536 | `54f5b2e6657562f4ae79380e56861c370e737e050da5399f29724efcfc657f0f` |
| `labels/demo_pair.csv` | 2 | `758efa2be50c04e7992f015d6cc21799e1e754cf64f6721f5f764b17eed04730` |
| `labels/ring_devices.csv` | 370 | `40dde8b39011b52cd126e3d8c16fa329b3d3e88c21b29ff59e10f91a803e314d` |

## Rebuild

Run `python3 ../synthetic/generate_overlay.py` first (it writes `labels/ring_devices.csv`), then `python3 build_eval_set.py` from this folder. The same inputs give identical files.
