# Third-party notices

RGB Sync includes material derived from the following open-source project.

## nuga-lib

- Project: https://github.com/mishamyrt/nuga-lib (v0.9.3)
- Author: Mikhael Khrustik
- License: MIT

The NuPhy backend (`rgbsync/backends/nuphy.py`) is a Python port of the USB HID
protocol reverse-engineered in nuga-lib: device discovery, the feature-report
transport, the read/write commands and the byte layout of the lighting
("effects") block and color palette. See the header of that file for the list
of ported parts and the deliberate differences.

The nuga-lib license text follows, unmodified:

---

MIT License

Copyright (c) 2024 Mikhael Khrustik

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
