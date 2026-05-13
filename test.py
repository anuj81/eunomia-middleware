import requests
import base64
OM_URL = "http://localhost:8585/api/v1"
pwd_b64 = base64.b64encode(b"admin").decode()
resp = requests.post(f"{OM_URL}/users/login", json={"email": "admin@open-metadata.org", "password": pwd_b64})
HEADERS = {"Authorization": f"Bearer {resp.json()['accessToken']}", "Content-Type": "application/json"}
table_payload = {
    "name": "finance_customer_payment_history_view",
    "description": "Get payment history",
    "columns": [
        {"name": "customer_id", "dataType": "INT"},
        {"name": "first_name", "dataType": "VARCHAR", "tags": [{"tagFQN": "PII.Sensitive"}]}
    ],
    "databaseSchema": "zenith_mysql.zenith_corp_eunomia.zenith_corp_eunomia"
}
resp2 = requests.put(f"{OM_URL}/tables", headers=HEADERS, json=table_payload)
print(resp2.text)
