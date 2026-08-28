-- Weekly Vendor Payment Summary
SELECT
    COUNT(DISTINCT vendor_name)                     AS total_vendors,
    COALESCE(SUM(gross_outstanding), 0)            AS total_outstanding,
    COALESCE(SUM(document_amount), 0)              AS total_document_amount,
    COALESCE(SUM(not_due_amount), 0)               AS not_due_amount,
    COALESCE(SUM(tot01_30days_outstanding), 0)     AS bucket_01_30,
    COALESCE(SUM(tot31_60days_outstanding), 0)     AS bucket_31_60,
    COALESCE(SUM(tot61_90days_outstanding), 0)     AS bucket_61_90,
    COALESCE(SUM(tot91_120days_outstanding), 0)    AS bucket_91_120,
    COALESCE(SUM(tot121_150days_outstanding), 0)   AS bucket_121_150,
    COALESCE(SUM(tot151_180days_outstanding), 0)   AS bucket_151_180,
    COALESCE(SUM(tot181_360days_outstanding), 0)   AS bucket_181_360,
    COALESCE(SUM(tot361_999days_outstanding), 0)   AS bucket_361_999
FROM vendor_ageing_prod
WHERE posting_date BETWEEN :week_start AND :week_end
  AND currency = 'INR';

-- Vendor Ageing Bucket Detail (for ageing chart)
SELECT
    vendor_name,
    supplier,
    COALESCE(SUM(gross_outstanding), 0)            AS gross_outstanding,
    COALESCE(SUM(tot01_30days_outstanding), 0)     AS bucket_01_30,
    COALESCE(SUM(tot31_60days_outstanding), 0)     AS bucket_31_60,
    COALESCE(SUM(tot61_90days_outstanding), 0)     AS bucket_61_90,
    COALESCE(SUM(tot91_120days_outstanding), 0)    AS bucket_91_120,
    COALESCE(SUM(tot181_360days_outstanding), 0)   AS bucket_181_360,
    COALESCE(SUM(tot361_999days_outstanding), 0)   AS bucket_361_plus,
    MAX(no_of_days_outstanding)                     AS max_days_outstanding,
    MAX(due_date)                                   AS latest_due_date
FROM vendor_ageing_prod
WHERE currency = 'INR'
  AND gross_outstanding > 0
GROUP BY vendor_name, supplier
ORDER BY gross_outstanding DESC
LIMIT 15;

-- Critical overdue vendors (>90 days)
SELECT
    vendor_name,
    supplier,
    due_date,
    no_of_days_outstanding,
    gross_outstanding,
    payt_terms,
    currency
FROM vendor_ageing_prod
WHERE no_of_days_outstanding > 90
  AND gross_outstanding > 0
  AND currency = 'INR'
ORDER BY no_of_days_outstanding DESC, gross_outstanding DESC
LIMIT 10;

-- Payment due next week
SELECT
    vendor_name,
    supplier,
    due_date,
    COALESCE(SUM(document_amount), 0)             AS amount_due,
    payt_terms
FROM vendor_ageing_prod
WHERE due_date BETWEEN :next_week_start AND :next_week_end
  AND gross_outstanding > 0
  AND currency = 'INR'
GROUP BY vendor_name, supplier, due_date, payt_terms
ORDER BY due_date, amount_due DESC;
