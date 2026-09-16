package example;

public interface OrderRepository {
    OrderResponse save(OrderRequest request, String paymentId);
}
