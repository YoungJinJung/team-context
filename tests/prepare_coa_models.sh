#!/usr/bin/env bash
# Generate pinned upstream COA API models in its disposable checkout.
set -euo pipefail
coa_root="$(cd "$1" && pwd)"
(cd "$coa_root/models" && ./gradlew build)
output="$coa_root/smithy-generated"
mkdir -p "$output/openapi/openapi" "$output/openapi/python-codegen"
cp "$coa_root/models/build/smithyprojections/models/openapi-control-plane/openapi/ControlPlaneService.openapi.json" "$output/openapi/openapi/"
cp "$coa_root/models/build/smithyprojections/models/openapi-data-layer/openapi/DataLayerService.openapi.json" "$output/openapi/openapi/"
python3 - "$output/openapi/openapi/ControlPlaneService.openapi.json" "$output/openapi/python-codegen/ControlPlaneService.openapi.json" <<'PY'
import json
import sys

def strip(node):
    if isinstance(node, dict):
        if node.get('format') == 'password':
            del node['format']
        for value in node.values():
            strip(value)
    elif isinstance(node, list):
        for value in node:
            strip(value)

with open(sys.argv[1]) as source:
    spec = json.load(source)
strip(spec)
with open(sys.argv[2], 'w') as target:
    json.dump(spec, target)
PY
jar="$coa_root/openapi-generator-7.12.0.jar"
curl --fail --silent --show-error --location -o "$jar" https://repo.maven.apache.org/maven2/org/openapitools/openapi-generator-cli/7.12.0/openapi-generator-cli-7.12.0.jar
printf '%s  %s\n' 33e7dfa7a1f04d58405ee12ae19e2c6fc2a91497cf2e56fa68f1875a95cbf220 "$jar" | shasum -a 256 --check
java -jar "$jar" generate -i "$output/openapi/python-codegen/ControlPlaneService.openapi.json" -g python -o "$output/control-plane-python-server" --package-name coa_control_plane_server --global-property models,supportingFiles=__init__.py --additional-properties=generateSourceCodeOnly=true
python3 - "$output/control-plane-python-server/coa_control_plane_server/__init__.py" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
lines = [line for line in p.read_text().splitlines() if line.startswith('from coa_control_plane_server.models.')]
if not lines:
    raise SystemExit('Generated server package exports no models')
p.write_text('\n'.join(lines) + '\n')
PY
