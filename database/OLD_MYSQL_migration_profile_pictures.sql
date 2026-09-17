-- =====================================================================
-- migration_profile_pictures.sql
-- Run this ONCE in phpMyAdmin (or `mysql -u root pharmacy_db < migration_profile_pictures.sql`)
-- Adds a profile_image column so Admin/Cashier/Customer can upload a
-- profile picture. Safe to run once — running it twice will error with
-- "Duplicate column name", which just means it's already applied.
-- =====================================================================

ALTER TABLE `staff_info`
  ADD COLUMN `profile_image` VARCHAR(255) DEFAULT NULL AFTER `address`;

ALTER TABLE `customers`
  ADD COLUMN `profile_image` VARCHAR(255) DEFAULT NULL AFTER `email`;
