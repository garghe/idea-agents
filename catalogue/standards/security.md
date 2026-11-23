# Security standards (starter set)

> Replace this file with your organisation's real policies. Reviewers cite these ids, so keep them stable.

- SEC-STD-01 Single sign-on: people authenticate through the corporate identity provider; no local user stores.
- SEC-STD-02 Least privilege: each service and person gets only the permissions the task needs.
- SEC-STD-03 Encryption: data is encrypted in transit (TLS 1.2+) and at rest with managed keys.
- SEC-STD-04 Secrets: credentials live in a secrets manager, never in code, images or configuration files.
- SEC-STD-05 Personal data: personal data is classified, minimised, and kept no longer than its stated retention.
- SEC-STD-06 Logging: security-relevant events are logged centrally and kept for at least 12 months.
- SEC-STD-07 Network: internal services are not reachable from the internet unless the design justifies it.
- SEC-STD-08 Third parties: new vendors handling company data pass a security assessment before go-live.
