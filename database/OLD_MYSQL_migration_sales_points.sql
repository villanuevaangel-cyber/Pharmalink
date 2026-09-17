-- =====================================================================
-- migration_sales_points.sql
-- Run this ONCE in phpMyAdmin (or `mysql -u root pharmacy_db < migration_sales_points.sql`)
--
-- Adds points_redeemed / points_discount_value columns to `sales`.
-- Required by transactions.php's loyalty-points redemption feature —
-- without this migration, EVERY sale (not just ones redeeming points)
-- fails at checkout with a SQL error, because transactions.php's INSERT
-- into `sales` unconditionally references these two columns.
--
-- Safe to run once — running it twice will error with
-- "Duplicate column name", which just means it's already applied.
-- =====================================================================

ALTER TABLE `sales`
  ADD COLUMN `points_redeemed` DECIMAL(10,2) NOT NULL DEFAULT 0.00 AFTER `payment_method`,
  ADD COLUMN `points_discount_value` DECIMAL(10,2) NOT NULL DEFAULT 0.00 AFTER `points_redeemed`;