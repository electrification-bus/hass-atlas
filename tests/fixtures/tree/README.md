# tree-v1 fixtures

`r202639-a-tree-v1.json` to `r202639-e-tree-v1.json` and `main32_r202639-tree-v1.json` are masked captures of real SPAN panels on firmware `spanos3/r202639/03`, copied unchanged from `tests/fixtures/captures/` and `tests/fixtures/` of [SpanPanel/span-panel-api](https://github.com/SpanPanel/span-panel-api) (branch `prerelease/3.8`, commit `877c5aa`). Each is byte-identical to the file of the same name in [electrification-bus/distribution-enclosure-simulator](https://github.com/electrification-bus/distribution-enclosure-simulator). Their panels report `info/model` UNKNOWN (`r202639-a`), MAIN_16 (`b`), MAIN_40 (`c`), MLO_24 (`d`), MLO_48 (`e`), and MAIN_32 (`main32_r202639`).

In this format each device carries its parsed `$description` (with `parent`) and its retained values split into `properties` and `numeric_properties`. `tests/_fixture_loader.py` reads both this format and the format of `nt-0000-abc12.json` and `nt-0000-def34.json`.

## License

The six captures are used under the MIT License:

```
MIT License

Copyright (c) 2026 Bill Flood, Clark Communications Corporation

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
