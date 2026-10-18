# sandbox

Phase 2 steps 11-12: the OpenShell sandboxes for the three agents. See `SANDBOX.md` in the repository root.

| File | What it is |
| --- | --- |
| `policies/tracer.yaml`, `adjudicator.yaml`, `verifier.yaml` | one deny-by-default policy per agent (plan Appendix I) |
| `profiles/tracehold-bedrock.yaml`, `tracehold-langfuse.yaml` | OpenShell provider profiles: the model route and Langfuse, with the keys added by OpenShell's proxy |
| `Containerfile.agents`, `requirements.agents.txt` | the agent image (built by `deploy/sandboxes.sh build`) |
| `probe_policy.yaml` | the policy of the one-off probe (`deploy/sandbox_probe.sh`) |
| `probe_output.txt`, `check_output.txt` | written by the probe and by `deploy/sandboxes.sh check` |
