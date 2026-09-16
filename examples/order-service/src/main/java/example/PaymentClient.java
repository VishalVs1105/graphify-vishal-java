package example;

public interface PaymentClient {
    String charge(String customerId, int quantity);
}
