# API Reference

## Webhook Endpoint

### POST `/webhook`

Accepts a JSON payload to trigger trade operations.

**Headers**
- `Content-Type`: `application/json`
- `Passphrase`: must match `WEBHOOK_PASSPHRASE` in `.env`

**Payload Example**

```json
{
  "symbol": "BTCUSDT",
  "side": "buy",
  "quantity": "0.001",
  "order_type": "market"
}
```

**Response**

- `200 OK`: Trade was processed
- `400 Bad Request`: Validation failed or wrong passphrase
- `500 Internal Server Error`: Unexpected error

