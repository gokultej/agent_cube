-- Monthly Planned vs Achieved (from sales_data planned fields)
-- NOTE: Adjust column names to match your planned production field
SELECT
    TO_CHAR(sales_date, 'Mon YYYY')               AS month_label,
    financial_year,
    COALESCE(SUM(ordered_qty), 0)                  AS total_ordered_qty,
    COUNT(DISTINCT sales_order_number)              AS orders_count
FROM sales_data
WHERE sales_date BETWEEN :fy_start AND :today
  AND plant = :plant
GROUP BY TO_CHAR(sales_date, 'Mon YYYY'), financial_year,
         DATE_TRUNC('month', sales_date)
ORDER BY DATE_TRUNC('month', sales_date);

-- Weekly Ordered vs Invoiced Qty
SELECT
    DATE_TRUNC('week', sales_date)                 AS week_start,
    'Week ' || TO_CHAR(
        DATE_TRUNC('week', sales_date), 'W')       AS week_label,
    COALESCE(SUM(ordered_qty), 0)                  AS ordered_qty,
    COALESCE(SUM(CASE WHEN invoice_number IS NOT NULL
        THEN ordered_qty ELSE 0 END), 0)           AS invoiced_qty
FROM sales_data
WHERE sales_date BETWEEN :month_start AND :month_end
  AND plant = :plant
GROUP BY DATE_TRUNC('week', sales_date)
ORDER BY week_start;

-- Transformer-specific production KPIs
SELECT
    capacity_kva,
    rating,
    material_description,
    COALESCE(SUM(ordered_qty), 0)                  AS qty_ordered,
    COALESCE(SUM(net_value) / 1e7, 0)             AS value_cr
FROM sales_data
WHERE sales_date BETWEEN :month_start AND :today
  AND is_transformer = 'Y'
  AND plant = :plant
GROUP BY capacity_kva, rating, material_description
ORDER BY value_cr DESC
LIMIT 10;
