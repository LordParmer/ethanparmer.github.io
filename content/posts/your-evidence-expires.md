---
title: "Your evidence expires: building an audit-evidence layer you own"
date: 2026-11-22
draft: false
tags: [databricks, gxp, data-integrity, unity-catalog, audit-trail]
description: "Databricks telemetry ages out on its own schedule. How to capture, protect and rehearse an audit-evidence layer you own."
---

An inspector is reviewing an out-of-specification investigation from early last year. The data part goes fine. Bronze is append-only, and the SCD Type 2 history table shows the record as it was at release, as described in [my ALCOA+ map]({{< relref "gxp-ready-pipelines-on-databricks.md" >}}). Then comes the follow-up: *which job wrote that correction, what code was it running, and who changed the job the week before?*

The Jobs UI keeps 60 days of runs. `system.lakeflow.job_run_timeline` keeps 395 days, so the run is gone. `system.access.audit` also keeps 395 days, so the job edit is gone. Delta history on the table keeps 30 days unless someone changed it. The value is still in the table. Nothing that explains how it got there survived.

That scenario is a composite, but every number in it is a documented default. This post expands one line from the first post's "where the platform stops" list: **the platform's operational telemetry is not your evidence record.** Its retention is set for operations. Your record-retention period is set by your QA and regulatory function, and nothing on the platform connects the two. If you want that evidence in three years, you have to build somewhere to keep it.

## Know what ages out, and when

These are the sources I treat as evidence, with their defaults on Azure Databricks as of November 2026:

| Source | Default retention | Configurable? | What I capture | Lands in |
|---|---|---|---|---|
| `system.access.audit` (Public Preview) | 395 days | Yes, account-wide | All events: Unity Catalog, jobs, permissions | `gxp_evidence.audit.platform_audit` |
| `system.access.table_lineage`, `column_lineage` | 395 days | Yes, account-wide | Rows touching GxP catalogs | `gxp_evidence.lineage.*` |
| `system.lakeflow.jobs`, `job_tasks` (SCD2) | 395 days, latest record kept | Yes, account-wide | Every version of every GxP job | `gxp_evidence.lakeflow.jobs` |
| `system.lakeflow.job_run_timeline`, `job_task_run_timeline` | 395 days | Yes, account-wide | All run slices for GxP jobs | `gxp_evidence.lakeflow.job_run_timeline` |
| `system.lakeflow.pipelines` (SCD2), `pipeline_update_timeline` | Indefinite; 395 days | Pipelines: no. Updates: yes | Pipeline settings versions, every update | `gxp_evidence.lakeflow.*` |
| Pipeline event log, published to Unity Catalog | Not documented | Not documented | `flow_progress` expectation metrics, `user_action` | `gxp_evidence.pipelines.event_log` |
| Jobs UI run history | 60 days | No | Nothing directly; it's a view over the above | — |
| Delta table history (`DESCRIBE HISTORY`) | 30 days (`delta.logRetentionDuration`) | Yes, per table | Every commit on GxP tables | `gxp_evidence.history.table_commits` |

A few details from the reference pages matter more than they look:

- Expectation results aren't in a system table. They're in the pipeline event log under `details:flow_progress:data_quality:expectations`, and the docs say events are delivered "on a best-effort basis." If a check's result is itself a GxP record, compute it as data in your pipeline and write it to a table you own. Don't rebuild it later from the event log.
- If a workspace is deleted, its audit events older than 14 days are removed from `system.access.audit`. Decommissioning a workspace is an evidence event, so capture before you delete.
- Lineage rows are emitted only "when lineage can be inferred." The Catalog Explorer lineage graph keeps lineage captured since September 2024 indefinitely, but the system table keeps 395 days.
- A system table is read-only, but it isn't fixed. Databricks deletes records past retention and adds columns when it needs to.

{{< callout type="gxp" title="The trap" >}}
`system.lakeflow.jobs` keeps the most recent record for each job even after it passes the retention period. So a query for an old job still returns a row, and the history looks complete. The versions in between, including what the job looked like on the day it ran, still age out. The jobs table also records *that* a definition changed, not *who* changed it. That's in the audit log.
{{< /callout >}}

## Configurable retention: turn it on, then don't rely on it

