# Architecture standards (starter set)

> Replace this file with your organisation's real standards. Reviewers cite these ids, so keep them stable.

- ARCH-01 Reuse before build: use an existing shared platform when one meets the need; justify any new one in an ADR.
- ARCH-02 Cloud first: new workloads run on the approved cloud platform using managed services where available.
- ARCH-03 Clear ownership: every component has one owning team named in the design.
- ARCH-04 Contracts: services talk through documented, versioned APIs or events, never through each other's databases.
- ARCH-05 Loose coupling: a failure in one component must not cascade; use timeouts, retries with backoff and queues.
- ARCH-06 Infrastructure as code: all environments are created from code, with no manual changes in production.
- ARCH-07 Decisions recorded: significant choices (technology, vendor, pattern) are captured as ADRs.
- ARCH-08 Requirement traceability: every component traces to the requirements it serves.
