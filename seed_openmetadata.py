import requests
import json
import time

import base64

OM_URL = "http://localhost:8585/api/v1"
HEADERS = {"Content-Type": "application/json"}

print("Logging into OpenMetadata...")
# Login
for _ in range(30):
    try:
        pwd_b64 = base64.b64encode(b"admin").decode()
        resp = requests.post(f"{OM_URL}/users/login", json={"email": "admin@open-metadata.org", "password": pwd_b64})
        if resp.status_code == 200:
            token = resp.json()["accessToken"]
            HEADERS["Authorization"] = f"Bearer {token}"
            print("Logged in successfully.")
            break
    except Exception as e:
        pass
    print("Waiting for OpenMetadata server...")
    time.sleep(5)
else:
    print("Failed to connect to OpenMetadata")
    exit(1)

# 1. Create Database Service
service_payload = {
    "name": "zenith_mysql",
    "serviceType": "Mysql"
}
resp = requests.post(f"{OM_URL}/services/databaseServices", headers=HEADERS, json=service_payload)
service_fqn = "zenith_mysql"
print("Created Service:", resp.status_code)

# 2. Create Database
db_payload = {
    "name": "zenith_corp_eunomia",
    "service": "zenith_mysql"
}
resp = requests.post(f"{OM_URL}/databases", headers=HEADERS, json=db_payload)
print("Created Database:", resp.status_code)

# 3. Create Schema
schema_payload = {
    "name": "zenith_corp_eunomia",
    "database": "zenith_mysql.zenith_corp_eunomia"
}
resp = requests.post(f"{OM_URL}/databaseSchemas", headers=HEADERS, json=schema_payload)
print("Created Schema:", resp.status_code)

# Views Definitions
views = [
    {
        "name": "finance_daily_revenue_view",
        "description": "Get daily revenue",
        "columns": [
            {"name": "order_date", "dataType": "DATE"},
            {"name": "total_revenue", "dataType": "DECIMAL"}
        ],
        "tags": [{"tagFQN": "Tier.Tier1"}]
    },
    {
        "name": "finance_customer_payment_history_view",
        "description": "Get payment history",
        "columns": [
            {"name": "customer_id", "dataType": "INT"},
            {"name": "first_name", "dataType": "VARCHAR", "dataLength": 255, "tags": [{"tagFQN": "PII.Sensitive"}]},
            {"name": "last_name", "dataType": "VARCHAR", "dataLength": 255, "tags": [{"tagFQN": "PII.Sensitive"}]},
            {"name": "email", "dataType": "VARCHAR", "dataLength": 255, "tags": [{"tagFQN": "PII.Sensitive"}]},
            {"name": "order_id", "dataType": "INT"},
            {"name": "amount", "dataType": "DECIMAL"},
            {"name": "payment_method", "dataType": "VARCHAR", "dataLength": 255},
            {"name": "card_last_four", "dataType": "VARCHAR", "dataLength": 255, "tags": [{"tagFQN": "PII.Sensitive"}]},
            {"name": "status", "dataType": "VARCHAR", "dataLength": 255}
        ]
    },
    {
        "name": "marketing_customer_ltv_view",
        "description": "Customer LTV",
        "columns": [
            {"name": "customer_id", "dataType": "INT"},
            {"name": "email", "dataType": "VARCHAR", "dataLength": 255, "tags": [{"tagFQN": "PII.Sensitive"}]},
            {"name": "region", "dataType": "VARCHAR", "dataLength": 255},
            {"name": "lifetime_value", "dataType": "DECIMAL"}
        ]
    },
    {
        "name": "marketing_regional_performance_view",
        "description": "Regional performance",
        "columns": [
            {"name": "region", "dataType": "VARCHAR", "dataLength": 255},
            {"name": "regional_revenue", "dataType": "DECIMAL"},
            {"name": "total_customers", "dataType": "INT"}
        ]
    }
]

for view in views:
    table_payload = {
        "name": view["name"],
        "description": view["description"],
        "columns": view["columns"],
        "databaseSchema": "zenith_mysql.zenith_corp_eunomia.zenith_corp_eunomia"
    }
    resp = requests.put(f"{OM_URL}/tables", headers=HEADERS, json=table_payload)
    print(f"Created/Updated Table {view['name']}:", resp.status_code)

print("OpenMetadata Seeding Complete!")
