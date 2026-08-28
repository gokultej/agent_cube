from modules.db_connector import DBConnector

def fetch_customer_ageing_summary(dates):
    sql = """
        SELECT
            COUNT(DISTINCT customer_name)                     AS total_customers,
            COALESCE(SUM(total_outstanding), 0)              AS total_outstanding,
            COALESCE(SUM(not_due_amount), 0)                 AS not_due_amount,
            COALESCE(SUM(tot01_30days_outstanding), 0)       AS bucket_01_30,
            COALESCE(SUM(tot31_60days_outstanding), 0)       AS bucket_31_60,
            COALESCE(SUM(tot61_90days_outstanding), 0)       AS bucket_61_90,
            COALESCE(SUM(tot91_120days_outstanding), 0)      AS bucket_91_120,
            COALESCE(SUM(tot121_180days_outstanding), 0)     AS bucket_121_180,
            COALESCE(SUM(tot181_360days_outstanding), 0)     AS bucket_181_360,
            COALESCE(SUM(tot361_999days_outstanding), 0) +
            COALESCE(SUM(tot1000_9999days_outstanding), 0)   AS bucket_361_plus
        FROM customer_ageing_prod
        WHERE currency = 'INR'
          AND total_outstanding > 0
    """
    result = DBConnector.execute_query(sql)
    return result[0] if result else {}


def fetch_vendor_ageing_summary(dates):
    sql = """
        SELECT
            COUNT(DISTINCT vendor_name)                    AS total_vendors,
            COALESCE(SUM(gross_outstanding), 0)           AS total_outstanding,
            COALESCE(SUM(tot01_30days_outstanding), 0)    AS bucket_01_30,
            COALESCE(SUM(tot31_60days_outstanding), 0)    AS bucket_31_60,
            COALESCE(SUM(tot61_90days_outstanding), 0)    AS bucket_61_90,
            COALESCE(SUM(tot91_120days_outstanding), 0)   AS bucket_91_120,
            COALESCE(SUM(tot121_150days_outstanding), 0)  AS bucket_121_150,
            COALESCE(SUM(tot151_180days_outstanding), 0)  AS bucket_151_180,
            COALESCE(SUM(tot181_360days_outstanding), 0)  AS bucket_181_360,
            COALESCE(SUM(tot361_999days_outstanding), 0)  AS bucket_361_plus
        FROM vendor_ageing_prod
        WHERE currency = 'INR'
          AND gross_outstanding > 0
    """
    result = DBConnector.execute_query(sql)
    return result[0] if result else {}

def fetch_top_vendors_overdue(limit=10):
    sql = """
        SELECT
            vendor_name,
            supplier,
            COALESCE(SUM(gross_outstanding), 0)           AS gross_outstanding,
            MAX(no_of_days_outstanding)                    AS max_days_overdue,
            COALESCE(SUM(tot61_90days_outstanding)
                + SUM(tot91_120days_outstanding)
                + SUM(tot121_150days_outstanding)
                + SUM(tot151_180days_outstanding)
                + SUM(tot181_360days_outstanding)
                + SUM(tot361_999days_outstanding), 0)     AS overdue_60plus
        FROM vendor_ageing_prod
        WHERE currency = 'INR'
          AND gross_outstanding > 0
        GROUP BY vendor_name, supplier
        ORDER BY gross_outstanding DESC
        LIMIT %(limit)s
    """
    return DBConnector.execute_query(sql, {"limit": limit})

def fetch_payments_due_next_week(dates):
    sql = """
        SELECT
            vendor_name,
            supplier,
            due_date,
            COALESCE(SUM(document_amount), 0)            AS amount_due,
            payt_terms,
            currency
        FROM vendor_ageing_prod
        WHERE due_date BETWEEN %(next_week_start)s AND %(next_week_end)s
          AND gross_outstanding > 0
          AND currency = 'INR'
        GROUP BY vendor_name, supplier, due_date, payt_terms, currency
        ORDER BY due_date, amount_due DESC
        LIMIT 20
    """
    return DBConnector.execute_query(sql, dates)

def fetch_critical_overdue_vendors(min_days=90):
    sql = """
        SELECT
            vendor_name,
            due_date,
            no_of_days_outstanding,
            gross_outstanding,
            payt_terms
        FROM vendor_ageing_prod
        WHERE no_of_days_outstanding > %(min_days)s
          AND gross_outstanding > 0
          AND currency = 'INR'
        ORDER BY no_of_days_outstanding DESC, gross_outstanding DESC
        LIMIT 10
    """
    return DBConnector.execute_query(sql, {"min_days": min_days})
