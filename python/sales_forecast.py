import pandas as pd
from prophet import Prophet
from sqlalchemy import create_engine
import matplotlib.pyplot as plt

# --- Supabase (PostgreSQL) credentials ---
# Standalone/offline script - not called by the PHP app itself (unlike
# forecast_prophet.py and segment_kmeans.py, which receive their data via
# stdin JSON from PHP). Run this manually, e.g. `python sales_forecast.py`.
# Fill these in from Supabase Dashboard -> Project Settings -> Database.
db_user = "postgres"
db_pass = ""  # your Supabase DB password
db_host = "db.xxxxxxxxxxxxxxxxxxxx.supabase.co"
db_port = "5432"
db_name = "postgres"

# --- Create SQLAlchemy engine (requires: pip install psycopg2-binary) ---
engine = create_engine(f'postgresql+psycopg2://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}')

# --- 1️⃣ Forecast total sales using Prophet ---
query_sales = "SELECT date_created AS ds, total_amount AS y FROM sales"
df_sales = pd.read_sql(query_sales, engine)

# Make sure data is sorted by date
df_sales['ds'] = pd.to_datetime(df_sales['ds'])
df_sales = df_sales.sort_values('ds')

# Initialize and train Prophet model
model = Prophet()
model.fit(df_sales)

# Create future dataframe and forecast
future = model.make_future_dataframe(periods=30)  # next 30 days
forecast = model.predict(future)

# Print forecast
print("=== Sales Forecast ===")
print(forecast[['ds', 'yhat', 'yhat_lower', 'yhat_upper']].tail())

# Plot forecast
fig1 = model.plot(forecast)
plt.title("Sales Forecast (Next 30 Days)")
plt.show()

# --- 2️⃣ Monthly Sales Trend ---
query_monthly = """
SELECT TO_CHAR(date_created, 'YYYY-MM') AS month, SUM(total_amount) AS monthly_sales
FROM sales
GROUP BY month
ORDER BY month
"""
df_monthly = pd.read_sql(query_monthly, engine)
df_monthly['month'] = pd.to_datetime(df_monthly['month'])

# Plot monthly sales trend
plt.figure(figsize=(10,5))
plt.plot(df_monthly['month'], df_monthly['monthly_sales'], marker='o')
plt.title("Monthly Sales Trend")
plt.xlabel("Month")
plt.ylabel("Total Sales")
plt.grid(True)
plt.show()

# --- 3️⃣ Top Selling Categories ---
# NOTE: this query predates the current schema - sales_items has no
# `category` or `amount` column, and sales' primary key is `sale_id`, not
# `id` (see database/pharmacy_postgres.sql). Left as-is from the original
# file; fix the column/table names below before running this section.
query_categories = """
SELECT dm.category, SUM(si.subtotal) AS total_sales
FROM sales_items si
JOIN sales s ON si.sale_id = s.sale_id
JOIN drugs_master dm ON si.drug_id = dm.drug_id
GROUP BY dm.category
ORDER BY total_sales DESC
LIMIT 10
"""
df_categories = pd.read_sql(query_categories, engine)

# Plot top categories
plt.figure(figsize=(10,6))
plt.bar(df_categories['category'], df_categories['total_sales'], color='skyblue')
plt.title("Top Selling Categories")
plt.xlabel("Category")
plt.ylabel("Total Sales")
plt.xticks(rotation=45)
plt.show()
