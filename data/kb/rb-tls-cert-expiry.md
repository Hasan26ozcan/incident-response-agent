---
id: rb-tls-cert-expiry
type: runbook
title: Runbook: a TLS certificate has expired
class: tls_cert_expiry
---
Symptoms: handshake failures, clients rejecting the connection because the certificate is no longer valid, an abrupt start exactly at the expiry time, and a failure rate close to total for affected endpoints. Server-side resource metrics look normal.
First checks: read the certificate's validity dates from the endpoint; identify which certificate is in use (edge, mesh, internal service); confirm the renewal job and why it did not run.
Remediation: issue or renew the certificate and reload the terminating component (changing certificates on a production endpoint is a medium to high risk change); verify clients can connect. Then fix the automation and add expiry monitoring weeks in advance.
