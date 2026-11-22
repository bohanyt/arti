"""Optional native row collections: absent/None/empty release no rows."""

def native_rows(value):
    return value if isinstance(value,(list,tuple)) else ()
