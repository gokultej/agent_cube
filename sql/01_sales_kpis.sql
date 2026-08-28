-- Weekly Net Sales & Invoice Count
SELECT
    COALESCE(SUM(net_value) / 1e7, 0)            AS total_net_sales_cr,
    COALESCE(SUM(gross_value_cr), 0)              AS total_gross_value_cr,
    COUNT(DISTINCT invoice_number)                 AS total_invoices,
    COUNT(DISTINCT sales_order_number)             AS total_orders,
    COUNT(DISTINCT customer_code)                  AS unique_customers,
    COALESCE(AVG(yoy_growth_percent), 0)           AS avg_yoy_growth_pct
FROM sales_data
WHERE sales_date BETWEEN :week_start AND :week_end
  AND billing_type NOT IN ('RE', 'RK')
  AND plant = :plant;

-- Daily Breakdown for Bar Chart
SELECT
    sales_date,
    COALESCE(SUM(net_value) / 1e7, 0)             AS daily_net_sales_cr,
    COUNT(DISTINCT invoice_number)                  AS daily_invoices
FROM sales_data
WHERE sales_date BETWEEN :week_start AND :week_end
  AND billing_type NOT IN ('RE', 'RK')
  AND plant = :plant
GROUP BY sales_date
ORDER BY sales_date;

-- Month-to-Date Sales
SELECT
    COALESCE(SUM(net_value) / 1e7, 0)             AS mtd_net_sales_cr,
    COUNT(DISTINCT invoice_number)                  AS mtd_invoices,
    COALESCE(SUM(cfy_net_sum) / 1e7, 0)           AS cfy_total_cr,
    COALESCE(SUM(pfy_net_sum) / 1e7, 0)           AS pfy_total_cr
FROM sales_data
WHERE sales_date BETWEEN :month_start AND :today
  AND billing_type NOT IN ('RE', 'RK')
  AND plant = :plant;
