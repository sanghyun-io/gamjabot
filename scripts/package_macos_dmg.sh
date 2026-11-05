#!/usr/bin/env bash
# Headless DMG from a completed Tauri .app; no Finder/AppleScript interaction.
set -euo pipefail

app=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
output=$2
core_prefix=${3:-}
core_target=${4:-aarch64-apple-darwin}
expected_manifest=${5:-}
expected_core=${6:-}
expected_engine=${7:-}
expected_helper=${8:-}
expected_artifact=${9:-}
signed_receipt=${10:-}
package_binding=${11:-}
fixed_manifest=c7db6f970b456e75eb23359dfdbe79e2aa1cb16c4c72eed5649bae3a3a11b27f
fixed_core=b885e14bbacfdcbe7c68e4ff051db2d33ade88733d69471897fe4ed55da10a31
fixed_engine=2f6b534d61ef0212248ec76b6f92b9a9e5dc9e15dae21622c8ab785fa2bfded0
fixed_helper=0d10afdab1573a67b5be78c9746512e56a2a81f1d7afd6d77efa7c8a00ae0d5d
if [[ -n $core_prefix ]]; then
  [[ $expected_manifest =~ ^[0-9a-f]{64}$ && $expected_core =~ ^[0-9a-f]{64}$ && $expected_engine =~ ^[0-9a-f]{64}$ && $expected_helper =~ ^[0-9a-f]{64}$ && $expected_artifact =~ ^[0-9a-f]{64}$ ]] || {
    echo "exact core and artifact hashes are required" >&2
    exit 1
  }
fi
if [[ -n $signed_receipt || -n $package_binding ]]; then
  [[ -n $core_prefix && -n $signed_receipt && -n $package_binding ]] || {
    echo "signed receipt and package binding output require the verified Core package" >&2
    exit 1
  }
fi
verify_core() {
  "${GAMJA_BUILD_PYTHON:-python3}" "$(dirname "$0")/build_core.py" --verify "$1" --target "$core_target"
  cmp "$core_prefix/core-manifest.json" "$1/core-manifest.json"
  if [[ -n $core_prefix ]]; then
    [[ $(shasum -a 256 "$1/core-manifest.json" | awk '{print $1}') == "$expected_manifest" ]]
    [[ $(shasum -a 256 "$1/bin/gamja" | awk '{print $1}') == "$expected_core" ]]
    [[ $(shasum -a 256 "$1/libexec/gamja/gamja-engine" | awk '{print $1}') == "$expected_engine" ]]
    [[ $(shasum -a 256 "$1/libexec/gamja/gamja-platform-helper" | awk '{print $1}') == "$expected_helper" ]]
  fi
}
verify_artifact() {
  local artifact="$1/Contents/MacOS/gamja-artifact"
  [[ -f $artifact && ! -L $artifact && -x $artifact ]]
  [[ $(shasum -a 256 "$artifact" | awk '{print $1}') == "$expected_artifact" ]]
  codesign --verify --strict --all-architectures "$artifact"
  local details
  details=$(codesign -dv --verbose=4 "$artifact" 2>&1)
  [[ $details == *"Identifier=top.gamja.artifact"* && $details == *"Signature=adhoc"* && $details != *"Timestamp="* ]]
}
stage=$(mktemp -d "${TMPDIR:-/tmp}/gamja-dmg-stage.XXXXXX")
mount=$(mktemp -d "${TMPDIR:-/tmp}/gamja-dmg-mount.XXXXXX")
pending_dir=
mounted=0
cleanup() {
  if [[ $mounted == 1 ]]; then hdiutil detach "$mount" -quiet || true; fi
  rm -rf "$stage" "$mount"
  if [[ -n $pending_dir ]]; then rm -rf "$pending_dir"; fi
}
trap cleanup EXIT

[[ -d "$app/Contents/MacOS" ]] || { echo "missing app: $app" >&2; exit 1; }
if [[ -n $core_prefix ]]; then verify_artifact "$app"; fi
help_file="$(dirname "$0")/Diagnostics Help.txt"
[[ -s "$help_file" ]] || { echo "missing diagnostics help: $help_file" >&2; exit 1; }
embedded_help="$app/Contents/Resources/Diagnostics Help.txt"
[[ -f "$embedded_help" && ! -L "$embedded_help" ]] || {
  echo "missing embedded diagnostics help" >&2
  exit 1
}
cmp "$help_file" "$embedded_help"
staged_app="$stage/$(basename "$app")"
ditto "$app" "$staged_app"
if [[ -n $core_prefix ]]; then
  verify_core "$core_prefix"
  verify_core "$staged_app/Contents/Resources/gamja-core"
  verify_artifact "$staged_app"
fi
cmp "$help_file" "$staged_app/Contents/Resources/Diagnostics Help.txt"
codesign --verify --deep --strict "$staged_app"
if [[ -n $core_prefix ]]; then
  verify_core "$staged_app/Contents/Resources/gamja-core"
  verify_artifact "$staged_app"
fi
mkdir -p "$(dirname "$output")"
pending_dir=$(mktemp -d "$(dirname "$output")/.gamja-dmg.XXXXXX")
pending="$pending_dir/$(basename "$output")"
cp "$help_file" "$stage/Diagnostics Help.txt"
ln -s /Applications "$stage/Applications"
hdiutil create -srcfolder "$stage" -volname "Gamja Bot" -format UDZO -ov "$pending" -quiet
[[ -s $pending ]]
digest=$(shasum -a 256 "$pending" | awk '{print $1}')
hdiutil attach -nobrowse -readonly -mountpoint "$mount" "$pending" -quiet
mounted=1
if [[ -n $core_prefix ]]; then
  verify_core "$mount/$(basename "$app")/Contents/Resources/gamja-core"
  verify_artifact "$mount/$(basename "$app")"
else
  cmp "$staged_app/Contents/MacOS/backend" "$mount/$(basename "$app")/Contents/MacOS/backend"
fi
cmp "$help_file" "$mount/Diagnostics Help.txt"
cmp "$help_file" "$mount/$(basename "$app")/Contents/Resources/Diagnostics Help.txt"
codesign --verify --deep --strict "$mount/$(basename "$app")"
[[ $(readlink "$mount/Applications") == /Applications ]]
if [[ -n $package_binding ]]; then
  "${GAMJA_BUILD_PYTHON:-python3}" \
    "$(dirname "$0")/../.superpowers/sdd/2026-10-06-first-success/ops/first_success_cutover_v2.py" \
    bind-package \
    --signed-receipt "$signed_receipt" \
    --candidate-app "$app" \
    --mounted-app "$mount/$(basename "$app")" \
    --dmg "$pending" \
    --published-dmg "$output" \
    --package-binding "$package_binding"
fi
hdiutil detach "$mount" -quiet
mounted=0
[[ $(shasum -a 256 "$pending" | awk '{print $1}') == "$digest" ]]
mv -f "$pending" "$output"
echo "headless DMG mounted, runtime payload byte-identical, Applications link verified: $output SHA-256 $digest"
