# Source-only Java API documentation fixture

This deliberately small service is a parser/documentation example, not a deployable Spring application. Its repository and payment interfaces intentionally have no implementation; the graph must not invent a database or remote controller behind them.

From the repository root:

```powershell
graphify extract ./examples/order-service --no-cluster --force
graphify api-docs --graph ./examples/order-service/graphify-out/graph.json --output ./docs/example-api --strict --force
```

Expected structural calls: OrderController.create → OrderService.create → possible DefaultOrderService.create, then InventoryRepository.available, PaymentClient.charge and OrderRepository.save under their recorded guards.

Expected outcomes: invalid request returns a bad-request expression; unavailable inventory returns null; null payment ID throws; controller selects not-found vs OK based on its response. Framework methods remain unresolved because dependency bodies are not included.

See the committed docs/example-api reference and tests/test_java_api_docs.py for executable expectations.
