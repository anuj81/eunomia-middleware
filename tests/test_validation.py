import pytest
from src.validation.sql_parser import validate_sql, SQLValidationError

def test_valid_sql_allowed_views():
    query = "SELECT * FROM finance.daily_revenue_view"
    allowed = ["finance.daily_revenue_view"]
    assert validate_sql(query, allowed) == True

def test_valid_sql_allowed_views_no_schema():
    query = "SELECT * FROM daily_revenue_view"
    allowed = ["finance.daily_revenue_view"]
    assert validate_sql(query, allowed) == True

def test_invalid_sql_base_table_blocked():
    query = "SELECT * FROM core.fct_orders"
    allowed = ["finance.daily_revenue_view"]
    with pytest.raises(SQLValidationError, match="Unauthorized table/view reference"):
        validate_sql(query, allowed)

def test_invalid_sql_multiple_statements_blocked():
    query = "SELECT * FROM finance.daily_revenue_view; DROP TABLE core.dim_customers;"
    allowed = ["finance.daily_revenue_view"]
    with pytest.raises(SQLValidationError, match="Multiple SQL statements"):
        validate_sql(query, allowed)

def test_invalid_sql_not_select():
    query = "DELETE FROM finance.daily_revenue_view"
    allowed = ["finance.daily_revenue_view"]
    with pytest.raises(SQLValidationError, match="Only SELECT statements are allowed"):
        validate_sql(query, allowed)
