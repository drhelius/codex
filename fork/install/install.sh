#!/bin/sh
# Install complete, verified fork packages without changing official Codex.
set -eu

fail() { printf 'codex-mcp: %s\n' "$*" >&2; exit 1; }
release="${CODEX_MCP_RELEASE:-latest}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --release) [ "$#" -ge 2 ] || fail '--release needs a tag'; release="$2"; shift 2 ;;
    --help|-h)
      echo 'Usage: install.sh [--release latest|mcp-rust-vMAJOR.MINOR.PATCH-rN]'
      echo 'Installs or updates codex-mcp; official codex is left untouched.'
      echo 'Settings: CODEX_MCP_INSTALL_ROOT, CODEX_MCP_BIN_DIR, CODEX_MCP_MODIFY_PATH=0'
      exit 0 ;;
    *) fail "Unknown argument: $1" ;;
  esac
done
valid_tag() {
  printf '%s\n' "$1" | LC_ALL=C awk 'NR != 1 || $0 !~ /^mcp-rust-v[0-9]+\.[0-9]+\.[0-9]+-r[1-9][0-9]*$/ {bad=1} END {exit bad || NR != 1}'
}
[ "$release" = latest ] || valid_tag "$release" || fail 'Invalid stable fork release tag'

case "$(uname -s)" in
  Darwin) platform=apple-darwin ;;
  Linux) platform=unknown-linux-musl ;;
  *) fail 'Use fork/install/install.ps1 on Windows' ;;
esac
case "$(uname -m)" in
  x86_64|amd64) arch=x86_64 ;;
  arm64|aarch64) arch=aarch64 ;;
  *) fail 'Unsupported architecture' ;;
esac
if [ "$platform" = apple-darwin ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || true)" = 1 ]; then
  arch=aarch64
