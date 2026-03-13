"""
Locust template for LAB 05 RPS measurements.

Run:
locust -f loadtest/locustfile.py --host=http://localhost:8082
"""

from locust import HttpUser, task, between


class CacheUser(HttpUser):
    wait_time = between(0.1, 0.5)

    @task(3)
    def get_catalog(self):
        self.client.get("/api/cache-demo/catalog?use_cache=true")

    @task(2)
    def get_order_card(self):
        # TODO: заменить order_id на существующий
        self.client.get("/api/cache-demo/orders/37e0b274-405d-4d76-9044-dc08ad25c6f5/card?use_cache=true")