On November 18, 2026, Azure Databricks made configurable retention for system tables generally available. An account admin sets one account-wide retention period from 30 to 3,650 days. The setting is in the account console under **Settings → Account settings → System Tables data retention (days)**, or in the Account Settings API as the setting `st_retention`. The first 395 days are free. Data kept longer than that is billed as storage.

Set it, and set it from your requirement rather than to the maximum by reflex. Just don't confuse it with owning the record. The documented limits explain why:

- **No backfill.** Raising the period doesn't bring back records that have already aged out.
- **One number, held by one role.** It applies to the whole account and is changed by account admins, who usually sit outside your GxP change control. If it's lowered, Databricks waits seven days before applying the change, then typically deletes records that have aged out within a week. Two weeks from one setting change to permanent deletion.
- **No disaster-recovery guarantee.** Databricks doesn't guarantee recovery of all system table data for the configured period after a disaster.
- **Gaps in coverage.** The setting doesn't apply to `system.data_classification`, `system.data_quality_monitoring` or `system.lakebase`. It doesn't touch Delta table history, the pipeline event log, or anything about your code version. Those were never in system tables to begin with.
- **A ceiling.** 3,650 days is ten years. Some record-retention periods are longer.

Configurable retention lets the platform keep its copy for longer, which is worth having. The platform still owns that copy. You can't control its schema, its deletion schedule or who can change its setting.

{{< callout type="gxp" title="The trap" >}}
Raising retention doesn't backfill. The day you turn it on is the earliest date it can help you, and the same is true of the evidence layer below. Start both now, even if the rest of the design isn't finished.
{{< /callout >}}

## The evidence layer

The pattern is simple: a dedicated catalog, an append-only copy of each source keyed on a stable ID, and capture jobs that you can rerun at any time without creating duplicates.

### A catalog with its own storage

```sql
CREATE CATALOG IF NOT EXISTS gxp_evidence
  MANAGED LOCATION 'abfss://evidence@<storage-account>.dfs.core.windows.net/';

CREATE SCHEMA IF NOT EXISTS gxp_evidence.audit;     -- system.access.audit
CREATE SCHEMA IF NOT EXISTS gxp_evidence.lakeflow;  -- jobs, runs, pipelines
CREATE SCHEMA IF NOT EXISTS gxp_evidence.history;   -- Delta commits, run context
CREATE SCHEMA IF NOT EXISTS gxp_evidence.control;   -- capture log, drift checks
```

A separate storage account lets you put resource locks, storage logging and network rules on the evidence without affecting anything else.

### Append-only tables shaped like their source

Create each evidence table from its source with zero rows, so the shape matches, and add two capture columns:

```sql
CREATE TABLE IF NOT EXISTS gxp_evidence.audit.platform_audit
CLUSTER BY (event_date)
TBLPROPERTIES (
  'delta.appendOnly' = 'true',
  -- Set both from your record-retention requirement, not from this example
  'delta.logRetentionDuration' = 'interval 3650 days',
  'delta.deletedFileRetentionDuration' = 'interval 3650 days'
)
AS SELECT *,
  CAST(NULL AS TIMESTAMP) AS captured_at,
  CAST(NULL AS STRING)    AS capture_run_id
FROM system.access.audit
WHERE false;
```

These are the same properties I put on bronze in the first post, for the same reason. The evidence of how the data got there deserves at least the same protection as the data itself.

### Incremental, replay-safe capture

Each source needs a key that stays the same every time you read it:

- audit: `event_id`
- lineage: `record_id`
- run timelines: `(workspace_id, run_id, period_start_time)`, because runs longer than an hour are split into hourly slices
- SCD2 tables: `(workspace_id, job_id, change_time)` or `(workspace_id, pipeline_id, change_time)`
- Delta history: table ID and `version`

The high-water mark comes from the evidence table itself, so there's no separate state that could drift. Each run goes back a few days before it, because system tables aren't real-time and records can arrive late:

