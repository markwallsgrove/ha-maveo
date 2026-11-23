# Maveo for Home Assistant

[![HA Version](https://img.shields.io/badge/Home%20Assistant-2026.10%2B-blue)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/github/license/markwallsgrove/ha-maveo)](https://opensource.org/license/mit)

Open, close and stop your **Marantec maveo** garage door, switch its light, and see
what the door is doing, all from Home Assistant.

> **Unofficial.** This is a community project. It is not made, endorsed or supported
> by Marantec. Marantec and maveo are trademarks of their owners.

> **Needs the internet.** Everything goes through the Marantec cloud, using your maveo
> account. Nothing works while your internet or the Marantec cloud is down.

---

## Supported devices

- Marantec **Maveo Connect** stick
- Marantec **Maveo BlueFi** stick, including the optional indoor temperature/humidity (H+T) sensor

Any stick on your Maveo account is added. Sticks added to or removed from the account
later are picked up within 10 minutes, without a restart.

## Supported functions

| Feature | What you get |
|---------|--------------|
| Garage door | Open, close and stop; shows open, closed, opening or closing |
| Garage light | Turn on and off |
| Opening time / Closing time | How many seconds the last full open or close took; the history shows if the door slows down |
| Openings | How many times the door has opened |
| Time to close | The stick's auto-close timer, in minutes |
| Outdoor temperature / humidity | The weather where the stick is (needs a location set in the maveo app) |
| Indoor temperature / humidity / battery | From the optional maveo indoor sensor |
| Location | The stick on the map |
| Guest keys | One sensor per guest key: time left, access level, whether it has been used |
| Diagnostics | Online status, firmware, WiFi signal, buzzer and stick type |

Guest key sensors appear and disappear as keys are created or revoked. The WiFi signal
and stick type sensors are off by default; turn them on from the device page.

## Use cases

- See whether the garage is open from anywhere, and close it from the dashboard.
- Close the garage automatically at night or when everybody leaves.
- Give a visitor or tradesperson a time-limited key from a script.
- Spot a door that gets slower over time, an early sign of wear.

---

## Installation

1. Open **Marketplace** (or HACS) → three-dot menu → **Custom repositories**.
2. Add `https://github.com/markwallsgrove/ha-maveo` with the category **Integration**.
3. Find **Maveo**, download it, and restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration** and search for **Maveo**.

### Configuration parameters

| Parameter | Description |
|-----------|-------------|
| Email | The email address of your Maveo account |
| Password | Your Maveo password |
| Region | `EU` (Europe) or `US` (North America): where your account was created |

Change the password or region later with **Reconfigure** on the integration entry.
There are no other options.

You sign in once. Home Assistant only asks for your password again if maveo rejects it,
for example after you change it.

Home Assistant stores your maveo password unencrypted in its configuration folder, as it
does for other cloud integrations. Keep backups of that folder private.

### Removal

1. **Settings → Devices & services → Maveo** → three-dot menu → **Delete**.
2. Optionally remove **Maveo** from the Marketplace (or HACS) and restart Home Assistant.

Deleting the entry does not change anything on your Maveo account. Guest keys stay
valid until they expire or you revoke them in the Maveo app.

---

## Actions

Both actions are admin only and take the garage **device** (device picker in the UI).

### `maveo.create_guest`

Creates a time-limited guest key and returns it in the action response: `user_id`,
`link` (the Maveo share link) and `expires_at`. The link grants access to the garage,
so share it only with the guest.

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `device_id` | ✓ | — | The Maveo garage device |
| `ttl_hours` | — | 24 | Validity in hours (1–720) |
| `admin` | — | false | Full remote access without geofence restriction |

### `maveo.remove_guest`

Revokes a guest key immediately.

| Field | Required | Description |
|-------|----------|-------------|
| `device_id` | ✓ | The Maveo garage device |
| `user_id` | ✓ | Guest UUID (`user_id` attribute of the guest sensor) |

## Examples

Close the garage at 22:00 if it is open:

```yaml
automation:
  - alias: Close garage at night
    triggers:
      - trigger: time
        at: "22:00:00"
    conditions:
      - condition: state
        entity_id: cover.garage_garage_door
        state: open
    actions:
      - action: cover.close_cover
        target:
          entity_id: cover.garage_garage_door
```

Create a 4-hour guest key and send the link to your phone:

```yaml
script:
  garage_guest_key:
    sequence:
      - action: maveo.create_guest
        data:
          device_id: YOUR_DEVICE_ID  # pick the device in the UI editor
          ttl_hours: 4
        response_variable: guest
      - action: notify.mobile_app_my_phone
        data:
          message: "Garage key: {{ guest.link }} (valid until {{ guest.expires_at }})"
```

---

## Data updates

The door, light and sensors update every 30 seconds. While the door moves, and for
20 seconds after you open, close or stop it, the door updates every 2 seconds. Guest keys
update every minute, and new or removed sticks on your account appear within 10 minutes.

## Known limitations

- A door moved with a remote or wall button shows up within about 30 seconds.
- Opening and closing times are accurate to about 2 seconds. A door that was never seen
  moving is counted but not timed.
- Accounts with two-step sign-in are not supported.
- Indoor sensor readings have not been tested with a real sensor.

## Troubleshooting

- **Asked to sign in again**: Maveo rejected the stored password. Enter the current one.
- **Entities unavailable**: the cloud or the stick did not answer. This is logged once
  and again when it recovers. Check the stick's WiFi and the Maveo app.
- **Cloud connectivity is off**: the stick is reachable through the cloud but does not
  answer; power-cycle the stick.
- For more detail, enable debug logging and attach the diagnostics download to an issue:

```yaml
logger:
  logs:
    custom_components.maveo: debug
```

## Upgrading from 0.x

1.0 has breaking changes:

- `sensor.*_stick_type` reports `bluefi` / `connect`, and the indoor sensor reports
  `paired` / `not_paired` (both are translated in the UI).
- `maveo.create_guest` and `maveo.remove_guest` take a Home Assistant device instead of the
  numeric Maveo device ID.
- The WiFi signal and stick type sensors are disabled by default for new installs.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits

Based on [yoda-jm/ha-maveo](https://github.com/yoda-jm/ha-maveo). This integration works
by talking to the Marantec cloud the same way the maveo app does. Use it at your own risk.