fi
target="$arch-$platform"
asset="codex-mcp-$target.tar.gz"
root="${CODEX_MCP_INSTALL_ROOT:-$HOME/.local/share/codex-mcp}"
bin_dir="${CODEX_MCP_BIN_DIR:-$HOME/.local/bin}"
for path in "$root" "$bin_dir"; do
  case "$path" in /*) ;; *) fail 'Install paths must be absolute' ;; esac
  case "$path" in *'
'*|*':'*) fail 'Install paths cannot contain newlines or colons' ;; esac
done
mkdir -p "$root" "$bin_dir"
root="$(cd "$root" && pwd -P)"
bin_dir="$(cd "$bin_dir" && pwd -P)"
command_path="$bin_dir/codex-mcp"
expected_link="$root/current/bin/codex-mcp"
if [ -e "$command_path" ] || [ -L "$command_path" ]; then
  [ "$(readlink "$command_path" || true)" = "$expected_link" ] || fail "Refusing to replace unrelated $command_path"
fi
if [ -e "$root/current" ] && [ ! -L "$root/current" ]; then
  fail "Refusing to replace unrelated $root/current"
fi
mkdir "$root/install.lock" 2>/dev/null || fail "Another install is locked at $root/install.lock (remove it only after its installer has stopped)"
temporary=""
cleanup() {
  [ -z "$temporary" ] || rm -rf "$temporary"
  rmdir "$root/install.lock"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
temporary="$(mktemp -d "$root/.staging.XXXXXXXX")"

download() {
  if command -v curl >/dev/null 2>&1; then
    curl -q --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
      --connect-timeout 20 --max-time 600 --retry 2 --output "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget --https-only --timeout=60 --tries=3 -q -O "$2" "$1"
  else
    fail 'curl or wget is required'
  fi
}
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}
verify() {
  printf '%s\n' "$2" | LC_ALL=C grep -Eq '^[0-9a-f]{64}$' || fail 'Missing SHA-256 digest'
  [ "$(sha256 "$1")" = "$2" ] || fail "Checksum mismatch: $(basename "$1")"
}

parse_release_metadata() {
  # Bound awk's record size so compact, single-line JSON stays fast on every
  # supported awk implementation. JSON strings cannot contain literal newlines,
  # so the record boundaries inserted by fold do not change the document.
  LC_ALL=C fold -b -w 4096 | LC_ALL=C awk '
    function finish_string(value) {
      if (object_depth == 1 && key == "tag_name") {
        print "tag_name\t" value
      } else if (object_depth == asset_object_depth) {
        if (key == "name") {
          asset_name = value
        } else if (key == "digest") {
          asset_digest = value
        }
      }

      expecting_value = 0
      key = ""
    }

    {
      for (i = 1; i <= length($0); i++) {
        char = substr($0, i, 1)

        if (in_string) {
          if (escaped) {
            token = token "\\" char
            escaped = 0
          } else if (char == "\\") {
            escaped = 1
          } else if (char == "\"") {
            in_string = 0
            if (string_is_value) {
              finish_string(token)
            } else {
              pending_key = token
            }
          } else {
            token = token char
          }
          continue
        }

        if (object_depth == 1 && expecting_value && (key == "draft" || key == "prerelease") && char == "t") {
          print "ineligible\t" key
        }
        if (char == "\"") {
          in_string = 1
          token = ""
          escaped = 0
          string_is_value = expecting_value
        } else if (char == ":" && pending_key != "") {
          key = pending_key
          pending_key = ""
          expecting_value = 1
        } else if (char == "{") {
          object_depth++
          if (assets_array_depth != 0 &&
              array_depth == assets_array_depth &&
              asset_object_depth == 0) {
            asset_object_depth = object_depth
            asset_name = ""
            asset_digest = ""
          }
          expecting_value = 0
          key = ""
        } else if (char == "}") {
          if (object_depth == asset_object_depth) {
            if (asset_name != "" && asset_digest != "") {
              print "asset\t" asset_name "\t" asset_digest
            }
            asset_object_depth = 0
            asset_name = ""
            asset_digest = ""
          }
          object_depth--
          expecting_value = 0
          key = ""
          pending_key = ""
        } else if (char == "[") {
          array_depth++
          if (expecting_value && key == "assets" && object_depth == 1) {
            assets_array_depth = array_depth
          }
          expecting_value = 0
          key = ""
        } else if (char == "]") {
          if (array_depth == assets_array_depth) {
            assets_array_depth = 0
          }
          array_depth--
          expecting_value = 0
          key = ""
          pending_key = ""
        } else if (char == ",") {
          expecting_value = 0
          key = ""
          pending_key = ""
        }
      }
    }

    END {
      if (in_string || object_depth != 0 || array_depth != 0) {
        exit 1
      }
    }
  '
}

endpoint="tags/$release"
[ "$release" != latest ] || endpoint=latest
download "https://api.github.com/repos/drhelius/codex/releases/$endpoint" "$temporary/release.json" ||
  fail 'No published fork release is available, or GitHub could not be reached; the current installation is unchanged'
parse_release_metadata < "$temporary/release.json" > "$temporary/metadata"
if grep -q '^ineligible' "$temporary/metadata"; then fail 'Draft and prerelease packages are excluded'; fi
tag="$(awk -F '\t' '$1 == "tag_name" {print $2}' "$temporary/metadata")"
valid_tag "$tag" || fail 'GitHub did not return a stable fork release'
[ "$release" = latest ] || [ "$release" = "$tag" ] || fail 'Release identity mismatch'
base="https://github.com/drhelius/codex/releases/download/$tag"
digest="$(awk -F '\t' '$1 == "asset" && $2 == "SHA256SUMS" {sub(/^sha256:/, "", $3); print $3}' "$temporary/metadata")"
download "$base/SHA256SUMS" "$temporary/SHA256SUMS"
verify "$temporary/SHA256SUMS" "$digest"
digest="$(awk -v asset="$asset" '$2 == asset && NF == 2 {print $1}' "$temporary/SHA256SUMS")"
api_digest="$(awk -F '\t' -v asset="$asset" '$1 == "asset" && $2 == asset {sub(/^sha256:/, "", $3); print $3}' "$temporary/metadata")"
[ "$digest" = "$api_digest" ] || fail 'Archive digest does not match release metadata'
download "$base/$asset" "$temporary/$asset"
verify "$temporary/$asset" "$digest"

# The publisher creates only regular files/directories. Reject links and traversal
# before extraction, even though the archive already matches GitHub's digest.
tar -tzf "$temporary/$asset" > "$temporary/paths"
LC_ALL=C awk '/^\// || /(^|\/)\.\.(\/|$)/ || /\\/ {exit 1}' "$temporary/paths" || fail 'Unsafe archive path'
tar -tvzf "$temporary/$asset" > "$temporary/types"
LC_ALL=C awk 'substr($0,1,1) !~ /[-d]/ {exit 1}' "$temporary/types" || fail 'Unsupported archive entry'
mkdir "$temporary/package"
tar -xzf "$temporary/$asset" -C "$temporary/package"
package="$temporary/package"
for executable in bin/codex bin/codex-mcp bin/codex-code-mode-host bin/codex-responses-api-proxy codex-path/rg codex-resources/zsh/bin/zsh; do
  [ -x "$package/$executable" ] || fail "Incomplete package: $executable"
done
[ "$platform" != unknown-linux-musl ] || [ -x "$package/codex-resources/bwrap" ] || fail 'Missing Linux sandbox'
for file in codex-package.json fork-build.json LICENSE install/install.sh install/install.ps1; do
  [ -f "$package/$file" ] || fail "Incomplete package: $file"
done
version="${tag#mcp-rust-v}"
version="${version%-r*}"
[ "$("$package/bin/codex-mcp" --version)" = "codex-cli $version" ] || fail 'Unexpected fork executable version'
printf '%s\n' "$bin_dir" > "$package/.codex-mcp-bin-dir"
printf '%s\n' "$digest" > "$package/.archive-sha256"
mkdir -p "$root/releases"
destination="$root/releases/$tag-$target"
if [ -e "$destination" ] || [ -L "$destination" ]; then
  [ ! -L "$destination" ] && [ -f "$destination/.archive-sha256" ] &&
    [ "$(cat "$destination/.archive-sha256")" = "$digest" ] &&
    [ "$(cat "$destination/.codex-mcp-bin-dir")" = "$bin_dir" ] || fail "Refusing to overwrite $destination"
  [ "$("$destination/bin/codex-mcp" --version)" = "codex-cli $version" ] || fail 'Existing release is damaged; move it aside before reinstalling'
else
  mv "$package" "$destination"
fi

replace_link() {
  ln -s "$1" "$temporary/link"
  # macOS may also have GNU coreutils on PATH. Both forms atomically replace
  # the link itself, rather than moving a file into its target directory.
  mv -Tf "$temporary/link" "$2" 2>/dev/null || mv -hf "$temporary/link" "$2"
}
replace_link "$destination" "$root/current"
replace_link "$expected_link" "$command_path"
printf 'Installed %s. Run: %s\nUpdate: codex-mcp update\n' "$tag" "$command_path"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *)
    if [ "${CODEX_MCP_MODIFY_PATH:-1}" != 0 ]; then
      case "$platform:${SHELL:-}" in
        apple-darwin:*/zsh) profile="$HOME/.zprofile" ;;
        apple-darwin:*/bash) profile="$HOME/.bash_profile" ;;
        *:*/zsh) profile="$HOME/.zshrc" ;;
        *:*/bash) profile="$HOME/.bashrc" ;;
        *) profile="$HOME/.profile" ;;
      esac
      quoted="$(printf '%s' "$bin_dir" | sed "s/'/'\\\\''/g")"
      line="export PATH='$quoted':\$PATH"
      if ! grep -Fqx "$line" "$profile" 2>/dev/null; then
        printf '\n# codex-mcp installer\n%s\n' "$line" >> "$profile"
      fi
      printf 'Open a new shell to use codex-mcp on PATH (%s).\n' "$profile"
    else
      printf 'Add %s to PATH to use codex-mcp.\n' "$bin_dir"
    fi ;;
esac
