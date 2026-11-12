// Contract: MessageRecord.processing_status check constraint and DuplicateStatus.
export const PROCESSING_STATUSES = ["ingested", "processing", "processed", "failed"] as const;
export const DUPLICATE_STATUSES = ["unchecked", "unique", "duplicate_skipped", "duplicate_reclassify"] as const;
