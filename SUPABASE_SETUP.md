# PharmaLink — FastAPI + Supabase

The app is HTML/CSS/JS on the front end and Python FastAPI on the back end.
The database is Supabase (PostgreSQL). PHP is not used.

## 1. Create a Supabase project

1. Go to https://supabase.com → New Project.
2. Save the database password — you will need it in step 3.

## 2. Load the database schema

1. In the Supabase Dashboard, open **SQL Editor**.
2. Copy the full contents of `database/pharmacy_postgres.sql`.
3. Paste and **Run**. This creates tables, keys, indexes, and seed data.
   You do not need to run the historical `OLD_MYSQL_*.sql` files in
   `database/` — keep them only as reference.

## 3. Configure the connection

1. Copy `.env.example` to `.env`.
2. In Supabase Dashboard → **Project Settings → Database → Connection
   info**, copy Host, Port, Database name, User, and Password into `.env`.
   - If port 5432 is blocked, use the **Session pooler** host/port
     (often 6543).
3. Set `SESSION_SECRET` to a long random string.
4. Fill SMTP settings if you want password-reset email.
5. **Do not** commit `.env`.

## 4. Run the app

From this folder (`PharmaLink/`):

```
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Open http://127.0.0.1:8080 and log in. Demo accounts:

- Admin: `admin@pharmalink.ph` / `admin123`
- Customer: `customer@pharmalink.ph` / `customer123`

Staff (cashier/pharmacist) accounts are in the `users` table. An admin can
also open cashier pages.

## Layout

```
app/                     → FastAPI (auth, customer, cashier, staff, admin)
python/                  → forecast_prophet.py, sales_forecast.py, segment_kmeans.py
admin/                   → admin.html + css/js
cashier/                 → cashier.html + css/js
customer/                → customer.html + css/js
shared/                  → login.css, login.js
assets/                  → theme.css, theme.js
database/                → pharmacy_postgres.sql + historical MySQL dumps
uploads/                 → prescriptions/, profile_pictures/
```

## Notes

- `sales.customer_id` references `users(user_id)`, not `customers(customer_id)`.
  That matches the original schema.
- User and lot primary keys are integers without IDENTITY; inserts use
  `MAX(id)+1`.
