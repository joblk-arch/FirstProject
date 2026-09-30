# Local AI Operations Dashboard

Private, read-only visibility into local agent workflows, project activity,
model usage, stage duration, and recorded tokens. The browser never receives
the agent gateway credential. Approval, merge, push, deployment, prompts, and
arbitrary execution are deliberately excluded.

The dashboard is served on the M1 by Docker and reads aggregated metrics from
the authenticated agent gateway. Token accounting starts after the metrics
instrumentation is deployed; historical jobs remain explicitly unrecorded.
