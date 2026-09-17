-- Preserve all ingested features and provenance in a stable staging view for downstream models.
select *
from {{ source('raw_lamda', 'lamda_samples') }}
