"""
Give the app's managed identity access to the Azure SQL database.

Runs as you (Azure CLI login, which must be the server's Entra admin) and
creates a contained database user for the identity with the least rights the
app needs: read, write, and create its own tables on first start.

    python infra/grant_sql_access.py <server>.database.windows.net <database> <identity-name>
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import _connect_mssql  # noqa: E402


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    server, database, identity = sys.argv[1:]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
        sys.exit("Unexpected identity name.")

    conn = _connect_mssql(
        f"Server=tcp:{server},1433;Database={database};Encrypt=yes;"
        "TrustServerCertificate=no;Authentication=ActiveDirectoryDefault"
    )
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sys.database_principals WHERE name = ?", (identity,))
    if not cur.fetchone():
        cur.execute(f"CREATE USER [{identity}] FROM EXTERNAL PROVIDER")
    for role in ("db_datareader", "db_datawriter", "db_ddladmin"):
        cur.execute(
            "SELECT 1 FROM sys.database_role_members m "
            "JOIN sys.database_principals r ON r.principal_id = m.role_principal_id "
            "JOIN sys.database_principals u ON u.principal_id = m.member_principal_id "
            "WHERE r.name = ? AND u.name = ?",
            (role, identity),
        )
        if not cur.fetchone():
            cur.execute(f"ALTER ROLE {role} ADD MEMBER [{identity}]")
    conn.commit()
    conn.close()
    print(f"'{identity}' can now use {database}.")


if __name__ == "__main__":
    main()
