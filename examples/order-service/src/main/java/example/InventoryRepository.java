package example;

public interface InventoryRepository {
    boolean available(String sku, int quantity);
}
