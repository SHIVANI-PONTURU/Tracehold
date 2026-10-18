# TraceHold synthetic data report (overlay v1)

Generated 2026-10-14 13:11 IST by `generate_overlay.py` with seed 20261015 in 27 s. 12 of 12 checks pass.

Only data that is not already in IBM's three files is written here. Banks, owners and owner types come straight from `HI-Small_accounts.csv`; laundering attempts from `HI-Small_Patterns.txt`.

## Inputs

| File | Rows | Notes | SHA-256 |
| --- | --- | --- | --- |
| `HI-Small_Trans.csv` | 5,078,345 transfers | 2022-09-01 00:00 IST to 2022-09-18 16:18 IST; 515,088 accounts | `b19d39f515523373f991b689c07e11e7b0b95c17a2c27a87d91584ae16c5b040` |
| `HI-Small_accounts.csv` | 518,581 accounts | 30,470 banks; 166,207 owners; 3,493 accounts never transfer | `786808526e33cfc441212dd6fccda7edfc24172149bed59c6ef59b186836b014` |
| `HI-Small_Patterns.txt` | 370 attempts, 3,209 rows | 100.00% of rows matched to a transfer | `7c5029d7ff0baaf01dba98d9ac2c51c98de288bc37317f4ee5edc44eeef3cb37` |

## What was created

| File | Rows | What it holds |
| --- | --- | --- |
| `output/account_overlay.csv` | 518,581 | Opening date and opening balance per account: a generated buffer on top of the calculated minimum |
| `output/registered_merchants.csv` | 1,108 | Owners (`Entity ID`) that are registered merchants: business owners in the top 1% by distinct payers |
| `output/demo_banks.csv` | 3 | The demo banks: IBM banks with the most accounts in laundering attempts |
| `output/used_device.csv` | 677,036 | Which account used which device, from when (generated) |
| `output/has_phone.csv` | 168,966 | Which owner uses which phone (generated) |
| `output/logged_in_from.csv` | 1,050,790 | Which account logged in from which IP, from when (generated, private 10.0.0.0/8 range) |
| `output/complaints.csv` | 370 | One complaint per IBM attempt; report times generated |
| `../eval/labels/ring_devices.csv` | 370 | The shared device and phone of each laundering attempt. Ground truth, part of the evaluation set: never load into the graph |
| `fx_rates.csv` | 15 | Exchange rates used for the balances; the cleaning step must use the same table |

## How the graph uses these with IBM's files

- `Bank`: one per `Bank ID` in `HI-Small_accounts.csv`, named with its `Bank Name`. Demo banks from `demo_banks.csv`.
- `Customer`: one per `Entity ID` in `HI-Small_accounts.csv` (166,207 owners). `kind` is person for Individual and Sole Proprietorship owners (55,503) and business otherwise (110,704); `registered_merchant` is true for owners in `registered_merchants.csv`.
- `Account`: one per row of `HI-Small_accounts.csv`, with ID `<Bank ID>-<Account Number>`; `opened_at` and `opening_balance` from `account_overlay.csv`.
- `Device`, `Phone`, `IP`: the distinct IDs in `used_device.csv`, `has_phone.csv` and `logged_in_from.csv`.
- `Complaint`: `complaints.csv`. Complaint `CMP-0007` is attempt 7 (the 7th block) in `HI-Small_Patterns.txt`.

## Measured rates

| Signal | Setting | Measured |
| --- | --- | --- |
| Accounts in an attempt on its shared device | 0.6 | 0.618 |
| Owners in an attempt on its shared phone | 0.4 | 0.390 |
| Owners sharing a household device | 0.03 | 0.0300 |
| Accounts outside attempts on a shared household device | n/a | 0.0256 |
| Owners with a second device | 0.3 | 0.300 |
| Cross-currency transfers whose converted amounts differ by more than 1% | n/a | 1,615 of 72,170 |

## Checks

