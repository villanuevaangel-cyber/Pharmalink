-- =====================================================================
-- migration_customer_order_token.sql
-- Run this ONCE in phpMyAdmin (or `mysql -u root pharmacy_db < migration_customer_order_token.sql`)
--
-- Adds order_token to `customer_orders`. Required by
-- process_customer_order.php (the customer-facing "Submit Order for
-- Pickup" backend) for its duplicate-submission protection — without
-- this column, EVERY customer checkout fails, because the very first
-- query the endpoint runs references order_token.
--
-- (`is_read` is already present in this database's customer_orders
-- table, so it is not included here.)
--
-- Safe to run once — running it twice will error with
-- "Duplicate column name", which just means it's already applied.
-- =====================================================================

ALTER TABLE `customer_orders`
  ADD COLUMN `order_token` VARCHAR(64) NULL UNIQUE AFTER `sc_pwd_applied`;