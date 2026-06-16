select
    config_name,
    split_name,
    label,
    family,
    count(*) as sample_count,
    avg(vt_count) as avg_vt_count
from {{ ref('stg_lamda_samples') }}
group by 1, 2, 3, 4