```sql
MERGE INTO gxp_evidence.audit.platform_audit AS t
USING (
  SELECT *, current_timestamp() AS captured_at, :capture_run_id AS capture_run_id
  FROM system.access.audit
  WHERE event_date >= (
    SELECT coalesce(date_sub(max(event_date), 3), DATE'1970-01-01')
    FROM gxp_evidence.audit.platform_audit
  )
) AS s
ON  t.event_date = s.event_date
AND t.event_id   = s.event_id
WHEN NOT MATCHED THEN INSERT *;
```

The MERGE only inserts, so it never needs to update or delete. Overlapping windows, retries and reruns after a failure do nothing to rows that are already there. The first run backfills whatever the platform still holds. Set `capture_run_id` to `{{job.run_id}}` in the job's parameters. Then give the job `max_concurrent_runs: 1` and run it daily. That leaves plenty of margin, even against the seven-day `VACUUM` window that would apply if you streamed from system tables instead.

After each run, append a row to `gxp_evidence.control.capture_log` with the source, window and rows inserted. Then reconcile daily counts against the source for the days both still hold. That reconciliation shows the capture is complete, which matters as much as having the copy.

{{< callout type="gxp" title="The trap" >}}
New columns can appear in system tables at any time. Without schema evolution, `INSERT *` quietly drops source columns the target doesn't have. Check before every MERGE, and fail loudly so the change goes through assessment rather than getting absorbed.
{{< /callout >}}

```python
src = set(spark.table("system.access.audit").columns)
tgt = set(spark.table("gxp_evidence.audit.platform_audit").columns)
if new := sorted(src - tgt):
    raise RuntimeError(f"New source columns {new}: assess, then evolve under change control")
```

### Delta history for the tables that matter

`DESCRIBE HISTORY` holds the record of which operation changed a GxP table, under which identity and from which job. By default it lasts 30 days. Capture it daily from a list of tables kept in Git:

```python
from delta.tables import DeltaTable
from pyspark.sql import functions as F

GXP_TABLES = ["gxp.bronze.mes_events", "gxp.silver.batch_records_history"]
target = DeltaTable.forName(spark, "gxp_evidence.history.table_commits")

for name in GXP_TABLES:
    table_id = spark.sql(f"DESCRIBE DETAIL {name}").first()["id"]  # versions restart if a table is recreated
    commits = (DeltaTable.forName(spark, name).history()
        .select(F.lit(name).alias("table_full_name"), F.lit(table_id).alias("table_id"),
                "version", "timestamp", "userName", "operation",
                "operationParameters", "operationMetrics", "job", "userMetadata")
        .withColumn("captured_at", F.current_timestamp()))
    (target.alias("t")
        .merge(commits.alias("s"), "t.table_id = s.table_id AND t.version = s.version")
        .whenNotMatchedInsertAll()
        .execute())
```

## Capture run context the platform doesn't

System tables can tell you a run happened. They can't tell you which commit of your code it ran, because no dynamic value reference exposes the Git commit. So stamp it at deploy time and record it at run time. In the bundle:

```yaml
parameters:
  - name: job_run_id
    default: "{{job.run_id}}"
  - name: trigger_type
    default: "{{job.trigger.type}}"
  - name: repair_count
    default: "{{job.repair_count}}"
  - name: code_version
    default: ${bundle.git.commit}   # resolved when CI deploys the bundle
```

The first task of every GxP job appends one row. The second line below carries the same context into every commit the job makes:

```python
import json

spark.sql("""
  INSERT INTO gxp_evidence.history.run_context
  SELECT :job_run_id, :repair_count, :trigger_type, :code_version, current_user(), current_timestamp()
""", args={k: dbutils.widgets.get(k) for k in ["job_run_id", "repair_count", "trigger_type", "code_version"]})

ctx = json.dumps({"job_run_id": dbutils.widgets.get("job_run_id"),
                  "code_version": dbutils.widgets.get("code_version")})
df.write.option("userMetadata", ctx).mode("append").saveAsTable("gxp.silver.consumption")
```

Use the `userMetadata` writer option rather than the `spark.databricks.delta.commitInfo.userMetadata` Spark conf. The conf isn't supported on serverless. If your jobs run from a Git source instead of a deployed bundle, the Jobs API returns the commit for each run as `git_source.git_snapshot.used_commit`.

Each repair adds a row with a higher `repair_count`, so a repaired run shows every attempt instead of overwriting the first.

