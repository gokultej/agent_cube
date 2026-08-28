-- Top 10 Customers by Net Value this week
SELECT
    customer_code,
    customer_name,
    customer_region,
    region_desc,
    COALESCE(SUM(net_value) / 1e7, 0)             AS net_sales_cr,
    COUNT(DISTINCT invoice_number)                  AS invoice_count,
    COALESCE(AVG(basic_rate), 0)                   AS avg_basic_rate
FROM sales_data
WHERE sales_date BETWEEN :week_start AND :week_end
  AND billing_type NOT IN ('RE', 'RK')
  AND plant = :plant
GROUP BY customer_code, customer_name, customer_region, region_desc
ORDER BY net_sales_cr DESC
LIMIT 10;

-- Region-wise sales breakdown
SELECT
    customer_region,
    region_desc,
    COALESCE(SUM(net_value) / 1e7, 0)             AS net_sales_cr,
    COUNT(DISTINCT customer_code)                   AS customer_count,
    COUNT(DISTINCT invoice_number)                  AS invoice_count
FROM sales_data
WHERE sales_date BETWEEN :week_start AND :week_end
  AND billing_type NOT IN ('RE', 'RK')
  AND plant = :plant
GROUP BY customer_region, region_desc
ORDER BY net_sales_cr DESC;
