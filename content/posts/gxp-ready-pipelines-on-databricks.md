---
title: "GxP-ready pipelines on Databricks: an ALCOA+ map"
date: 2026-10-15
draft: false
tags: [databricks, gxp, data-integrity, delta-lake, unity-catalog]
description: "Delta history is not an audit trail. How ALCOA+ maps onto Databricks, what to configure, and where the platform stops."
---

An auditor picks a lot your site released fourteen months ago and asks a simple question: *show me the batch record data exactly as it was when QA released this lot, and every change made to it since.*

Someone on the team says that's fine, Delta keeps history, we'll use time travel. Then they run `DESCRIBE HISTORY`, and the table only goes back 30 days, because that's the default `delta.logRetentionDuration`. The data files behind older versions are gone too. `VACUUM` keeps seven days of files by default, and on Unity Catalog managed tables, predictive optimization runs it for you. Nobody chose to delete the evidence. The platform cleaned up on schedule, exactly as designed.

That scenario is a composite, but the pattern isn't rare: missing audit trails and unexplained data changes are recurring themes in FDA data-integrity findings. As regulated companies move more of their data onto lakehouses, auditors will follow it there.

The lesson is the thesis of this post: **Delta history is a convenience feature, not a Part 11 audit trail.** Databricks is GxP-capable. Being GxP-ready is a design decision you own.

## Start with the system of record

Before any configuration, decide what the lakehouse is. In most GxP landscapes, the MES, LIMS or QMS is the system of record, with its own validated audit trail and electronic signatures. The pipeline's job is to *preserve* that trail faithfully, carrying the source user, timestamp and reason for change through as columns, not to rebuild it from Delta logs. Almost every choice below follows from that.

## ALCOA+ mapped to the lakehouse

| Principle | What an auditor asks | What I build on Databricks |
|---|---|---|
| Attributable | Who did it? | Source user carried as a column; Unity Catalog identities and `system.access.audit` for platform actions |
| Legible | Can it be read in ten years? | Open Delta and Parquet formats, column comments, schemas kept in Git |
| Contemporaneous | Was it recorded when it happened? | Source and ingest timestamps on every row |
| Original | Is the first capture preserved? | Append-only bronze, exactly as received |
| Accurate | Is it correct? | Expectations that fail the update, `CHECK` and `NOT NULL` constraints |
| Complete | Is anything missing or doubled? | Deduplication on event ID, reconciliation between systems |
| Consistent | Same answer every time? | ACID writes, SCD Type 2 history instead of overwrites |
| Enduring | Will it last the retention period? | Retention set on purpose, archives, an evidence layer you own |
| Available | Can you produce it on request? | Queryable history, rehearsed retrieval, lineage IDs on every row |

## 1. Retain on purpose

Land every source message in bronze exactly as received, with the message ID, ingest timestamp and source metadata. Then make the table enforce that:

```sql
ALTER TABLE gxp.bronze.mes_events SET TBLPROPERTIES (
  'delta.appendOnly' = 'true',
  -- Set both from your record-retention requirement, not from this example
  'delta.logRetentionDuration' = 'interval 3650 days',
  'delta.deletedFileRetentionDuration' = 'interval 3650 days'
);
```

`delta.appendOnly` blocks `UPDATE` and `DELETE`. `VACUUM`, including the runs predictive optimization triggers, honors the retention properties. Keeping GxP tables in their own catalog lets you treat maintenance there differently, including switching predictive optimization off with `ALTER CATALOG gxp DISABLE PREDICTIVE OPTIMIZATION`.

{{< callout type="gxp" title="The trap" >}}
Time travel needs both the transaction log and the data files. Retaining one without the other still loses the history.
{{< /callout >}}

Change data feed is useful here, but treat it as a pipe, not an archive: its files are cleaned up by the same `VACUUM`. Read the feed and persist it into an audit table you control.

## 2. Build history you can query

Silver tables that receive corrections should keep every version instead of overwriting in place. Lakeflow's `AUTO CDC` (formerly `APPLY CHANGES`) handles SCD Type 2 cleanly:

```sql
CREATE OR REFRESH STREAMING TABLE batch_records_history;

CREATE FLOW batch_records_scd2 AS AUTO CDC INTO batch_records_history
FROM STREAM(gxp.bronze.batch_record_changes)
KEYS (record_id)
SEQUENCE BY source_changed_at
STORED AS SCD TYPE 2;
```

Now "show me this record as of release" is a filter on validity dates, with no dependency on time travel:

```sql
SELECT *
FROM gxp.silver.batch_records_history
WHERE lot_id = 'L-2025-0412'
  AND __START_AT <= TIMESTAMP '2025-08-14 16:05:00'
  AND (__END_AT IS NULL OR __END_AT > TIMESTAMP '2025-08-14 16:05:00');
```

Don't set `appendOnly` on these tables. The history has to end-date rows.

## 3. Make retries boring

Message brokers deliver at least once. Picture a broker reconnect that redelivers a batch of material-consumption messages at the same moment a job retries after a timeout, and the ERP records the consumption twice. Reconciliation catches it before the lot is dispositioned, but only if reconciliation exists. In GxP, idempotency isn't tidiness. It decides whether a system of record can be trusted.

