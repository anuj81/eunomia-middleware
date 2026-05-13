import sqlglot
from sqlglot import exp

class SQLValidationError(Exception):
    pass

def validate_sql(query: str, allowed_views: list[str]) -> bool:
    """
    Validates that the SQL query is a SELECT statement and only references allowed views.
    """
    try:
        # Parse the query using the MySQL dialect
        # sqlglot.parse returns a list of expressions. For single statements, len should be 1.
        parsed_statements = sqlglot.parse(query, read="mysql")
    except sqlglot.errors.ParseError as e:
        raise SQLValidationError(f"SQL Syntax Error: {str(e)}")

    if not parsed_statements:
         raise SQLValidationError("Empty SQL query.")

    if len(parsed_statements) > 1:
        raise SQLValidationError("Multiple SQL statements are not allowed. Only a single SELECT is permitted.")

    parsed = parsed_statements[0]

    # Ensure it's a SELECT statement
    if not isinstance(parsed, exp.Select):
        raise SQLValidationError("Only SELECT statements are allowed.")

    # Extract all table references
    tables = [table.name.lower() for table in parsed.find_all(exp.Table)]
    
    for table in tables:
        # Check against the allowed views. We compare just the table name or schema.table
        allowed_names = [v.split('.')[-1].lower() for v in allowed_views]
        full_allowed_names = [v.lower() for v in allowed_views]
        
        if table not in allowed_names and table not in full_allowed_names:
            raise SQLValidationError(f"Unauthorized table/view reference: {table}. Allowed views: {', '.join(full_allowed_names)}")

    return True
