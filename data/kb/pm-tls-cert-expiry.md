---
id: pm-tls-cert-expiry
type: postmortem
title: Post-mortem: API clients rejected our certificate on the day it lapsed
class: tls_cert_expiry
---
Summary: all API consumers failed at the same minute on a Monday morning. Timeline: the certificate renewal job had silently failed three weeks earlier because its credentials were rotated; nobody was alerted. Impact: complete outage of the public endpoint for forty minutes. Resolution: manual issuance and reload. Action items: expiry alerts at thirty, fourteen and seven days, renewal-job failure alarms, a certificate inventory.
