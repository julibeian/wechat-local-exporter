# Third-party notices

This project contains an independent implementation informed by the following MIT-licensed research projects:

- `328336690/wechat-decrypt`, copyright (c) 2026 328336690. Used as a reference for the WeChat 4.0 SQLCipher page layout, HMAC verification, WAL handling, and database table names.
- `stargazer-2026/wechat-4.1.12-decrypt`, copyright (c) 2026 stargazer-2026. Used as a reference for the WeChat 4.1.11+ master-password KDF change and the `MMV1` codec-function discovery approach.
- `Evil0ctal/WeChat-Channels-Video-File-Decryption`, copyright (c) 2025 Evil0ctal. Used as an independent reference for WeChat's numeric-seed ISAAC-64 keystream ordering used by encrypted CDN media.

These projects were published under the MIT License. Their copyright and permission notices are reproduced below:

> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

The WeChat 4.x image `.dat` segment layout, account image-key derivation, and
`wxgf`/HEVC container handling, together with the documented `sns.db` /
`SnsTimeLine` Moments storage route, were independently implemented with reference to
`fanyuantaier/wechatauto-replica` (copyright its contributors), published under
the Apache License 2.0:

- https://github.com/fanyuantaier/wechatauto-replica
- https://www.apache.org/licenses/LICENSE-2.0

No source files from that project are bundled with this application. The
reference project and this software are provided without warranties.

The ISAAC-64 implementation is independently written from the algorithm
description and public-domain reference implementation by Bob Jenkins:

- https://burtleburtle.net/bob/rand/isaacafa.html
- https://burtleburtle.net/bob/c/isaac64.c

The offline HTML viewer's `icons/github.svg` is the unmodified black Invertocat
SVG from GitHub's official logo download. It links to this project's GitHub
repository. GitHub and the Invertocat design are trademarks of GitHub, Inc.;
this asset is not covered by the MIT license for the other Feather icons.

- https://brand.github.com/foundations/logo
- https://brand.github.com/GitHub_Logos.zip

The native face PNGs in `wechat_exporter/html_assets/faces` are unchanged emoji
resources extracted from the local Windows Weixin 4.1.15.51 client. Copyright
Tencent, all rights reserved. The project's MIT license does not cover this
artwork. The resource folder's `NOTICE.txt` and `provenance.json` document the
source and original image hashes. Offline exports copy that notice with the
faces used by the conversation.
