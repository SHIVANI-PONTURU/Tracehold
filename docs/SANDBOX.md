# TraceHold sandboxes

Each of TraceHold's three agents (Tracer, Adjudicator, Verifier) runs in its own NVIDIA OpenShell sandbox, with a
deny-by-default policy. This file says what runs where, what each sandbox may reach, how the keys
are kept out of the sandboxes, how to run it, and what the checks showed.

## What runs where

| Part | Where | Reachable by the agents? |
| --- | --- | --- |
| Tracer, Adjudicator, Verifier workers | one OpenShell sandbox each (`th-tracer`, `th-adjudicator`, `th-verifier`; OpenShell allows at most 19 characters), image `localhost/tracehold-agent:dev` | (they are the agents) |
| Tool server: MCP paths `/mcp/<agent>/` and the handoff API | the host, port 8000 | yes, each agent only its own MCP path and the handoff calls it makes |
| FalkorDB | the host, `127.0.0.1:6379` | **no** (blocked and logged) |
| Mock bank | the host, `127.0.0.1:8100` | **no**: liens go through the tool server's guarded `execute_lien` |
| Model route: Claude on Amazon Bedrock, us-east-2 | AWS | yes, through the OpenShell proxy, which adds the key |
| Langfuse | Langfuse Cloud | yes, ingestion paths only, through the proxy, which adds the keys |
| Everything else (the internet, other ports, other hosts) | | **no** (blocked and logged) |

The host is a Linux machine with OpenShell's gateway and rootless Podman: an EC2 server (Ubuntu 26.04 LTS,
`m7i.xlarge`, us-west-2), set up by `deploy/ec2_setup.sh` and run by `deploy/ec2.sh` (README, "Run on an EC2 server").
On it, `host.openshell.internal` is set to the server's private address (`host_gateway_ip` in the gateway
configuration), where the tool server listens; FalkorDB and the mock bank listen on 127.0.0.1 only. The probe below
ran earlier in the Ubuntu distribution in WSL 2 on the previous development PC, with the same scripts.

## The policies (`sandbox/policies/`)

| Section | Tracer | Adjudicator | Verifier |
| --- | --- | --- | --- |
| Filesystem | system paths and the code (`/opt/tracehold`) read-only; its working folder `/sandbox` read-write, nothing else | same | same; the HITL gate's checkpoint file lives in `/sandbox` |
| Landlock | `hard_requirement`: the sandbox does not start without it | same | same |
| Process | runs as the `sandbox` user, never root | same | same |
| MCP endpoint | `/mcp/tracer` | `/mcp/adjudicator` | `/mcp/verifier` |
| Handoff API | claim, submit, read, record spend, report a failure, and `POST /handoff/step` (a Step after every node) | same | same, plus list investigations, read gate decisions, record and execute approved liens, finish the gate, and the case chat: `POST /handoff/chat/claim` and `POST /handoff/chat/answer` |
| Model route | `tracehold-bedrock` provider: `bedrock-runtime.us-east-2.amazonaws.com`, `/model/**` | same | same |
| Langfuse | `tracehold-langfuse` provider: `/api/public/**` on the Langfuse host | same | same |
| Everything else | blocked and logged | blocked and logged | blocked and logged |

Every rule names the agent's Python interpreter (`/usr/local/bin/python3.13`) as its only binary, so no other
program in the sandbox can use it. Rules are enforced (`enforcement: enforce`), not just logged. The gate decision
itself (`/handoff/gate/decide`) is not open to any agent: only the UI, outside the sandboxes, can decide. The same
holds for every other UI route (undo, reviews, memory, trail, chat ask and read, close, banks).

## Keys

`.env` is read by `deploy/sandboxes.sh providers`, outside the sandboxes, and the Bedrock API key and the two
Langfuse keys are stored in the OpenShell gateway as provider credentials (`sandbox/profiles/`). Inside a sandbox the
same variable names hold placeholders. OpenShell's proxy replaces a placeholder with the real value only in requests
to that provider's endpoint, and only from the listed binary. Deleting a sandbox purges its injected credentials.

Earlier OpenShell versions sent model calls through an "inference router"; OpenShell 0.1.x replaced it with these
provider profiles. The goal is the same: no key inside a sandbox.

## Running it

On the Linux or WSL host, from the repository folder:

```bash
bash deploy/sandbox_probe.sh        # once: host reachability, blocking and logging, no keys (sandbox/probe_output.txt)
bash deploy/wsl_stack.sh setup      # once: Python 3.13 environment, packages, the work folder
bash deploy/sandboxes.sh build      # the agent image
bash deploy/sandboxes.sh providers  # the keys from .env into the gateway
bash deploy/wsl_stack.sh start      # FalkorDB, mock bank, tool server, UI (http://localhost:8501)
bash deploy/sandboxes.sh start      # the three sandboxed agents
bash deploy/sandboxes.sh check      # the blocked-request and key checks (sandbox/check_output.txt)
```

`bash deploy/sandboxes.sh logs tracer` shows a sandbox's policy log: every allowed and denied connection and
request, with the binary, the destination and the reason.

