-- Database normalization migration for PharmaLink
-- Run once: mysql -u root pharmacy < migration_normalize.sql

-- Supplier inactive reason (manual deactivation note)
ALTER TABLE `suppliers`
  ADD COLUMN IF NOT EXISTS `inactive_reason` VARCHAR(255) DEFAULT NULL AFTER `status`;

-- Persist computed stock status per drug (ok / low / out)
ALTER TABLE `drugs_master`
  ADD COLUMN IF NOT EXISTS `stock_status` ENUM('ok','low','out') NOT NULL DEFAULT 'ok' AFTER `minimum_stock`;

-- Backfill stock_status from current lot totals
UPDATE drugs_master dm
LEFT JOIN (
    SELECT drug_id, COALESCE(SUM(CASE WHEN is_active = 1 THEN current_stock ELSE 0 END), 0) AS total_stock
    FROM inventory_lots
    GROUP BY drug_id
) agg ON dm.drug_id = agg.drug_id
SET dm.stock_status = CASE
    WHEN COALESCE(agg.total_stock, 0) = 0 THEN 'out'
    WHEN COALESCE(agg.total_stock, 0) <= dm.minimum_stock THEN 'low'
    ELSE 'ok'
END
WHERE dm.is_active = 1;
