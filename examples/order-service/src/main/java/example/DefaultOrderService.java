package example;

public class DefaultOrderService implements OrderService {
    private InventoryRepository inventory;
    private PaymentClient payments;
    private OrderRepository orders;

    public OrderResponse create(OrderRequest request) {
        if (!inventory.available(request.sku, request.quantity)) {
            return null;
        }
        String paymentId = payments.charge(request.customerId, request.quantity);
        if (paymentId == null) {
            throw new IllegalStateException("Payment was declined");
        }
        return orders.save(request, paymentId);
    }
}
