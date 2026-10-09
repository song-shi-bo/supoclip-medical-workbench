# Third-party notices

## video-timeline-copilot

The local Studio timeline adapter uses the upstream
[video-timeline-copilot](https://github.com/ludmila-omlopes/video-timeline-copilot)
package for its EDL validation, SRT export, and FCPXML export.

Copyright (c) 2026 Ludmila Lopes. Distributed under the MIT License.
The fixed upstream source is kept in `backend/vendor/video-timeline-copilot`
with its complete `LICENSE`; it is declared as a local dependency in
`backend/pyproject.toml` so Docker builds do not depend on a Git clone.
