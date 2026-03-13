-- wrk script: GET order card endpoint
-- Usage:
-- wrk -t4 -c100 -d30s -s loadtest/wrk/order_card.lua http://localhost:8082
--
-- TODO: перед запуском подставьте валидный order_id в path.

wrk.method = "GET"
wrk.path = "/api/cache-demo/orders/37e0b274-405d-4d76-9044-dc08ad25c6f5/card?use_cache=true"
