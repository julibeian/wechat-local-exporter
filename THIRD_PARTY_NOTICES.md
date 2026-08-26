# Third-party notices

This project contains an independent implementation informed by the following MIT-licensed research projects:

- `328336690/wechat-decrypt`, copyright (c) 2026 328336690. Used as a reference for the WeChat 4.0 SQLCipher page layout, HMAC verification, WAL handling, and database table names.
- `stargazer-2026/wechat-4.1.12-decrypt`, copyright (c) 2026 stargazer-2026. Used as a reference for the WeChat 4.1.11+ master-password KDF change and the `MMV1` codec-function discovery approach.

Both projects were published under the MIT License. Their copyright and permission notices are reproduced below:

> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

The WeChat 4.x image `.dat` segment layout, account image-key derivation, and
`wxgf`/HEVC container handling were independently implemented with reference to
`fanyuantaier/wechatauto-replica` (copyright its contributors), published under
the Apache License 2.0:

- https://github.com/fanyuantaier/wechatauto-replica
- https://www.apache.org/licenses/LICENSE-2.0

No source files from that project are bundled with this application. The
reference project and this software are provided without warranties.