| Check | Result | Detail |
| --- | --- | --- |
| No running balance goes below zero | PASS | 0 accounts below zero (outflows processed before inflows within each minute) |
| Every account opens before its first transfer | PASS | 0 accounts open too late |
| Device and IP first_seen never before the account opens | PASS | 0 links violate it |
| Accounts in an attempt share its device at the set rate | PASS | measured 0.618, set 0.6 |
| Owners in an attempt share its phone at the set rate | PASS | measured 0.390, set 0.4 |
| Owners in households share a device at the noise rate | PASS | measured 0.0300, set 0.03 |
| Each disputed amount equals the sum of its fraud transfers | PASS | 370 complaints |
| Every fraud transfer leaves the victim's account | PASS | 370 complaints |
| Pattern rows matched to transfers | PASS | 100.00% of 3,209 rows |
| Account IDs are unique and no link is listed twice | PASS | 0 duplicate links |
| Every account is in IBM's accounts file | PASS | 0 accounts missing |
| No label or ground-truth columns in output/ | PASS | none found |

## Demo banks

| Slot | Bank ID | IBM name | Accounts in laundering attempts |
| --- | --- | --- | --- |
| 1 | 12 | National Bank of the East | 74 |
| 2 | 20 | China Bank #6 | 67 |
| 3 | 11 | Finland Bank #0 | 54 |

## Exchange rates

One amount per transfer, from the amount paid. Rates are the median implied rate per currency from IBM's cross-currency transfers, via US dollars; rupees per US dollar = 73.4440, from IBM's own Rupee rate.

## Notes

- Transfer IDs are `T` + the zero-based row number in `HI-Small_Trans.csv` (header excluded), zero-padded to 9 digits. Times are UTC epoch seconds; IBM's times are read as IST wall-clock.
- Minimum feasible balances are computed over the whole file with outflows before inflows in the same minute, so balances stay non-negative for any time-prefix slice and either same-minute order.
- Ring devices and phones are the only label-derived data; their attempts are recorded only in `../eval/labels/ring_devices.csv`. Set `injected_signals.enabled: false` for the ablation.
- Complaints: the victim is the sender of the attempt's earliest transfer between two different accounts; fraud transfers are the victim's transfers to other accounts in that attempt.

## File fingerprints

| File | Rows | SHA-256 |
| --- | --- | --- |
| `output/account_overlay.csv` | 518,581 | `3f64b8f7e14410473134d5d07b6f1f6c97bdce1a37cbf9145b751629a94528f1` |
| `output/registered_merchants.csv` | 1,108 | `fa9e837bc20bb481ce70be71b0b3f40164fcce85269d0b850190d71983c78b37` |
| `output/demo_banks.csv` | 3 | `6e62723d6615113b1ea41b22fbe399e1da8c74faf2137ba3662c76a583827384` |
| `output/used_device.csv` | 677,036 | `a2bef2ffff437c2827def93593e4527ccbca3ebce08da9fa2203eec2fb60860d` |
| `output/has_phone.csv` | 168,966 | `d43eb0160939155dfea8800e1dedd405d41c277f464c0351068e933e28353e6a` |
| `output/logged_in_from.csv` | 1,050,790 | `d20660472fd15f8a4b425a785cf0a249929367870e759f3fa6d8e28c4ad021da` |
| `output/complaints.csv` | 370 | `ffd9acf564d4b978f66cb4cdcbdc39dfd94a604fbed944a77a5f3c1283134206` |
| `../eval/labels/ring_devices.csv` | 370 | `40dde8b39011b52cd126e3d8c16fa329b3d3e88c21b29ff59e10f91a803e314d` |
| `fx_rates.csv` | 15 | `cb3209c23503e4061c09bec4f4162e096244e81e684e4b01f227ed1b3543a03d` |

## Rerun

`python3 generate_overlay.py` from this folder. The same seed and inputs give identical files and fingerprints.