Deduplicate at the door on the source event ID, and make replayed micro-batches harmless with `txnAppId` and `txnVersion`:

```python
events = (
    spark.readStream.table("gxp.bronze.mes_events")
    .withWatermark("ingested_at", "2 hours")
    .dropDuplicatesWithinWatermark(["event_id"])
)

def write_batch(df, batch_id):
    (df.write.format("delta")
        .option("txnAppId", "mes-consumption-silver")
        .option("txnVersion", batch_id)  # a replayed batch is skipped, not rewritten
        .mode("append")
        .saveAsTable("gxp.silver.consumption"))

(events.writeStream
    .foreachBatch(write_batch)
    .option("checkpointLocation", "/Volumes/gxp/silver/checkpoints/consumption")
    .start())
```

The call to the ERP is a side effect outside Delta, so it's at-least-once whatever you do. Keep a posting ledger: every outbound posting gets an idempotency key and a status, and the job checks the ledger before calling and records the outcome after. Where the target allows it, write against a natural key so a repeat updates instead of duplicating. Then reconcile counts and quantities per lot on a schedule.

## 4. Stop schema drift at bronze

An upstream release renames a field or changes a unit of measure. `mergeSchema` absorbs it, the pipeline stays green, and downstream values are wrong. The auditor's question, "was this change assessed before it reached production data?", now has an uncomfortable answer.

Keep automatic schema evolution out of silver and gold, and let Auto Loader rescue unexpected fields instead of evolving:

```python
raw = (
    spark.readStream.format("cloudFiles")
    .option("cloudFiles.format", "json")
    .option("cloudFiles.schemaLocation", "/Volumes/gxp/bronze/schemas/mes")
    .option("cloudFiles.schemaEvolutionMode", "rescue")  # or "failOnNewColumns"
    .load("/Volumes/gxp/landing/mes/")
)
```

Alert whenever `_rescued_data` is non-null. For critical fields, make expectations stop the pipeline rather than warn:

```sql
CREATE OR REFRESH STREAMING TABLE consumption (
  CONSTRAINT known_uom EXPECT (uom IN ('kg', 'g', 'L', 'mL')) ON VIOLATION FAIL UPDATE,
  CONSTRAINT positive_qty EXPECT (quantity > 0) ON VIOLATION FAIL UPDATE
) AS SELECT * FROM STREAM(gxp.bronze.mes_events_parsed);
```

Keep the expected schema in Git, so a change becomes a pull request with a validation run attached.

## 5. Lock it down, then prove it

- Have a group own GxP tables and grant `MODIFY` only to the pipeline's service principal, so nobody can change retention or run `VACUUM` from a notebook.
- Keep DDL, table properties included, in Git, and deploy with Declarative Automation Bundles through CI with an approval gate. No interactive edits in production.
- Check `SHOW TBLPROPERTIES` against expected values on a schedule, and watch `system.access.audit` for `ALTER TABLE` on GxP schemas.
- Once a quarter, rehearse the auditor's request: pick an old lot and reproduce its record as of release. That rehearsal is your validation evidence.

## Where the platform stops

Databricks gives you strong data-integrity mechanics, but it's a data platform, not a GxP record system. Six gaps are yours to close:

1. **Intent.** Delta records which identity ran an operation, usually a service principal, not a person's reason for a change or a Part 11 signature. Keep corrections and signatures in the validated source systems, or behind an application that writes user, reason and signature to an append-only audit table. Never a raw `UPDATE` from a notebook.
2. **A platform that keeps moving.** Runtimes and serverless compute change underneath you, and nobody can sell you a "validated" platform. Take a risk-based Computer Software Assurance approach: pin GxP jobs to an LTS runtime on classic compute, cover anything else with scheduled regression tests, and treat Databricks as an assessed supplier whose release notes someone reviews.
3. **Immutability below Delta.** `appendOnly` only constrains access through Databricks. Make sure only the Unity Catalog managed identity can reach GxP storage, add resource locks and storage logging, and export archives to immutable (WORM) storage. Don't put WORM on live Delta tables; Delta needs to clean up its own log.
4. **Evidence that expires.** System tables, job run history, pipeline event logs and expectation results each have their own retention, none tied to yours. If an auditor might ask for it in three years, copy it into a table you own.
5. **Lineage at the boundary.** Unity Catalog lineage doesn't see the broker or the ERP, the two ends an auditor cares about most. Carry the source message ID and pipeline run ID on every row, so tracing a transaction is a join.
6. **Periodic review and recovery.** Generate quarterly access and audit-trail exception reports and sign them in your QMS. Schedule `DEEP CLONE` copies and test restores; time travel and `UNDROP` cover short windows, not recovery.

## Checklist

- [ ] The system of record is named, and its audit trail is carried through as data
- [ ] Bronze is append-only, with retention set from your requirement
- [ ] Every corrected table keeps SCD Type 2 history
- [ ] Deduplication on event ID, a posting ledger and per-lot reconciliation
- [ ] No schema auto-evolution past bronze; critical expectations fail the update
- [ ] Retention, grants and DDL under change control, with drift alerts
- [ ] An evidence layer you own, and a quarterly retrieval rehearsal

The goal isn't "we configured it correctly." It's "we can prove how it was configured on any date you ask about." That's the difference between a platform that's GxP-capable and a pipeline that's GxP-ready.
