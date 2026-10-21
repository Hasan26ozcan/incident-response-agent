---
id: pm-post-maintenance-regression
type: postmortem
title: Post-mortem: orders degraded after an approved rolling restart changed a pool setting
class: post_maintenance_regression
---
Summary: orders-api timed out on database calls after a planned maintenance had ended successfully. Timeline: the restart refreshed configuration and loaded a smaller connection pool size than before; under normal load the pool emptied. The change record described the window but not the setting. Impact: forty minutes of failed orders. Resolution: previous value restored. Action items: config diff as part of every maintenance, post-window health verification.
