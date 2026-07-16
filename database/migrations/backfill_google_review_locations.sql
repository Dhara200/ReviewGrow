USE reputation_db;

-- Optional manual backfill.
-- Only run this when you are certain each business has exactly one correct
-- connected Google Business Profile location for its existing Google reviews.
-- If a business has legacy rows from multiple Google locations, review them
-- manually before updating.

UPDATE reviews r
JOIN google_business_connections gbc
    ON gbc.business_id = r.business_id
    AND gbc.google_location_id IS NOT NULL
SET r.google_location_id = gbc.google_location_id
WHERE r.source = 'google'
AND r.google_location_id IS NULL;
