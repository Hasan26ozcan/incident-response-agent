---
id: pm-n-plus-one-query
type: postmortem
title: Post-mortem: cart page slowed by one query per line item
class: n_plus_one_query
---
Summary: cart rendering became slow for customers with large carts. Timeline: a refactor changed an association to lazy loading, so rendering a cart with fifty items ran fifty-one queries. Database CPU doubled under normal traffic. Impact: slow pages and high pool usage. Resolution: eager loading restored. Action items: query-count assertions in tests, alert on queries per request.
