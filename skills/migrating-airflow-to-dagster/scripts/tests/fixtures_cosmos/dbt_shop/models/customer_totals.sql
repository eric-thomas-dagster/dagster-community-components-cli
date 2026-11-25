select c.customer_id, c.name, sum(o.amount) as total, count(*) as orders
from {{ ref('stg_customers') }} c join {{ ref('stg_orders') }} o using (customer_id)
where o.amount >= {{ var('min_amount', 0) }}
group by 1, 2
