---
id: pm-lb-misconfig
type: postmortem
title: Post-mortem: a health check pointed at the wrong path and kept a failing node in rotation
class: lb_misconfig
---
Summary: about a third of requests received bad-gateway responses. Timeline: after a node replacement the health-check path no longer existed, so the balancer treated every response as success from a static page while the application on that node was broken. Impact: persistent errors for one third of users. Resolution: path corrected and the node drained. Action items: health checks that exercise the application, balancer config in version control.
