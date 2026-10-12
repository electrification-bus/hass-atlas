# span_panel test fixtures

`topology/*.json` and `expected_entities/*.json` are copied unchanged from `tests/fixtures/topology/` and `tests/fixtures/expected_entities/` of [SpanPanel/span](https://github.com/SpanPanel/span), the `span_panel` Home Assistant integration (commit `9ce2463` on `prerelease/2.2`). Files with the same stem describe the same panel: `topology/` holds the `span_panel/panel_topology` WebSocket response, and `expected_entities/` holds the device and entity registry rows the integration creates, keyed by identifier and unique_id.

`tests/_span_panel_loader.py` turns `expected_entities/` into HA registry rows, model objects, and entity states.

## synthetic/

`synthetic/` holds edited replays of the `r202639-b`, `r202639-c`, and `r202639-e` captures above, produced with the replay harness of the span integration at tag `v2.2.0b2`: each edited capture went through the integration's real setup, which wrote `topology/`, `expected_entities/`, and `states/` (entity_id to `{state, attributes}`, the recorded attributes the loader uses in place of derived ones). The edits set each hosted circuit's `connection/feeds-role` and adjusted readings:

| Stem | Source | Edits |
|---|---|---|
| `other-solar-storage` | `r202639-c` | circuit-4 "Other" (2-pole, 240 V) set to SOLAR, with active-power +3840.0, current 16.0, imported-energy 6215402.3; circuit-2 "Bathroom / Outlets" set to STORAGE; panel `power-flows/pv` set to -3840.0; upstream lugs `fed-by-device-id` set to the battery, with type and status `OK`. |
| `other-subpanel` | `r202639-e` | circuit-4 "Laundry Room" (2-pole) set to SUBPANEL; every other hosted circuit set to LOADS. |
| `other-solar-only` | `r202639-b` | circuit-32 "breaker-16" (1-pole, 120 V) set to SOLAR, with active-power +1920.0, current 16.0, imported-energy 2840117.5; every other hosted circuit set to LOADS; remote-ct-3 unchanged. |

Panel grid and site power flows were not rebalanced, so panel and lug readings do not add up. Circuit names are as captured. In `other-solar-storage`, circuit-5 "Tesla" still names the battery as the device it feeds while the lugs also name it as their feed. Only `other-solar-storage` publishes `at_service_entrance` (false); the other two leave it off.

## License

These files, and the edited replays in `synthetic/` derived from them, are used under the MIT License:

```
MIT License

Copyright (c) 2025 Bill Flood <cayossarian@users.noreply.github.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
