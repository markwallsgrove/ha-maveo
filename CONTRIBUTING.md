# Contributing

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff check custom_components tests
.venv/bin/mypy
.venv/bin/pytest -q
```

Tests run Home Assistant against fakes at the network boundary: a moto server for
Cognito, and a local aiohttp server for the REST API and the stick's MQTT-over-WebSocket
endpoint. Protocol code also has property-based tests (Hypothesis).

`custom_components/maveo/api` is the async cloud client. It has no Home Assistant
imports and uses the `aiohttp` session passed to it.

## Protocol

- **Authentication**: AWS Cognito USER_PASSWORD_AUTH (setup) / REFRESH_TOKEN_AUTH → Identity Pool → temporary AWS credentials
- **Device control**: MQTT 3.1.1 over WebSocket (SigV4 header auth) to AWS IoT Core
- **Management**: Marantec REST API (`/admin` endpoint)

Full protocol documentation is in [`docs/`](docs/), including reverse-engineering notes.

## Polling

| Data | Interval |
|------|----------|
| Device state | 30 s, one MQTT burst |
| Door position while moving, and for 20 s after a door command | 2 s, MQTT |
| Guest keys | 60 s, REST |
| Sticks on the account | 10 min, REST |

## Credentials

Access renews with the Cognito refresh token. When Cognito rejects it, the integration
signs in again with the stored password, and asks the user (reauth) only when Maveo
rejects the password.
