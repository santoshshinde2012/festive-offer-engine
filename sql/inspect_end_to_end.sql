-- Choose a writable catalog used by notebooks/end_to_end_pipeline.py.
-- Named SQL parameters: :catalog, :publication_id, :ingestion_id.
-- Queries admit only batches with their final publication/import completion marker.

-- 1. Source contracts, approved contracts, and quarantined contracts reconcile.
SELECT revision, source_count, approved_count, quarantine_count,
       source_count = approved_count + quarantine_count AS contract_counts_reconcile,
       content_sha256, bundle_volume_path, published_at_utc
FROM IDENTIFIER(:catalog || '.festive_offer_e2e.publication_manifest')
WHERE publication_id = :publication_id;

SELECT q.offer_id, q.reason, q.source_record_json
FROM IDENTIFIER(:catalog || '.festive_offer_e2e.quarantined_contracts') q
JOIN IDENTIFIER(:catalog || '.festive_offer_e2e.publication_manifest') m
  ON q.publication_id = m.publication_id
WHERE q.publication_id = :publication_id
ORDER BY q.source_index;

-- 2. Expected value is a ranking objective, while nominal value is potential liability.
SELECT d.request_id, d.rank, d.offer_id, d.instrument_id, d.payout,
       d.nominal_benefit_paise / 100.0 AS nominal_rupees,
       d.realization_probability,
       d.expected_benefit_paise / 100.0 AS expected_rupees
FROM IDENTIFIER(:catalog || '.festive_offer_e2e.publication_decisions') d
JOIN IDENTIFIER(:catalog || '.festive_offer_e2e.publication_manifest') m
  ON d.publication_id = m.publication_id
WHERE d.publication_id = :publication_id AND d.eligible
ORDER BY d.request_id, d.rank;

-- The application export must identify the same published business revision and hash.
WITH imported_snapshots AS (
  SELECT get_json_object(r.payload_json, '$.revision') AS revision,
         get_json_object(get_json_object(r.payload_json, '$.bundle_json'), '$.manifest.content_sha256') AS content_sha256
  FROM IDENTIFIER(:catalog || '.festive_offer_e2e.operational_records') r
  JOIN IDENTIFIER(:catalog || '.festive_offer_e2e.operational_imports') i
    ON r.ingestion_id = i.ingestion_id
  WHERE r.ingestion_id = :ingestion_id AND r.source_table = 'snapshots'
)
SELECT m.revision, m.content_sha256 AS published_hash, s.content_sha256 AS application_hash,
       COALESCE(m.content_sha256 = s.content_sha256, FALSE) AS exact_published_bundle_used
FROM IDENTIFIER(:catalog || '.festive_offer_e2e.publication_manifest') m
LEFT JOIN imported_snapshots s ON s.revision = m.revision
WHERE m.publication_id = :publication_id;

-- 3. Event counts describe the local reference execution, not customer uplift.
WITH completed AS (
  SELECT r.*
  FROM IDENTIFIER(:catalog || '.festive_offer_e2e.operational_records') r
  JOIN IDENTIFIER(:catalog || '.festive_offer_e2e.operational_imports') i
    ON r.ingestion_id = i.ingestion_id
  WHERE r.ingestion_id = :ingestion_id AND r.source_table = 'events'
)
SELECT get_json_object(payload_json, '$.kind') AS event_kind, COUNT(*) AS event_count
FROM completed
GROUP BY get_json_object(payload_json, '$.kind')
ORDER BY event_count DESC, event_kind;

-- 4. A qualified purchase and a fulfilled cashback obligation are separate states.
WITH completed AS (
  SELECT r.*
  FROM IDENTIFIER(:catalog || '.festive_offer_e2e.operational_records') r
  JOIN IDENTIFIER(:catalog || '.festive_offer_e2e.operational_imports') i
    ON r.ingestion_id = i.ingestion_id
  WHERE r.ingestion_id = :ingestion_id AND r.source_table = 'applications'
)
SELECT get_json_object(payload_json, '$.application_id') AS application_id,
       get_json_object(payload_json, '$.payment_id') AS payment_id,
       get_json_object(payload_json, '$.offer_id') AS offer_id,
       get_json_object(payload_json, '$.payment_state') AS payment_state,
       get_json_object(payload_json, '$.benefit_state') AS benefit_state,
       get_json_object(payload_json, '$.budget_state') AS budget_state,
       TRY_CAST(get_json_object(payload_json, '$.nominal_paise') AS BIGINT) / 100.0 AS nominal_rupees
FROM completed
ORDER BY application_id;

-- 5. Budget conservation in the imported snapshot. NULL/malformed amounts fail the check.
WITH amounts AS (
  SELECT get_json_object(r.payload_json, '$.offer_id') AS offer_id,
         TRY_CAST(get_json_object(r.payload_json, '$.funding_paise') AS BIGINT) AS funding,
         TRY_CAST(get_json_object(r.payload_json, '$.available_paise') AS BIGINT) AS available,
         TRY_CAST(get_json_object(r.payload_json, '$.held_paise') AS BIGINT) AS held,
         TRY_CAST(get_json_object(r.payload_json, '$.committed_paise') AS BIGINT) AS committed
  FROM IDENTIFIER(:catalog || '.festive_offer_e2e.operational_records') r
  JOIN IDENTIFIER(:catalog || '.festive_offer_e2e.operational_imports') i
    ON r.ingestion_id = i.ingestion_id
  WHERE r.ingestion_id = :ingestion_id AND r.source_table = 'budgets'
)
SELECT *, COALESCE(funding = available + held + committed
                  AND available >= 0 AND held >= 0 AND committed >= 0, FALSE) AS conservation_passes
FROM amounts
ORDER BY offer_id;

-- 6. Outstanding versus fulfilled benefit obligations. This is a point-in-time export.
WITH obligations AS (
  SELECT get_json_object(r.payload_json, '$.payout') AS payout,
         get_json_object(r.payload_json, '$.state') AS obligation_state,
         TRY_CAST(get_json_object(r.payload_json, '$.amount_paise') AS BIGINT) AS amount_paise
  FROM IDENTIFIER(:catalog || '.festive_offer_e2e.operational_records') r
  JOIN IDENTIFIER(:catalog || '.festive_offer_e2e.operational_imports') i
    ON r.ingestion_id = i.ingestion_id
  WHERE r.ingestion_id = :ingestion_id AND r.source_table = 'benefit_obligations'
)
SELECT payout, obligation_state, COUNT(*) AS obligation_count,
       SUM(amount_paise) / 100.0 AS obligation_rupees
FROM obligations
GROUP BY payout, obligation_state
ORDER BY payout, obligation_state;
