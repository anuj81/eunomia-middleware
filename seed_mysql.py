import mysql.connector
from faker import Faker
import random
import sys

fake = Faker()

try:
    # Connect to MySQL
    conn = mysql.connector.connect(
        host="localhost",
        user="api_user",
        password="newvibes"
    )
    cursor = conn.cursor()
except mysql.connector.Error as err:
    print(f"Error connecting to MySQL: {err}")
    sys.exit(1)

# 1. Create Database
db_name = "zenith_corp_eunomia"
cursor.execute(f"DROP DATABASE IF EXISTS {db_name}")
cursor.execute(f"CREATE DATABASE {db_name}")
cursor.execute(f"USE {db_name}")

# 2. Create Tables
cursor.execute("""
CREATE TABLE core_dim_customers (
    customer_id INT AUTO_INCREMENT PRIMARY KEY,
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    email VARCHAR(255) UNIQUE,
    signup_date DATE,
    region VARCHAR(50)
)
""")

cursor.execute("""
CREATE TABLE core_fct_orders (
    order_id INT AUTO_INCREMENT PRIMARY KEY,
    customer_id INT,
    order_date DATE,
    amount DECIMAL(10, 2),
    FOREIGN KEY (customer_id) REFERENCES core_dim_customers(customer_id)
)
""")

cursor.execute("""
CREATE TABLE core_fct_payments (
    payment_id INT AUTO_INCREMENT PRIMARY KEY,
    order_id INT,
    payment_method VARCHAR(50),
    card_last_four VARCHAR(4),
    status VARCHAR(20),
    FOREIGN KEY (order_id) REFERENCES core_fct_orders(order_id)
)
""")

# 3. Create Views
cursor.execute("""
CREATE VIEW finance_daily_revenue_view AS
SELECT order_date, SUM(amount) as total_revenue
FROM core_fct_orders
GROUP BY order_date
""")

cursor.execute("""
CREATE VIEW finance_customer_payment_history_view AS
SELECT 
    c.customer_id, 
    c.first_name, 
    c.last_name, 
    c.email, 
    o.order_id, 
    o.amount, 
    p.payment_method, 
    p.card_last_four, 
    p.status
FROM core_dim_customers c
JOIN core_fct_orders o ON c.customer_id = o.customer_id
JOIN core_fct_payments p ON o.order_id = p.order_id
""")

cursor.execute("""
CREATE VIEW marketing_customer_ltv_view AS
SELECT 
    c.customer_id, 
    c.email, 
    c.region, 
    SUM(o.amount) as lifetime_value
FROM core_dim_customers c
JOIN core_fct_orders o ON c.customer_id = o.customer_id
GROUP BY c.customer_id, c.email, c.region
""")

cursor.execute("""
CREATE VIEW marketing_regional_performance_view AS
SELECT 
    c.region, 
    SUM(o.amount) as regional_revenue, 
    COUNT(DISTINCT c.customer_id) as total_customers
FROM core_dim_customers c
JOIN core_fct_orders o ON c.customer_id = o.customer_id
GROUP BY c.region
""")

# 4. Seed Data
print("Inserting mock data into MySQL...")
regions = ["North America", "Europe", "Asia", "South America"]

customer_ids = []
for _ in range(100):
    first_name = fake.first_name()
    last_name = fake.last_name()
    email = fake.unique.email()
    signup_date = fake.date_between(start_date='-2y', end_date='today')
    region = random.choice(regions)
    
    cursor.execute("""
    INSERT INTO core_dim_customers (first_name, last_name, email, signup_date, region)
    VALUES (%s, %s, %s, %s, %s)
    """, (first_name, last_name, email, signup_date, region))
    customer_ids.append(cursor.lastrowid)

for customer_id in customer_ids:
    for _ in range(random.randint(1, 4)):
        order_date = fake.date_between(start_date='-1y', end_date='today')
        amount = round(random.uniform(15.0, 300.0), 2)
        
        cursor.execute("""
        INSERT INTO core_fct_orders (customer_id, order_date, amount)
        VALUES (%s, %s, %s)
        """, (customer_id, order_date, amount))
        order_id = cursor.lastrowid
        
        payment_method = random.choice(["Credit Card", "PayPal", "Bank Transfer"])
        card_last_four = str(random.randint(1000, 9999)) if payment_method == "Credit Card" else None
        status = random.choice(["Completed", "Completed", "Completed", "Failed"])
        
        cursor.execute("""
        INSERT INTO core_fct_payments (order_id, payment_method, card_last_four, status)
        VALUES (%s, %s, %s, %s)
        """, (order_id, payment_method, card_last_four, status))

conn.commit()
cursor.close()
conn.close()

print("✅ Database 'zenith_corp_eunomia' successfully created, schemas applied, and 100+ rows seeded!")
