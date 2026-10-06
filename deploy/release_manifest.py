"""CI helper: emit the same immutable manifest accepted by polytraderctl init."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

digest = os.environ["RELEASE_IMAGE"]
commit = os.environ["RELEASE_COMMIT"]
Path("release.json").write_text(json.dumps({
    "schema_version": 1, "label": "build-" + commit[:12], "commit": commit,
    "built_at": datetime.now(timezone.utc).isoformat(),
    "worker_image": digest, "platform_image": digest,
    "architectures": ["amd64"], "config_schema": 1, "data_schema": 1,
}, indent=2) + "\n", encoding="utf-8")
