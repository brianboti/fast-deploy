# Research and review records

Read-only reviews written while the services were designed. Each names its method and marks its sources; none changed
the code when it was written. They are kept as the reasons behind the design, in the order they were written:

| Document | Question it answers | Version reviewed |
|---|---|---|
| [REFACTOR-2026-10-28.md](REFACTOR-2026-10-28.md) | How to cut a provider-independent infrastructure layer out of the earlier release controller: the mixed responsibilities, the target architecture, the catalog, the contract, the Google adapter | Fast Deploy 1.34.0 (the controller, since removed) |
| [RESEARCH-2026-10-28-infrastructure-layer.md](RESEARCH-2026-10-28-infrastructure-layer.md) | Is the infrastructure layer built the way the MCP specification, industry practice and the research say it should be; which kinds of infrastructure to add, in which order | cloudinfra 1.36.0 |
| [RESEARCH-2026-10-28-positioning.md](RESEARCH-2026-10-28-positioning.md) | Where the layer stands among the tools companies use, and what makes it practical: the three releases A, B and C, the service's identity and backstops | cloudinfra 1.36.1 |
| [RESEARCH-2026-10-29-delivery-layer.md](RESEARCH-2026-10-29-delivery-layer.md) | Whether a small separate delivery service above the infrastructure layer is the right shape, checked against Cloud Deploy, the CNCF platform papers, OpenGitOps, AWS and Score | cloudinfra 1.37.1 |

What these documents say about the earlier Fast Deploy workflow (`labdeploy`) describes code that was removed from the
repository on 2026-10-30; git history keeps it. Release A of the positioning review shipped as 1.37.0 and 1.37.1; the
delivery service's four phases are in [../DELIVERY-SPEC.md](../DELIVERY-SPEC.md) and its runs in
[../DELIVERY-SPIKE.md](../DELIVERY-SPIKE.md). Releases B (governance: teams, environments, budgets, approvals, audit
export) and C (SSO, durable state, AWS and Azure, drift and import) remain the plan.
