# Operability and cost standards (starter set)

> Replace this file with your organisation's real standards. Reviewers cite these ids, so keep them stable.

- OPS-STD-01 Service levels: every service states its availability, latency and recovery targets (RTO, RPO).
- OPS-STD-02 Monitoring: every service exposes health, latency, error-rate and saturation metrics with alerts.
- OPS-STD-03 Runbooks: each alert links to a runbook; on-call ownership is named before go-live.
- OPS-STD-04 Resilience: production runs across at least two availability zones unless the design justifies less.
- OPS-STD-05 Cost visibility: resources are tagged by service and owner; monthly running cost is estimated in the design.
- OPS-STD-06 Scaling: the design states the expected peak load and how it scales to 2x that peak.
- OPS-STD-07 Change: releases are automated and can be rolled back.
