select *
from {{ source('raw_lamda', 'lamda_samples') }}