A sandbox runs a copy of the agents' code, and the filesystem, Landlock and process sections of its policy are fixed
when it starts. After pushing new code or policies to the EC2 server, run `update`: it stops the stack, rebuilds the
agent image, starts the stack again and creates the three sandboxes again, so the new code and the current policy
files apply:

```bash
bash deploy/ec2.sh update
```

Avoid it while a case waits at the gate: the Verifier's checkpoint file lives in its sandbox, so the gate is then
replayed from the stored verdict handoff instead of resumed.

## Probe (before the agents moved in)

`deploy/sandbox_probe.sh` on the EC2 server (17 Oct, 00:02 London; OpenShell 0.1.4, Podman 5.7.0, kernel
7.0.0-1006-aws, `sandbox/probe_output.txt`), with a one-rule policy (`sandbox/probe_policy.yaml`), from inside a
`python:3.13-slim` sandbox. The same probe gave the same results on 16 Oct in WSL 2 on the previous PC.

| Request or action | Result | Policy log |
| --- | --- | --- |
| `GET http://host.openshell.internal:8765/` (a test service on the host, allowed; resolves to the server's private address) | 200 | `ALLOWED ... python3.13 -> host.openshell.internal:8765 [policy:probe_host_service]` |
| `GET https://www.python.org/` (allowed, TLS through the proxy) | 200 | `ALLOWED ... -> www.python.org:443` |
| `http://host.openshell.internal:6379/` (FalkorDB's port) | refused | `DENIED /usr/local/bin/python3.13 -> 198.18.0.2:6379 [reason:transparent_tcp_mapping_denied]` |
| `https://example.com/` (a public website) | refused | `DENIED example.com [reason:policy_dns_ineligible]`, `DENIED ... -> example.com:443 [reason:transparent_tcp_policy_denied]` |
| write to `/var/tmp` (not in the policy) | `PermissionError` (Landlock) | |
| user, interpreter, working folder | UID 1000, `/usr/local/bin/python3.13`, `/sandbox` | |
| key-like environment variables | none from TraceHold (only the base image's `GPG_KEY`, Python's public signing key ID) | |

Traffic leaves through OpenShell's transparent proxy (no proxy variables in the sandbox); OpenShell's own CA is
set through `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE`, which the agents' HTTP clients read.

## Checks

`deploy/sandboxes.sh check` on the EC2 server (the first one), 17 Oct 00:00 London, from inside the running Tracer
sandbox. Every run overwrites `sandbox/check_output.txt`, which now holds the moved server's run (see "The moved
server"):

| Check | Result |
| --- | --- |
| From the Tracer sandbox, requests to FalkorDB's port and to a public website are blocked and logged | **Pass.** FalkorDB 6379 (HTTP and raw TCP), the mock bank 8100 and `https://example.com/` refused; each one logged as `DENIED` with its reason (`transparent_tcp_mapping_denied`, `policy_dns_ineligible`, `transparent_tcp_policy_denied`) |
| The Tracer reaches only its own tools | **Pass.** `/mcp/tracer/` and its handoff calls go through; `/mcp/verifier/` and `/handoff/gate/decide` answer 403 and are logged as `L7_REQUEST deny ... not permitted by policy` |
| From the Tracer sandbox, the model route works | **Pass.** A 16-token request from inside `th-tracer` to Claude Opus 4.6 on Bedrock answered "OK"; the proxy added the key |
| The environment inside a sandbox shows no Bedrock key | **Pass.** `BEDROCK_API_KEY` holds a 59-character placeholder, not the key (compared by hash); the Langfuse variables are placeholders too |
| Complaint 1 runs through all three sandboxed agents to executed liens | **Pass.** Investigation `INV-0011-9059fa17d29d`: status `executed`, 2 lien requests and 4 information requests sent. It ran again on the moved server, see "The moved server" |

Listening ports on the server: the tool server on `0.0.0.0:8000` (the sandboxes reach it at the private address;
the security group keeps it closed to the internet); FalkorDB 6379, the FalkorDB Browser 3000, the mock bank 8100 and
the UI 8501 on `127.0.0.1` only.

## The moved server

On 18 Oct the server moved by AMI to a second AWS account: the same instance type (`m7i.xlarge`) in us-west-2, the same
image (Ubuntu 26.04, Podman 5.7.0, OpenShell 0.1.4, the same three policy files) and a new private address. The stack
was stopped cleanly before the image was made. Bedrock stays in the first account and is reached with the Bedrock API
key, so the model route in the policies did not change.

`bash deploy/ec2.sh check` on the moved server, 18 Oct 02:54 UTC (03:54 London), from inside the running Tracer
sandbox. `sandbox/check_output.txt` holds this run (19 lines); these are its header and result rows:

```text
== TraceHold sandbox checks, 2026-10-18T02:54:15Z, OpenShell 0.1.4
its handoff API (spend read)             GET  http://host.openshell.internal:8000/handoff/spend -> 200 allowed
its MCP path /mcp/tracer/                POST http://host.openshell.internal:8000/mcp/tracer/  -> allowed: reached the tool server, which answered HTTP 406 ({"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Not Accep)
another agent MCP path /mcp/verifier/    POST http://host.openshell.internal:8000/mcp/verifier/ -> HTTP 403 blocked by policy
the gate decision API                    POST http://host.openshell.internal:8000/handoff/gate/decide -> HTTP 403 blocked by policy
FalkorDB port 6379                       GET  http://host.openshell.internal:6379/             -> blocked (URLError: <urlopen error [Errno 13] Permission denied>)
the mock bank port 8100                  GET  http://host.openshell.internal:8100/health       -> blocked (URLError: <urlopen error [Errno 13] Permission denied>)
a public website                         GET  https://example.com/                             -> blocked (URLError: <urlopen error [Errno 13] Permission denied>)
raw TCP to FalkorDB 6379: blocked (PermissionError)
BEDROCK_API_KEY inside the sandbox: a placeholder (59 characters)
environment names with KEY or SECRET: ['BEDROCK_API_KEY', 'GPG_KEY', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY']
```

Same pattern as on the first server. The Tracer's own handoff read and MCP path reach the tool server (the 406 is the
MCP server's own answer to the check's empty request, so the path is open). Another agent's MCP path and the gate
decision API are refused by policy with 403. FalkorDB, the mock bank and `https://example.com/` are blocked, and so is
a raw TCP connection to FalkorDB. The key is a 59-character placeholder; `GPG_KEY` is the base image's. The policy log
lists seven `DENIED` lines for the run: two `L7_REQUEST deny` (the two 403 paths) and five network denials
(`transparent_tcp_mapping_denied` for port 6379 twice, for the HTTP request and for the raw connection, and once for
port 8100; `policy_dns_ineligible` and `transparent_tcp_policy_denied` for example.com).

Before this check the sandboxes could not reach the tool server at all (next section). A live complaint (CMP-0011 at
bank 11) then went through the three sandboxed agents to the gate and, once approved, to two executed lien requests
(case `INV-0011-e4ed483b178b`, 8 model calls).

## The gateway address

A sandbox reaches the tool server as `host.openshell.internal`. On native Linux OpenShell resolves that name to
`host_gateway_ip` in `~/.config/openshell/gateway.toml` (loopback by default, which a sandbox may not reach, see the
table below), and `deploy/ec2_setup.sh` writes this server's private address there. The value is tied to the machine.
A server launched from an AMI has a new private address while the file keeps the old one, so the sandboxes send their
requests to an address that is not this server and time out. That is what happened after the move: the agents' workers
raised `ReadTimeout`, and the check reported the Tracer's own paths as blocked with `TimeoutError`, although the policy
allows them. It was fixed by hand first: set the new address, preflight the file, restart the gateway, recreate the
sandboxes.

`gateway_ip_current` in `deploy/sandboxes.sh` does this now. `gateway_ready` calls it on every `sandboxes.sh start`, so
every `ec2.sh start` and `update` runs it before the sandboxes are created. It asks the instance metadata service
(IMDSv2: a token request, then `local-ipv4`) for this server's private address and compares it with `host_gateway_ip`.
If they differ it prints the change, copies the file to `gateway.toml.bak.<unix time>`, rewrites the line, runs
`openshell-gateway config preflight` on it and restarts the gateway (`systemctl --user restart openshell-gateway`). If
there is no metadata service (WSL) or the file has no `host_gateway_ip` line, it leaves the file alone.

Tested on a copy of `gateway.toml` with the gateway restart stubbed: it rewrote a stale address, made one backup and
changed nothing on a second run. That test did not restart a real gateway.

## What OpenShell 0.1.4 required that the first draft missed

| Problem on the first EC2 run | Fix |
| --- | --- |
| Provider profile lint: "protocol requires rules or access to define allowed traffic" | Each provider endpoint now lists its rules: Bedrock `POST /model/**`; Langfuse `POST` and `GET /api/public/**` |
| "unsupported provider profile category: observability" | Langfuse profile category `other` |
| `providers` could not run twice (lint refuses an existing profile ID; updates need the gateway's resource version) | `deploy/sandboxes.sh providers` deletes the two providers and profiles, then imports them again |
| "name exceeds maximum length (21 > 19)" for `tracehold-adjudicator` | Sandboxes are named `th-tracer`, `th-adjudicator`, `th-verifier` |
| On native Linux, `host.openshell.internal` defaults to loopback, which the sandboxes may not reach | `host_gateway_ip` in `~/.config/openshell/gateway.toml` is the server's private address (`deploy/ec2_setup.sh` writes it; `gateway_ip_current` in `deploy/sandboxes.sh` keeps it right after a move, see "The gateway address") |

## Versions

OpenShell 0.1.4 (gateway with the Podman driver, `~/.config/openshell/gateway.toml`, schema version 2, telemetry off
in `~/.config/openshell/gateway.env`), Podman 5.7.0 rootless with cgroups v2, Ubuntu 26.04 LTS with Linux kernel
7.0.0-1006-aws and Landlock enabled, agent image based on `python:3.13-slim` with the packages pinned by
`requirements.lock.txt`.
