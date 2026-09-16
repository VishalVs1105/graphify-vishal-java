package example;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/orders")
public class OrderController {
    private OrderService service;

    @PostMapping
    public ResponseEntity<OrderResponse> create(@RequestBody OrderRequest request) {
        if (request == null || request.quantity <= 0) {
            return ResponseEntity.badRequest().build();
        }
        OrderResponse response = service.create(request);
        return response == null ? ResponseEntity.notFound().build() : ResponseEntity.ok(response);
    }
}