### Lineage keys that join

The point of all this is that the evidence joins together without anyone reconstructing it from memory:

- GxP rows carry the job run ID or pipeline `update_id` (the first post's gap 5).
- Delta commits carry the same ID in `userMetadata`.
- `job_run_timeline.run_id` and `run_context.job_run_id` match it.
- `jobs` joins on `(workspace_id, job_id)` as of the run's start time.
- Audit events for the job are filtered on `service_name = 'jobs'`.
- Lineage rows identify the job through `entity_metadata.job_info.job_run_id`.

## Protect the evidence

### Grants

```sql
-- Owned by the deployment service principal, which only runs from CI after approval
ALTER CATALOG gxp_evidence OWNER TO `<deploy-sp-application-id>`;

-- The capture principal reads the system schemas and appends to the evidence layer
GRANT USE CATALOG ON CATALOG system TO `<capture-sp-application-id>`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.access   TO `<capture-sp-application-id>`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.lakeflow TO `<capture-sp-application-id>`;
GRANT USE CATALOG, USE SCHEMA, SELECT, MODIFY ON CATALOG gxp_evidence TO `<capture-sp-application-id>`;

-- QA, CSV and inspection support can read, never write
GRANT USE CATALOG, USE SCHEMA, SELECT ON CATALOG gxp_evidence TO `gxp-evidence-readers`;
```

`MODIFY` would normally let the capture principal update and delete. `delta.appendOnly` blocks that, and only the owner, or a principal with `MANAGE`, can unset the property. So nobody with day-to-day access can both change the rules and write the data. GxP jobs that write `run_context` get `MODIFY` on that one table only.

### Change control for the capture jobs

The capture jobs are part of your validated landscape. Their SQL, schedules, table list and expected properties live in Git and deploy through a bundle with an approval gate, like the pipelines they observe. A change to the capture code is a change to how evidence is collected, so assess it the same way.

### Drift checks

Check the settings that protect the evidence every day, and write the results to `gxp_evidence.control.drift_checks`:

```python
EXPECTED = {
    "delta.appendOnly": "true",
    "delta.logRetentionDuration": "interval 3650 days",
    "delta.deletedFileRetentionDuration": "interval 3650 days",
}

def property_drift(table: str) -> list[dict]:
    actual = {r.key: r.value.strip().lower() for r in spark.sql(f"SHOW TBLPROPERTIES {table}").collect()}
    return [{"table": table, "property": k, "expected": v, "actual": actual.get(k)}
            for k, v in EXPECTED.items() if actual.get(k) != v]
```

Do the same for the account's system-table retention. Run a `GET` against `/api/2.1/accounts/{account_id}/settings/st_retention` and compare the result to your floor. That call needs an account admin credential, so put it in its own tightly scoped job or add it to periodic review. Also watch the evidence layer's own audit trail. Alert on any `updateTables` or `updatePermissions` event whose target is in `gxp_evidence`.

### Where Delta stops

`appendOnly` and grants only control access through Databricks. For long-term retention, export closed periods: once a quarter, write the finished partitions to Parquet in a staging volume, record row counts and a SHA-256 hash per file in an export manifest, and copy the files into an Azure storage container with a **locked** time-based retention policy. ADLS Gen2 accounts (hierarchical namespace) support container-level WORM; version-level WORM isn't supported on them yet. Record the manifest in the evidence layer, so the export is evidence too.

Don't put WORM on the storage behind live Delta tables. Delta has to delete its own log and checkpoint files, and an immutability policy will stop it.

## Prove it: the quarterly retrieval rehearsal

A layer nobody has queried under pressure isn't proven. Once a quarter, run a scripted rehearsal and file it as validation evidence: a test script with predefined acceptance criteria, executed, with the output attached and signed in your QMS.

Pick a date older than the platform's retention, once your layer is that old. Confirm the system table no longer holds it (`SELECT min(event_date) FROM system.access.audit`). That proves the answer came from your layer. Then answer *who changed what, and which job ran, on 12 June 2025* from `gxp_evidence` alone:

```sql
WITH commits AS (
  SELECT version, timestamp, userName, operation, operationMetrics,
         userMetadata:job_run_id::string AS job_run_id
  FROM gxp_evidence.history.table_commits
  WHERE table_full_name = 'gxp.silver.batch_records_history'
    AND timestamp >= TIMESTAMP'2025-06-12' AND timestamp < TIMESTAMP'2025-06-13'
),
runs AS (
  SELECT workspace_id, job_id, run_id,
         min(period_start_time) AS run_start,
         max(result_state)      AS result_state  -- set on the final slice only
  FROM gxp_evidence.lakeflow.job_run_timeline
  WHERE run_id IN (SELECT job_run_id FROM commits)
  GROUP BY workspace_id, job_id, run_id
),
job_as_of AS (
  SELECT r.run_id, j.name AS job_name, j.run_as, j.change_time AS definition_as_of
  FROM runs r
  JOIN gxp_evidence.lakeflow.jobs j
    ON j.workspace_id = r.workspace_id AND j.job_id = r.job_id
   AND j.change_time <= r.run_start
  QUALIFY row_number() OVER (PARTITION BY r.run_id ORDER BY j.change_time DESC) = 1
)
SELECT c.version, c.timestamp, c.operation, c.userName, c.operationMetrics,
       r.job_id, r.run_id, r.result_state, a.job_name, a.run_as, a.definition_as_of,
       x.code_version, x.repair_count
FROM commits c
LEFT JOIN runs r                               ON r.run_id = c.job_run_id
LEFT JOIN job_as_of a                          ON a.run_id = r.run_id
LEFT JOIN gxp_evidence.history.run_context x   ON x.job_run_id = c.job_run_id
ORDER BY c.version;
```

Then find who touched that job in the month before:

```sql
SELECT event_time, user_identity.email, action_name, request_params
FROM gxp_evidence.audit.platform_audit
WHERE service_name = 'jobs'
  AND action_name IN ('create', 'update', 'reset', 'runNow', 'changeJobAcl')
  AND request_params['job_id'] = '<job_id from the result above>'
  AND event_date BETWEEN DATE'2025-05-12' AND DATE'2025-06-12'
ORDER BY event_time;
```

The acceptance criteria are written before the run. Every commit on that date resolves to a run. Every run resolves to a job definition and a code version. Every change to the job resolves to a named identity. Any `NULL` in a join is a finding. Record it, investigate it and fix the capture. A rehearsal that never finds anything probably isn't testing hard enough.

## Where this stops

This pattern copies evidence and proves the copy can be found again. It doesn't give you:

1. **Independence from the platform.** The live layer still sits on Databricks, under the same account admins. The WORM export is the part that survives a lost workspace, account or contract.
2. **Evidence the platform never recorded.** Event-log delivery is best effort and lineage is emitted only when it can be inferred. A gap in the source is a gap in your copy, and reconciliation can only show you where it is.
3. **Intent.** Identities in these tables are mostly service principals. Reasons for change and electronic signatures still belong in the system of record, as covered in the first post's gap 1.
4. **Stable sources.** `system.access.audit` is still labeled Public Preview on Azure, and the pipeline events system table is Beta. Assess them as you would any supplier dependency, and keep the schema guard.
5. **Review.** Capturing isn't reviewing. Exception reports and periodic review, the first post's gap 6, are still separate work.
6. **A retention number.** How long to keep this is for your QA and regulatory function to define, not for this post or Databricks to decide.

## Checklist

- [ ] Configurable system-table retention set from your requirement, with the setting under change control
- [ ] A dedicated `gxp_evidence` catalog on its own storage, owned by the deployment principal
- [ ] Append-only evidence tables with retention properties set on purpose
- [ ] Insert-only MERGE capture on stable keys, with an overlap window and a capture log
- [ ] A schema guard that fails on new source columns
- [ ] Delta history captured for every GxP table, with run context in `userMetadata`
- [ ] Code version and run ID recorded for every GxP job run
- [ ] Daily drift checks on table properties, grants and account retention
- [ ] Closed periods exported to locked WORM storage, with a hashed manifest
- [ ] A quarterly retrieval rehearsal, filed as validation evidence

The first post was about keeping the data. This one is about keeping the record of how it got there. On the day an inspector asks, the platform's copy may already be gone. Yours shouldn't be.
